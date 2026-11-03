from __future__ import annotations

import argparse
import io
import logging
import math
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import (
    DB_PATH,
    HTML_CACHE_DIR,
    LOGS_DIR,
    REPORTS_DIR,
    Config,
    Product,
    load_config,
    load_products,
)
from .notifier import send_email, send_telegram
from .reporter import (
    PriceDrop,
    PriceDropGrupo,
    RestockAlert,
    SitioComparacion,
    build_html_email,
    build_markdown_report,
    build_telegram_message,
    save_report,
)
from .extractors import ExtractedOffer, ExtractionError
from .scraper import RedirectError, ScrapeError, fetch_offer
from .storage import Storage, StoredProduct

logger = logging.getLogger("priceCompare")


class RejectedPriceError(ValueError):
    """Precio descartado antes de modificar el historial."""


_LEVEL_ICONS = {
    "DEBUG":    "🔍",
    "INFO":     "ℹ️ ",
    "WARNING":  "⚠️ ",
    "ERROR":    "❌",
    "CRITICAL": "🔥",
}


class _EmojiFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        icon = _LEVEL_ICONS.get(record.levelname, "  ")
        record.levelname = f"{icon} {record.levelname:<8}"
        return super().format(record)


class _CapturaLogsWorkers(logging.Handler):
    """Captura los registros que emiten los hilos workers durante el scraping
    paralelo, en un buffer por hilo, para re-emitirlos agrupados por producto
    en el orden de products.json (logs coherentes pese al paralelismo)."""

    def __init__(self) -> None:
        super().__init__()
        self._buffers: dict[int, list[logging.LogRecord]] = {}
        self._registro_lock = threading.Lock()

    def registrar_hilo(self, buffer: list[logging.LogRecord]) -> None:
        with self._registro_lock:
            self._buffers[threading.get_ident()] = buffer

    def liberar_hilo(self) -> None:
        with self._registro_lock:
            self._buffers.pop(threading.get_ident(), None)

    def capturando_en_hilo_actual(self) -> bool:
        with self._registro_lock:
            return threading.get_ident() in self._buffers

    def emit(self, record: logging.LogRecord) -> None:
        with self._registro_lock:
            buffer = self._buffers.get(threading.get_ident())
        if buffer is not None:
            buffer.append(record)


class _SilenciarHilosCapturados(logging.Filter):
    """Suprime en los handlers de salida los registros de los hilos capturados,
    para que no se intercalen en consola/archivo mientras dura la fase paralela."""

    def __init__(self, captura: _CapturaLogsWorkers) -> None:
        super().__init__()
        self._captura = captura

    def filter(self, record: logging.LogRecord) -> bool:
        return not self._captura.capturando_en_hilo_actual()


def _configure_logging() -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOGS_DIR / "run.log"
    plain_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    emoji_fmt = _EmojiFormatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    file_handler = RotatingFileHandler(log_path, maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    file_handler.setFormatter(plain_fmt)
    try:
        utf8_stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
    except AttributeError:
        utf8_stdout = sys.stdout
    stream_handler = logging.StreamHandler(utf8_stdout)
    stream_handler.setFormatter(emoji_fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers = [file_handler, stream_handler]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="priceCompare — monitor de precios automático")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Consulta precios pero no guarda en BD ni envía notificaciones",
    )
    parser.add_argument(
        "--product", metavar="ID",
        help="Procesa solo el producto con este ID (tal como aparece en products.json)",
    )
    parser.add_argument(
        "--cache-html", action="store_true",
        help="Lee/guarda HTML en data/html_cache/ para iterar sin re-scrapear",
    )
    return parser.parse_args()


def _effective_target(product: Product, initial_price: float) -> float | None:
    """Resuelve el target_price efectivo: absoluto o relativo ('-20%')."""
    if product.target_price is not None:
        return product.target_price
    if product.target_price_relative is not None:
        pct_str = product.target_price_relative.replace("%", "").strip()
        try:
            pct = float(pct_str) / 100.0
            return initial_price * (1.0 + pct)
        except ValueError:
            logger.warning("target_price_relative inválido para %s: %r", product.id, product.target_price_relative)
    return None


def _sanity_check(product_id: str, current_price: float, reference_price: float, config: Config) -> bool:
    """Retorna False si el precio parece ser una extracción rota."""
    lo = reference_price * config.scraper.sanity_min_ratio
    hi = reference_price * config.scraper.sanity_max_ratio
    if current_price < lo:
        logger.warning(
            "Precio sospechoso para %s: %.2f es menos del %.0f%% del precio de referencia %.2f — ignorando",
            product_id, current_price, config.scraper.sanity_min_ratio * 100, reference_price,
        )
        return False
    if current_price > hi:
        logger.warning(
            "Precio sospechoso para %s: %.2f supera %.0fx el precio de referencia %.2f — ignorando",
            product_id, current_price, config.scraper.sanity_max_ratio, reference_price,
        )
        return False
    return True


def _log_availability(available: bool | None, name: str) -> None:
    if available is False:
        logger.warning("🚫 SIN STOCK — %s", name)
    elif available is True:
        logger.info("🛒 Disponible para compra — %s", name)
    else:
        logger.info("❓ Disponibilidad desconocida — %s", name)


def _detect_restock(
    available: bool | None, last_available: bool | None, product: Product, current_price: float
) -> RestockAlert | None:
    if available is True and last_available is False:
        logger.info("🛒 Restock detectado para %s", product.name)
        return RestockAlert(
            product_id=product.id, name=product.name, url=product.url,
            currency=product.currency, current_price=current_price,
        )
    return None


def _build_drop(
    product: Product,
    initial_price: float,
    previous_price: float,
    current_price: float,
    effective_target: float | None,
    min_info: tuple[float, str] | None,
) -> PriceDrop | None:
    """Alerta únicamente al bajar respecto al último precio válido; conserva el inicial para calcular ahorro."""
    if current_price >= previous_price:
        return None
    logger.info('Main > _build_drop - Bajada para %s: anterior %.2f → hoy %.2f', product.id, previous_price, current_price)
    return PriceDrop(
        product_id=product.id, name=product.name, url=product.url, currency=product.currency,
        initial_price=initial_price, previous_price=previous_price, current_price=current_price,
        target_price=effective_target,
        min_price=min_info[0] if min_info else None,
        min_price_date=min_info[1] if min_info else None,
    )


def _validate_captured_price(
    product: Product, current_price: float, extracted_currency: str | None,
    stored: StoredProduct | None, config: Config,
) -> None:
    """Comprueba importe, moneda y límites antes de registrar cualquier captura."""
    if not math.isfinite(current_price) or current_price <= 0:
        raise RejectedPriceError('El precio debe ser positivo y finito.')
    if extracted_currency and extracted_currency.upper() != product.currency.upper():
        raise RejectedPriceError('La moneda extraída no coincide con la configurada.')
    if stored is not None and not _sanity_check(product.id, current_price, stored.initial_price, config):
        raise RejectedPriceError(
            f'Precio sospechoso: {current_price:,.2f}; referencia {stored.initial_price:,.2f}. '
            f'Rango permitido: {stored.initial_price * config.scraper.sanity_min_ratio:,.2f}'
            f' a {stored.initial_price * config.scraper.sanity_max_ratio:,.2f}.'
        )


def _register_first_capture(
    product: Product, price: float, available: bool | None, storage: Storage, dry_run: bool,
) -> None:
    """Registra el precio inicial y su primera observación sin generar alertas."""
    logger.info('Main > _register_first_capture - Nuevo producto %s: %.2f', product.id, price)
    if dry_run:
        return
    storage.register_new_product(
        product_id=product.id, name=product.name, url=product.url,
        initial_price=price, currency=product.currency,
        target_price=_effective_target(product, price),
    )
    storage.append_price(product.id, price, available)


def _process_product(
    product: Product,
    current_price: float,
    available: bool | None,
    storage: Storage,
    config: Config,
    dry_run: bool,
    extracted_currency: str | None = None,
) -> tuple[PriceDrop | None, RestockAlert | None, float | None, float | None]:
    """Compara una captura validada con el historial y excluye ofertas agotadas de los grupos."""
    stored = storage.get_product(product.id)
    _validate_captured_price(product, current_price, extracted_currency, stored, config)
    _log_availability(available, product.name)
    if stored is not None and stored.purchased:
        logger.info("🛍️  %s marcado como comprado — omitiendo notificaciones", product.name)
        if not dry_run:
            storage.append_price(product.id, current_price, available)
        return None, None, None, None
    if stored is None:
        _register_first_capture(product, current_price, available, storage, dry_run)
        return None, None, None, current_price if available is not False else None

    previous_price = storage.last_price(product.id)
    last_available = storage.last_availability(product.id)

    if not dry_run:
        storage.append_price(product.id, current_price, available)

    if available is False:
        logger.info("Sin stock para %s — omitiendo comparación de precios", product.id)
        return None, None, previous_price, None

    restock = _detect_restock(available, last_available, product, current_price)

    if previous_price is None:
        logger.warning("Producto %s sin historial previo pese a existir", product.id)
        return None, restock, None, current_price

    effective_target = _effective_target(product, stored.initial_price)
    min_info = storage.min_price(product.id) if not dry_run else None
    drop = _build_drop(product, stored.initial_price, previous_price, current_price, effective_target, min_info)

    return drop, restock, previous_price, current_price


def _proceso_grupo_con_padre(
    grupo_id: str,
    sitios: list[SitioComparacion],
    padre_sitio: SitioComparacion,
    mejor_sitio: SitioComparacion,
) -> PriceDropGrupo | None:
    padre_curr = padre_sitio.current_price
    padre_prev = padre_sitio.previous_price
    current_min = mejor_sitio.current_price
    alguien_mas_barato = current_min < padre_curr
    padre_bajo = padre_prev is not None and padre_curr < padre_prev

    changed = any(site.previous_price is not None and site.current_price < site.previous_price for site in sitios)
    if not changed:
        return None

    if not alguien_mas_barato and not padre_bajo:
        logger.info("Grupo %s: padre '%s' (%.2f) sigue siendo el más barato y no bajó",
                    grupo_id, padre_sitio.name, padre_curr)
        return None

    if alguien_mas_barato:
        logger.info("Grupo %s: '%s' (%.2f) más barato que padre '%s' (%.2f)",
                    grupo_id, mejor_sitio.name, current_min, padre_sitio.name, padre_curr)
    else:
        logger.info("Grupo %s: padre '%s' bajó %.2f → %.2f",
                    grupo_id, padre_sitio.name, padre_prev, padre_curr)

    ref_min = padre_prev if (padre_bajo and padre_prev is not None) else padre_curr
    return PriceDropGrupo(
        grupo_id=grupo_id, sitios=sitios, previous_min=ref_min,
        current_min=current_min, mejor_sitio=mejor_sitio,
        padre_id=grupo_id, padre_current_price=padre_curr,
    )


def _proceso_grupo_sin_padre(
    grupo_id: str,
    sitios: list[SitioComparacion],
    mejor_sitio: SitioComparacion,
) -> PriceDropGrupo | None:
    sitios_con_prev = [s for s in sitios if s.previous_price is not None]
    if not sitios_con_prev:
        return None

    current_min = mejor_sitio.current_price
    previous_min = min(s.previous_price for s in sitios_con_prev)  # type: ignore[arg-type]
    changed = any(site.previous_price is not None and site.current_price < site.previous_price for site in sitios)
    if not changed:
        return None

    if current_min >= previous_min:
        logger.info("Grupo %s sin nuevo mínimo: anterior=%.2f, hoy=%.2f", grupo_id, previous_min, current_min)
        return None

    logger.info("Nuevo mínimo en grupo %s: %.2f → %.2f (mejor: %s)",
                grupo_id, previous_min, current_min, mejor_sitio.name)
    return PriceDropGrupo(grupo_id=grupo_id, sitios=sitios, previous_min=previous_min,
                          current_min=current_min, mejor_sitio=mejor_sitio)


def _process_grupo(
    grupo_id: str,
    products: list[Product],
    results: dict[str, tuple[float | None, float | None]],
) -> PriceDropGrupo | None:
    sitios: list[SitioComparacion] = []
    for p in products:
        prev, curr = results.get(p.id, (None, None))
        if curr is None:
            continue
        sitios.append(SitioComparacion(
            product_id=p.id, name=p.name, url=p.url,
            currency=p.currency, current_price=curr, previous_price=prev,
        ))

    if len(sitios) < 2:
        return None

    if len({site.currency.upper() for site in sitios}) > 1:
        logger.warning('Main > _process_grupo - Monedas incompatibles en grupo %s', grupo_id)
        return None

    mejor_sitio = min(sitios, key=lambda s: s.current_price)
    padre_sitio = next((s for s in sitios if s.product_id == grupo_id), None)

    if padre_sitio is not None:
        return _proceso_grupo_con_padre(grupo_id, sitios, padre_sitio, mejor_sitio)
    return _proceso_grupo_sin_padre(grupo_id, sitios, mejor_sitio)


def _scrape_error_reason(error: Exception) -> str:
    """Clasifica fallos sin guardar respuestas, URLs ni datos sensibles del transporte."""
    if isinstance(error, ExtractionError):
        return 'Selector CSS inválido.'
    if isinstance(error, RedirectError):
        return 'La página redirigió a otro dominio.'
    if isinstance(error, ScrapeError):
        return 'No se pudo cargar la página o extraer un precio válido.'
    return f'Error de extracción ({type(error).__name__}).'


def _accept_offer(
    product: Product, offer: ExtractedOffer, storage: Storage, config: Config, dry_run: bool,
) -> tuple[PriceDrop | None, RestockAlert | None, float | None, float | None, bool]:
    """Valida una oferta y registra su aceptación o rechazo sin detener otros productos."""
    try:
        if offer.price is None:
            raise RejectedPriceError('No se extrajo un precio válido.')
        result = _process_product(product, offer.price, offer.available, storage, config, dry_run, offer.currency)
    except RejectedPriceError as error:
        logger.warning('Main > _accept_offer - Precio rechazado para %s: %s', product.id, error)
        if not dry_run:
            storage.record_scrape_result(product.id, 'rejected', str(error), offer.method)
        return None, None, None, None, True
    if not dry_run:
        storage.record_scrape_result(product.id, 'success', method=offer.method)
    return (*result, False)


def _process_scrape_result(
    product: Product, result: tuple[ExtractedOffer | None, str | None, list[logging.LogRecord], float],
    storage: Storage, config: Config, dry_run: bool,
) -> tuple[PriceDrop | None, RestockAlert | None, float | None, float | None, bool]:
    """Procesa un resultado de red y registra fallos antes de continuar con el siguiente producto."""
    offer, error, records, duration = result
    logger.info('Main > _process_scrape_result - Consultado %s (%.1fs)', product.name, duration)
    for record in records:
        logging.getLogger(record.name).handle(record)
    if error is not None:
        logger.error('Main > _process_scrape_result - Scrape falló para %s: %s', product.id, error)
        if not dry_run:
            storage.record_scrape_result(product.id, 'error', error)
        return None, None, None, None, True
    return _accept_offer(product, offer or ExtractedOffer(), storage, config, dry_run)


def _build_group_alerts(
    products: list[Product], price_results: dict[str, tuple[float | None, float | None]], drops: list[PriceDrop],
) -> tuple[list[PriceDrop], list[PriceDropGrupo]]:
    """Construye comparaciones y evita duplicar productos ya incluidos en una alerta de grupo."""
    groups: dict[str, list[Product]] = {}
    for product in products:
        if product.grupo:
            groups.setdefault(product.grupo, []).append(product)
    group_drops = [
        drop for group_id, members in groups.items()
        if (drop := _process_grupo(group_id, members, price_results)) is not None
    ]
    reported_groups = {drop.grupo_id for drop in group_drops}
    product_groups = {product.id: product.grupo for product in products}
    individual_drops = [drop for drop in drops if product_groups.get(drop.product_id) not in reported_groups]
    return individual_drops, group_drops


def _scrape_all(
    products: list[Product],
    storage: Storage,
    config: Config,
    cache_dir: Path | None,
    dry_run: bool,
) -> tuple[list[PriceDrop], list[PriceDropGrupo], list[RestockAlert], bool]:
    drops: list[PriceDrop] = []
    restocks: list[RestockAlert] = []
    price_results: dict[str, tuple[float | None, float | None]] = {}
    had_errors = False

    # Fase 1 (paralela): solo red. Fase 2 (secuencial, hilo principal): SQLite y decisiones.
    # Los logs de cada worker se capturan en un buffer y se re-emiten agrupados por
    # producto en el orden original de products.json, para que el log se lea igual
    # que una corrida secuencial.
    workers = max(1, config.scraper.max_workers)
    logger.info("🔎 Consultando %d producto(s) con %d worker(s)", len(products), workers)

    captura = _CapturaLogsWorkers()
    silenciador = _SilenciarHilosCapturados(captura)
    root = logging.getLogger()
    handlers_salida = list(root.handlers)
    for h in handlers_salida:
        h.addFilter(silenciador)
    root.addHandler(captura)

    def _fetch_con_captura(
        product: Product,
    ) -> tuple[ExtractedOffer | None, str | None, list[logging.LogRecord], float]:
        """Aísla errores del scraping y captura los logs del producto."""
        buffer: list[logging.LogRecord] = []
        captura.registrar_hilo(buffer)
        inicio = time.monotonic()
        try:
            try:
                offer = fetch_offer(product.url, product.selector, config, cache_dir, product.currency)
                return offer, None, buffer, time.monotonic() - inicio
            except Exception as exc:
                return None, _scrape_error_reason(exc), buffer, time.monotonic() - inicio
        finally:
            captura.liberar_hilo()

    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futuros = {p.id: pool.submit(_fetch_con_captura, p) for p in products}
            for product in products:
                drop, restock, prev, curr, rejected = _process_scrape_result(
                    product, futuros[product.id].result(), storage, config, dry_run
                )
                had_errors = had_errors or rejected
                if drop is not None:
                    drops.append(drop)
                if restock is not None:
                    restocks.append(restock)
                price_results[product.id] = (prev, curr)
    finally:
        root.removeHandler(captura)
        for h in handlers_salida:
            h.removeFilter(silenciador)

    drops, grupo_drops = _build_group_alerts(products, price_results, drops)
    return drops, grupo_drops, restocks, had_errors


def _notificar(
    drops: list[PriceDrop],
    grupo_drops: list[PriceDropGrupo],
    restocks: list[RestockAlert],
    config: Config,
    history_map: dict[str, list[tuple[str, float]]],
) -> None:
    run_date = date.today()
    gd = grupo_drops or None
    ra = restocks or None
    markdown = build_markdown_report(drops, run_date, gd, ra)
    report_path = save_report(markdown, REPORTS_DIR, run_date)
    logger.info("Reporte guardado en %s", report_path)

    if send_telegram(build_telegram_message(drops, gd, ra), config.telegram):
        logger.info("Telegram enviado.")

    total = len(drops) + len(grupo_drops) + len(restocks)
    subject = f"[priceCompare] {total} novedad(es) de precio - {run_date.isoformat()}"
    html_body = build_html_email(drops, run_date, gd, ra, history_map)
    if send_email(subject, markdown, config.email, body_html=html_body):
        logger.info("Email enviado.")


def run() -> int:
    args = _parse_args()
    _configure_logging()
    logger.info("=== priceCompare iniciando corrida%s ===", " (DRY-RUN)" if args.dry_run else "")

    config = load_config()

    all_products = [p for p in load_products() if p.active]
    if args.product:
        products = [p for p in all_products if p.id == args.product]
        if not products:
            logger.error("No se encontró ningún producto activo con id=%r", args.product)
            return 1
    else:
        products = all_products

    if not products:
        logger.warning("No hay productos activos en products.json")
        return 0

    use_cache = args.cache_html or config.scraper.cache_html
    cache_dir = HTML_CACHE_DIR if use_cache else None
    if cache_dir:
        logger.info("Caché HTML activa en %s", cache_dir)

    storage = Storage(DB_PATH)
    try:
        drops, grupo_drops, restocks, had_errors = _scrape_all(
            products, storage, config, cache_dir, args.dry_run
        )
        history_map = {d.product_id: storage.price_history(d.product_id, 30) for d in drops}
    finally:
        storage.close()

    if args.dry_run:
        logger.info(
            "[DRY-RUN] Resultados: %d bajada(s), %d restock(s), errores=%s — sin guardar ni notificar",
            len(drops), len(restocks), had_errors,
        )
        return 0

    if not drops and not grupo_drops and not restocks:
        logger.info("No se detectaron novedades de precio. Sin notificaciones.")
        return 1 if had_errors else 0

    _notificar(drops, grupo_drops, restocks, config, history_map)
    return 0


if __name__ == "__main__":
    sys.exit(run())
