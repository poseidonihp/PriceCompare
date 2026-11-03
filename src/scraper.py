from __future__ import annotations

import functools
import hashlib
import logging
import threading
import time
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlparse

import requests

from .config import Config
from .extractors import ExtractedOffer, extract_offer, validate_selector

logger = logging.getLogger(__name__)

# Evita que dos Chromium no-headless corran a la vez (roban foco y duplican memoria)
_lock_no_headless = threading.Lock()


class ScrapeError(Exception):
    pass


class RedirectError(ScrapeError):
    pass


def _same_domain(url_a: str, url_b: str) -> bool:
    return urlparse(url_a).netloc == urlparse(url_b).netloc


def _cache_filename(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest() + ".html"


def _load_cache(cache_dir: Path, url: str) -> str | None:
    path = cache_dir / _cache_filename(url)
    if path.exists():
        return path.read_text(encoding="utf-8", errors="replace")
    return None


def _save_cache(cache_dir: Path, url: str, html: str) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / _cache_filename(url)).write_text(html, encoding="utf-8")


def _fetch_http(url: str, config: Config) -> str:
    headers = {
        "User-Agent": config.scraper.user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
    }
    response = requests.get(
        url,
        headers=headers,
        timeout=config.scraper.http_timeout_seconds,
    )
    response.raise_for_status()
    final_url = response.url
    if not _same_domain(url, final_url):
        raise RedirectError(
            f"La URL redirigió a un dominio distinto: {url!r} → {final_url!r}"
        )
    if final_url.rstrip("/") != url.rstrip("/"):
        logger.warning("Redirección dentro del mismo dominio: %s → %s", url, final_url)
    return response.text


def _es_reintentable(exc: requests.RequestException) -> bool:
    """Un fallo transitorio (timeout, conexión, 5xx, 429) merece reintento;
    un 4xx distinto de 429 (p. ej. 403 anti-bot) no mejora esperando."""
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        status = exc.response.status_code
        return status == 429 or status >= 500
    return True


def _fetch_http_con_reintentos(url: str, config: Config) -> str:
    """Ejecuta _fetch_http con reintentos y backoff exponencial ante fallos transitorios.
    No reintenta RedirectError ni errores HTTP 4xx (salvo 429)."""
    total_intentos = 1 + max(0, config.scraper.http_retries)
    for intento in range(total_intentos):
        try:
            return _fetch_http(url, config)
        except RedirectError:
            raise
        except requests.RequestException as exc:
            if intento + 1 >= total_intentos or not _es_reintentable(exc):
                raise
            espera = config.scraper.http_backoff_base_seconds * (2 ** intento)
            logger.warning(
                "Intento HTTP %d/%d falló para %s (%s); reintentando en %.1fs",
                intento + 1, total_intentos, url, exc, espera,
            )
            time.sleep(espera)
    raise ScrapeError(f"Reintentos HTTP agotados para {url}")  # inalcanzable, por tipado


@functools.lru_cache(maxsize=4)
def _obtener_stealth(user_agent: str):
    """Construye (una sola vez) la configuración stealth para Playwright.

    El objeto Stealth es inmutable tras crearse y seguro de compartir entre hilos:
    solo genera un init-script que se inyecta en cada contexto."""
    from playwright_stealth import Stealth

    return Stealth(
        navigator_user_agent_override=user_agent,
        navigator_languages_override=("es-ES", "es"),
    )


def _fetch_playwright(url: str, config: Config, headless: bool = True) -> str:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        args = ["--disable-blink-features=AutomationControlled", "--accept-lang=es-ES,es"]
        if not headless:
            args += ["--window-position=-32000,-32000", "--window-size=1,1"]
        browser = p.chromium.launch(headless=headless, args=args)
        try:
            context = browser.new_context(user_agent=config.scraper.user_agent)
            if config.scraper.stealth_enabled:
                _obtener_stealth(config.scraper.user_agent).apply_stealth_sync(context)
            page = context.new_page()
            page.goto(
                url,
                timeout=config.scraper.playwright_timeout_seconds * 1000,
                wait_until="domcontentloaded",
            )
            final_url = page.url
            if not _same_domain(url, final_url):
                raise RedirectError(
                    f"La URL redirigió a un dominio distinto: {url!r} → {final_url!r}"
                )
            if final_url.rstrip("/") != url.rstrip("/"):
                logger.warning("Redirección dentro del mismo dominio: %s → %s", url, final_url)
            try:
                page.wait_for_load_state(
                    "networkidle",
                    timeout=config.scraper.playwright_timeout_seconds * 1000,
                )
            except Exception:
                pass
            return page.content()
        finally:
            browser.close()


def _try_playwright(
    url: str,
    selector: str | None,
    config: Config,
    headless: bool,
    expected_currency: str | None = None,
) -> tuple[ExtractedOffer, str] | None:
    """Obtiene una oferta del HTML renderizado manteniendo precio, moneda y stock unidos."""
    try:
        html = _fetch_playwright(url, config, headless=headless)
    except Exception as exc:
        raise ScrapeError(f"Playwright falló al cargar {url}: {exc}") from exc
    offer = extract_offer(html, selector, url, expected_currency)
    return (offer, html) if offer.price is not None else None


def fetch_offer(
    url: str,
    selector: str | None,
    config: Config,
    cache_dir: Path | None = None,
    expected_currency: str | None = None,
) -> ExtractedOffer:
    """Consulta una oferta y conserva el método de extracción.

    Args:
        url: Página del producto.
        selector: Selector opcional.
        config: Configuración de transporte.
        cache_dir: Caché opcional.
        expected_currency: Moneda configurada.
    Returns:
        ExtractedOffer
    """
    validate_selector(selector)

    if cache_dir is not None:
        cached_html = _load_cache(cache_dir, url)
        if cached_html is not None:
            offer = extract_offer(cached_html, selector, url, expected_currency)
            if offer.price is not None:
                logger.info("Precio extraído desde caché HTML: %s", url)
                return replace(offer, method=f'cache:{offer.method}')
            logger.info("Caché HTML no tiene precio válido, reintentando en red: %s", url)

    try:
        html = _fetch_http_con_reintentos(url, config)
        offer = extract_offer(html, selector, url, expected_currency)
        if offer.price is not None:
            logger.info('Scraper > fetch_offer - Precio extraído vía HTTP: %s', offer.price)
            if cache_dir is not None:
                _save_cache(cache_dir, url, html)
            return replace(offer, method=f'http:{offer.method}')
        logger.info("HTTP no extrajo precio, reintentando con Playwright: %s", url)
    except requests.RequestException as exc:
        logger.warning("Fallo HTTP (%s), reintentando con Playwright: %s", exc, url)

    result = _try_playwright(url, selector, config, headless=True, expected_currency=expected_currency)
    if result is not None:
        offer, html = result
        logger.info('Scraper > fetch_offer - Precio extraído vía Playwright headless: %s', offer.price)
        if cache_dir is not None:
            _save_cache(cache_dir, url, html)
        return replace(offer, method=f'playwright_headless:{offer.method}')

    logger.info("Playwright headless bloqueado, reintentando sin headless: %s", url)
    with _lock_no_headless:
        result = _try_playwright(url, selector, config, headless=False, expected_currency=expected_currency)
    if result is None:
        raise ScrapeError(f"No se pudo extraer precio de {url}")
    offer, _ = result
    logger.info('Scraper > fetch_offer - Precio extraído vía Playwright no-headless: %s', offer.price)
    return replace(offer, method=f'playwright_visible:{offer.method}')


def fetch_price(
    url: str, selector: str | None, config: Config, cache_dir: Path | None = None,
) -> tuple[float, bool | None]:
    """Conserva la interfaz de consulta existente.

    Args:
        url: Página del producto.
        selector: Selector opcional.
        config: Configuración de transporte.
        cache_dir: Caché opcional.
    Returns:
        tuple[float, bool | None]
    """
    offer = fetch_offer(url, selector, config, cache_dir)
    if offer.price is None:
        raise ScrapeError('No se pudo extraer un precio válido.')
    return offer.price, offer.available
