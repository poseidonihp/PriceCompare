from __future__ import annotations

import base64
import io
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path


@dataclass
class PriceDrop:
    product_id: str
    name: str
    url: str
    currency: str
    initial_price: float
    previous_price: float
    current_price: float
    target_price: float | None
    min_price: float | None = None
    min_price_date: str | None = None

    @property
    def is_historical_min(self) -> bool:
        return self.min_price is not None and self.current_price <= self.min_price

    @property
    def delta_vs_previous(self) -> float:
        return self.current_price - self.previous_price

    @property
    def delta_vs_initial(self) -> float:
        return self.current_price - self.initial_price

    @property
    def pct_vs_previous(self) -> float:
        if self.previous_price == 0:
            return 0.0
        return (self.delta_vs_previous / self.previous_price) * 100

    @property
    def pct_vs_initial(self) -> float:
        if self.initial_price == 0:
            return 0.0
        return (self.delta_vs_initial / self.initial_price) * 100

    @property
    def hit_target(self) -> bool:
        return self.target_price is not None and self.current_price <= self.target_price


@dataclass
class RestockAlert:
    product_id: str
    name: str
    url: str
    currency: str
    current_price: float


@dataclass
class SitioComparacion:
    product_id: str
    name: str
    url: str
    currency: str
    current_price: float
    previous_price: float | None

    @property
    def delta(self) -> float | None:
        if self.previous_price is None:
            return None
        return self.current_price - self.previous_price

    @property
    def pct(self) -> float | None:
        if self.previous_price is None or self.previous_price == 0:
            return None
        return (self.current_price - self.previous_price) / self.previous_price * 100


@dataclass
class PriceDropGrupo:
    grupo_id: str
    sitios: list[SitioComparacion]
    previous_min: float
    current_min: float
    mejor_sitio: SitioComparacion
    padre_id: str | None = None
    padre_current_price: float | None = None

    @property
    def delta_min(self) -> float:
        return self.current_min - self.previous_min

    @property
    def pct_min(self) -> float:
        if self.previous_min == 0:
            return 0.0
        return self.delta_min / self.previous_min * 100


def _fmt(value: float, currency: str) -> str:
    return f"{currency} {value:,.2f}"


def _sparkline_base64(history: list[tuple[str, float]]) -> str | None:
    """Genera un sparkline PNG del historial de precios y lo retorna en base64."""
    if len(history) < 2:
        return None
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        prices = [p for _, p in history]
        fig, ax = plt.subplots(figsize=(3.2, 0.9))
        ax.plot(prices, color="#2563eb", linewidth=1.8, solid_capstyle="round")
        ax.fill_between(range(len(prices)), prices, alpha=0.12, color="#2563eb")
        ax.axis("off")
        fig.patch.set_alpha(0)
        buf = io.BytesIO()
        fig.savefig(buf, format="png", bbox_inches="tight", dpi=72, transparent=True)
        plt.close(fig)
        buf.seek(0)
        return base64.b64encode(buf.read()).decode("ascii")
    except Exception:
        return None


# ──────────────────────────────── Markdown ────────────────────────────────

def _md_fila_drop(d: PriceDrop) -> str:
    target_cell = _fmt(d.target_price, d.currency) if d.target_price is not None else "—"
    hit_flag = " ✅" if d.hit_target else ""
    min_cell = "—"
    if d.min_price is not None:
        min_date = ""
        if d.min_price_date:
            try:
                dt = datetime.fromisoformat(d.min_price_date)
                min_date = f" ({dt.strftime('%d/%m/%y')})"
            except ValueError:
                pass
        new_min_flag = " 🏆" if d.is_historical_min else ""
        min_cell = f"{_fmt(d.min_price, d.currency)}{min_date}{new_min_flag}"
    return (
        f"| [{d.name}]({d.url}) "
        f"| {_fmt(d.initial_price, d.currency)} "
        f"| {_fmt(d.previous_price, d.currency)} "
        f"| {_fmt(d.current_price, d.currency)} "
        f"| {d.delta_vs_previous:+,.2f} ({d.pct_vs_previous:+.1f}%) "
        f"| {d.delta_vs_initial:+,.2f} ({d.pct_vs_initial:+.1f}%) "
        f"| {-d.pct_vs_initial:.1f}% "
        f"| {min_cell} "
        f"| {target_cell}{hit_flag} |"
    )


def _md_seccion_drops(drops: list[PriceDrop]) -> list[str]:
    lines = [
        "| Producto | Inicial | Anterior | Hoy | Δ vs anterior | Δ vs inicial | % ahorro | Mín. histórico | Objetivo |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    lines += [_md_fila_drop(d) for d in drops]
    lines.append("")
    return lines


def _md_seccion_restocks(alerts: list[RestockAlert]) -> list[str]:
    lines = ["## 🛒 Volvieron a tener stock", ""]
    for a in alerts:
        lines.append(f"- [{a.name}]({a.url}) — {_fmt(a.current_price, a.currency)}")
    lines.append("")
    return lines


def _md_fila_sitio(s: SitioComparacion, mejor_id: str) -> str:
    mejor_flag = " 🏆" if s.product_id == mejor_id else ""
    delta_str = f"{s.delta:+,.2f} ({s.pct:+.1f}%)" if s.delta is not None else "—"
    prev_str = _fmt(s.previous_price, s.currency) if s.previous_price is not None else "—"
    return (
        f"| [{s.name}]({s.url}) "
        f"| {_fmt(s.current_price, s.currency)} "
        f"| {prev_str} "
        f"| {delta_str} "
        f"| {mejor_flag} |"
    )


def _md_fila_sitio_padre(s: SitioComparacion, mejor_id: str, padre_id: str, padre_price: float) -> str:
    mejor_flag = " 🏆" if s.product_id == mejor_id else ""
    padre_mark = " 📍" if s.product_id == padre_id else ""
    if s.product_id == padre_id:
        vs_str = "— (referencia)"
    else:
        delta = s.current_price - padre_price
        pct = (delta / padre_price) * 100 if padre_price != 0 else 0.0
        vs_str = f"{delta:+,.2f} ({pct:+.1f}%)"
    if s.previous_price is None:
        cambio_str = "—"
    elif s.delta == 0:
        cambio_str = "sin cambio"
    else:
        cambio_str = f"{_fmt(s.previous_price, s.currency)} → {_fmt(s.current_price, s.currency)} ({s.pct:+.1f}%)"
    return (
        f"| [{s.name}]({s.url}){padre_mark}{mejor_flag} "
        f"| {_fmt(s.current_price, s.currency)} "
        f"| {cambio_str} "
        f"| {vs_str} |"
    )


def _md_seccion_grupo(g: PriceDropGrupo) -> list[str]:
    currency = g.mejor_sitio.currency
    if g.padre_id is not None and g.padre_current_price is not None:
        padre_sitio = next((s for s in g.sitios if s.product_id == g.padre_id), None)
        padre_name = padre_sitio.name if padre_sitio else g.padre_id
        lines = [
            f"### {g.grupo_id} — mejor precio: {_fmt(g.current_min, currency)} "
            f"({g.pct_min:+.1f}% vs {padre_name} {_fmt(g.previous_min, currency)})",
            "",
            "| Sitio | Precio actual | Cambio vs anterior | vs Padre |",
            "|---|---|---|---|",
        ]
        for s in sorted(g.sitios, key=lambda s: s.current_price):
            lines.append(_md_fila_sitio_padre(s, g.mejor_sitio.product_id, g.padre_id, g.padre_current_price))
    else:
        lines = [
            f"### {g.grupo_id} — nuevo mínimo: {_fmt(g.current_min, currency)} "
            f"({g.pct_min:+.1f}% vs anterior {_fmt(g.previous_min, currency)})",
            "",
            "| Sitio | Precio actual | Anterior | Cambio | Mejor |",
            "|---|---|---|---|---|",
        ]
        for s in sorted(g.sitios, key=lambda s: s.current_price):
            lines.append(_md_fila_sitio(s, g.mejor_sitio.product_id))
    lines.append("")
    return lines


def build_markdown_report(
    drops: list[PriceDrop],
    run_date: date,
    grupo_drops: list[PriceDropGrupo] | None = None,
    restock_alerts: list[RestockAlert] | None = None,
) -> str:
    lines: list[str] = [
        f"# Reporte de bajadas de precio — {run_date.isoformat()}",
        "",
        f"Se detectaron **{len(drops)}** productos con precio más bajo.",
        "",
    ]
    if drops:
        lines += _md_seccion_drops(drops)
    if grupo_drops:
        lines += ["## Comparación multi-sitio", ""]
        for g in grupo_drops:
            lines += _md_seccion_grupo(g)
    if restock_alerts:
        lines += _md_seccion_restocks(restock_alerts)
    return "\n".join(lines)


# ──────────────────────────────── Telegram ────────────────────────────────

def _tg_bloque_drop(d: PriceDrop) -> str:
    lines = [
        f"• {d.name}",
        f"   {_fmt(d.previous_price, d.currency)} → {_fmt(d.current_price, d.currency)} "
        f"({d.delta_vs_previous:+,.2f}, {d.pct_vs_previous:+.1f}%)",
        f"   vs inicial {_fmt(d.initial_price, d.currency)}: "
        f"ahorras {_fmt(-d.delta_vs_initial, d.currency)} ({-d.pct_vs_initial:.1f}%)",
    ]
    if d.is_historical_min:
        lines.append("   🏆 ¡Nuevo mínimo histórico!")
    elif d.min_price is not None:
        lines.append(f"   Mín. histórico: {_fmt(d.min_price, d.currency)}")
    if d.hit_target:
        lines.append(f"   ✅ ¡Alcanzó tu precio objetivo de {_fmt(d.target_price, d.currency)}!")
    lines.append(f"   {d.url}")
    return "\n".join(lines)


def _tg_bloque_restock(a: RestockAlert) -> str:
    return (
        f"• {a.name}\n"
        f"   🛒 Volvió al stock — {_fmt(a.current_price, a.currency)}\n"
        f"   {a.url}"
    )


def _tg_vs_padre(s: SitioComparacion, padre_id: str, padre_price: float) -> str:
    if s.product_id == padre_id:
        return " (referencia)"
    delta = s.current_price - padre_price
    pct = (delta / padre_price) * 100 if padre_price != 0 else 0.0
    return f" ({pct:+.1f}% vs padre)"


def _tg_cambio_vs_anterior(s: SitioComparacion) -> str:
    if s.previous_price is None or s.delta is None or s.delta == 0:
        return ""
    if s.delta < 0:
        return f" [▼ {-s.pct:.1f}% vs {_fmt(s.previous_price, s.currency)}]"
    return f" [▲ {s.pct:+.1f}% vs {_fmt(s.previous_price, s.currency)}]"


def _tg_lineas_sitios_padre(g: PriceDropGrupo) -> list[str]:
    padre_price = g.padre_current_price or 0.0
    return [
        f"     {s.name}"
        f"{'  📍' if s.product_id == g.padre_id else ''}"
        f"{'  🏆' if s.product_id == g.mejor_sitio.product_id else ''}"
        f": {_fmt(s.current_price, s.currency)}"
        f"{_tg_vs_padre(s, g.padre_id or '', padre_price)}"
        f"{_tg_cambio_vs_anterior(s)}"
        for s in sorted(g.sitios, key=lambda s: s.current_price)
    ]


def _tg_bloque_grupo(g: PriceDropGrupo) -> str:
    currency = g.mejor_sitio.currency
    lines = [f"📦 {g.grupo_id}"]
    if g.padre_id is not None and g.padre_current_price is not None:
        padre_sitio = next((s for s in g.sitios if s.product_id == g.padre_id), None)
        padre_name = padre_sitio.name if padre_sitio else g.padre_id
        lines += [
            f"   Más barato que {padre_name}: {_fmt(g.current_min, currency)} ({g.pct_min:+.1f}%)",
            f"   Mejor sitio: {g.mejor_sitio.name}",
            f"   {g.mejor_sitio.url}",
            f"   Comparativa ({len(g.sitios)} sitios):",
        ]
        lines += _tg_lineas_sitios_padre(g)
    else:
        lines += [
            f"   Nuevo mínimo: {_fmt(g.current_min, currency)} ({g.pct_min:+.1f}%)",
            f"   Mejor sitio: {g.mejor_sitio.name}",
            f"   {g.mejor_sitio.url}",
            f"   Comparativa ({len(g.sitios)} sitios):",
        ]
        for s in sorted(g.sitios, key=lambda s: s.current_price):
            bandera = " 🏆" if s.product_id == g.mejor_sitio.product_id else ""
            lines.append(f"     {s.name}: {_fmt(s.current_price, s.currency)}{bandera}")
    return "\n".join(lines)


def build_telegram_message(
    drops: list[PriceDrop],
    grupo_drops: list[PriceDropGrupo] | None = None,
    restock_alerts: list[RestockAlert] | None = None,
) -> str:
    blocks: list[str] = []

    if drops:
        blocks.append(f"💰 {len(drops)} producto(s) bajaron de precio hoy:")
        blocks.append("")
        for d in drops:
            blocks.append(_tg_bloque_drop(d))
            blocks.append("")

    if grupo_drops:
        blocks.append(f"🔀 {len(grupo_drops)} grupo(s) con nuevo precio mínimo:")
        blocks.append("")
        for g in grupo_drops:
            blocks.append(_tg_bloque_grupo(g))
            blocks.append("")

    if restock_alerts:
        blocks.append(f"🛒 {len(restock_alerts)} producto(s) volvieron al stock:")
        blocks.append("")
        for a in restock_alerts:
            blocks.append(_tg_bloque_restock(a))
            blocks.append("")

    return "\n".join(blocks).strip()


# ──────────────────────────────── HTML email ────────────────────────────────

def _html_badge_vs_padre(s: SitioComparacion, padre_id: str, padre_price: float) -> str:
    if s.product_id == padre_id:
        return "<span style='color:#6b7280;font-size:13px'>referencia</span>"
    delta = s.current_price - padre_price
    pct = (delta / padre_price) * 100 if padre_price != 0 else 0.0
    if pct < 0:
        return (f"<span style='background:#dcfce7;color:#15803d;font-weight:700;"
                f"padding:4px 10px;border-radius:999px;font-size:13px'>▼ {-pct:.1f}%</span>")
    return f"<span style='color:#6b7280'>{delta:+,.2f} ({pct:+.1f}%)</span>"


def _html_cambio_vs_anterior(s: SitioComparacion) -> str:
    if s.previous_price is None:
        return "<span style='color:#9ca3af;font-size:13px'>—</span>"
    if s.delta is None or s.delta == 0:
        return "<span style='color:#6b7280;font-size:13px'>sin cambio</span>"
    if s.delta < 0:
        return (
            f"<span style='color:#6b7280;text-decoration:line-through;font-size:12px'>{_fmt(s.previous_price, s.currency)}</span> "
            f"<span style='background:#dcfce7;color:#15803d;font-weight:700;padding:3px 8px;border-radius:999px;font-size:12px'>▼ {-s.pct:.1f}%</span>"
        )
    return (
        f"<span style='color:#6b7280;font-size:12px'>{_fmt(s.previous_price, s.currency)} →</span> "
        f"<span style='color:#b91c1c;font-weight:600;font-size:12px'>+{s.pct:.1f}%</span>"
    )


def _html_fila_sitio_padre(s: SitioComparacion, mejor_id: str, padre_id: str, padre_price: float) -> str:
    bandera = " 🏆" if s.product_id == mejor_id else ""
    padre_mark = " 📍" if s.product_id == padre_id else ""
    return (
        f"<tr><td style='padding:10px 12px;border-bottom:1px solid #e5e7eb'>"
        f"<a href='{s.url}' style='color:#2563eb;text-decoration:none'>{s.name}{padre_mark}{bandera}</a></td>"
        f"<td style='padding:10px 12px;border-bottom:1px solid #e5e7eb;text-align:right;font-weight:700'>{_fmt(s.current_price, s.currency)}</td>"
        f"<td style='padding:10px 12px;border-bottom:1px solid #e5e7eb;text-align:right'>{_html_cambio_vs_anterior(s)}</td>"
        f"<td style='padding:10px 12px;border-bottom:1px solid #e5e7eb;text-align:center'>{_html_badge_vs_padre(s, padre_id, padre_price)}</td></tr>"
    )


def _html_fila_sitio_simple(s: SitioComparacion, mejor_id: str) -> str:
    bandera = " 🏆" if s.product_id == mejor_id else ""
    delta_str = f"{s.delta:+,.2f} ({s.pct:+.1f}%)" if s.delta is not None else "—"
    return (
        f"<tr><td style='padding:10px 12px;border-bottom:1px solid #e5e7eb'>"
        f"<a href='{s.url}' style='color:#2563eb;text-decoration:none'>{s.name}</a>{bandera}</td>"
        f"<td style='padding:10px 12px;border-bottom:1px solid #e5e7eb;text-align:right;font-weight:700'>{_fmt(s.current_price, s.currency)}</td>"
        f"<td style='padding:10px 12px;border-bottom:1px solid #e5e7eb;text-align:right;color:#6b7280'>{delta_str}</td></tr>"
    )


def _html_tabla_grupo(g: PriceDropGrupo) -> str:
    currency = g.mejor_sitio.currency
    sitios_ord = sorted(g.sitios, key=lambda s: s.current_price)
    if g.padre_id is not None and g.padre_current_price is not None:
        padre_sitio = next((s for s in g.sitios if s.product_id == g.padre_id), None)
        padre_name = padre_sitio.name if padre_sitio else g.padre_id
        titulo = (f"{g.grupo_id} — mejor precio: {_fmt(g.current_min, currency)} "
                  f"({g.pct_min:+.1f}% vs {padre_name})")
        filas = "".join(
            _html_fila_sitio_padre(s, g.mejor_sitio.product_id, g.padre_id, g.padre_current_price)
            for s in sitios_ord
        )
        encabezados = (
            "<th style='padding:10px 12px;text-align:left;font-size:12px;color:#6b7280;border-bottom:1px solid #e5e7eb'>Sitio</th>"
            "<th style='padding:10px 12px;text-align:right;font-size:12px;color:#6b7280;border-bottom:1px solid #e5e7eb'>Precio actual</th>"
            "<th style='padding:10px 12px;text-align:right;font-size:12px;color:#6b7280;border-bottom:1px solid #e5e7eb'>Cambio vs anterior</th>"
            "<th style='padding:10px 12px;text-align:center;font-size:12px;color:#6b7280;border-bottom:1px solid #e5e7eb'>vs Padre</th>"
        )
    else:
        titulo = (f"{g.grupo_id} — nuevo mínimo: {_fmt(g.current_min, currency)} "
                  f"({g.pct_min:+.1f}%)")
        filas = "".join(_html_fila_sitio_simple(s, g.mejor_sitio.product_id) for s in sitios_ord)
        encabezados = (
            "<th style='padding:10px 12px;text-align:left;font-size:12px;color:#6b7280;border-bottom:1px solid #e5e7eb'>Sitio</th>"
            "<th style='padding:10px 12px;text-align:right;font-size:12px;color:#6b7280;border-bottom:1px solid #e5e7eb'>Precio actual</th>"
            "<th style='padding:10px 12px;text-align:right;font-size:12px;color:#6b7280;border-bottom:1px solid #e5e7eb'>Cambio</th>"
        )
    return (
        f"<p style='margin:0 0 8px;font-weight:600;color:#111827'>{titulo}</p>"
        f"<table width='100%' cellpadding='0' cellspacing='0' style='border-collapse:collapse;border:1px solid #e5e7eb;border-radius:8px;overflow:hidden;margin-bottom:24px'>"
        f"<thead><tr style='background:#f9fafb'>{encabezados}</tr></thead>"
        f"<tbody>{filas}</tbody></table>"
    )


def _html_tabla_grupos(grupo_drops: list[PriceDropGrupo]) -> str:
    contenido = "".join(_html_tabla_grupo(g) for g in grupo_drops)
    return (
        f"<tr><td style='padding:24px 40px 0'>"
        f"<h2 style='margin:0 0 16px;color:#1e40af;font-size:18px'>Comparación multi-sitio</h2>"
        f"{contenido}</td></tr>"
    )


def _html_tabla_restocks(alerts: list[RestockAlert]) -> str:
    filas = "".join(
        f"<tr>"
        f"<td style='padding:14px 16px;border-bottom:1px solid #e5e7eb'>"
        f"<a href='{a.url}' style='color:#2563eb;font-weight:600;text-decoration:none'>{a.name}</a></td>"
        f"<td style='padding:14px 16px;border-bottom:1px solid #e5e7eb;text-align:right;font-weight:700;color:#111827'>"
        f"{_fmt(a.current_price, a.currency)}</td>"
        f"</tr>"
        for a in alerts
    )
    return (
        f"<tr><td style='padding:24px 40px 0'>"
        f"<h2 style='margin:0 0 12px;color:#15803d;font-size:18px'>🛒 Volvieron al stock</h2>"
        f"<table width='100%' cellpadding='0' cellspacing='0' style='border-collapse:collapse;border:1px solid #e5e7eb;border-radius:8px;overflow:hidden'>"
        f"<tbody>{filas}</tbody></table></td></tr>"
    )


def _html_resumen(
    drops: list[PriceDrop],
    grupo_drops: list[PriceDropGrupo] | None,
    restock_alerts: list[RestockAlert] | None,
) -> str:
    partes = []
    if drops:
        partes.append(f"<strong style='color:#1e40af'>{len(drops)} producto(s)</strong> con precio más bajo")
    if grupo_drops:
        partes.append(f"<strong style='color:#1e40af'>{len(grupo_drops)} grupo(s)</strong> con mejor precio disponible")
    if restock_alerts:
        partes.append(f"<strong style='color:#15803d'>{len(restock_alerts)} producto(s)</strong> volvieron al stock")
    return "Se detectaron " + " y ".join(partes) + " hoy." if partes else "Sin novedades de precio hoy."


def _html_fila_drop(d: PriceDrop, sparkline_b64: str | None) -> str:
    pct = -d.pct_vs_initial
    target_html = ""
    if d.hit_target:
        target_html = f"<br><span style='color:#16a34a;font-weight:600'>✅ Objetivo alcanzado ({_fmt(d.target_price, d.currency)})</span>"
    min_html = ""
    if d.is_historical_min:
        min_html = "<br><span style='color:#d97706;font-weight:600;font-size:12px'>🏆 Nuevo mínimo histórico</span>"
    elif d.min_price is not None:
        min_html = f"<br><span style='color:#6b7280;font-size:12px'>Mín: {_fmt(d.min_price, d.currency)}</span>"
    spark_html = ""
    if sparkline_b64:
        spark_html = f"<img src='data:image/png;base64,{sparkline_b64}' alt='historial' style='display:block;margin-top:6px;max-width:200px'>"
    return (
        f"<tr>"
        f"<td style='padding:16px;border-bottom:1px solid #e5e7eb'>"
        f"<a href='{d.url}' style='color:#2563eb;font-weight:600;text-decoration:none'>{d.name}</a>"
        f"{target_html}{min_html}{spark_html}</td>"
        f"<td style='padding:16px;border-bottom:1px solid #e5e7eb;text-align:right;color:#6b7280'>{_fmt(d.initial_price, d.currency)}</td>"
        f"<td style='padding:16px;border-bottom:1px solid #e5e7eb;text-align:right;color:#6b7280;text-decoration:line-through'>{_fmt(d.previous_price, d.currency)}</td>"
        f"<td style='padding:16px;border-bottom:1px solid #e5e7eb;text-align:right;font-weight:700;color:#111827;font-size:16px'>{_fmt(d.current_price, d.currency)}</td>"
        f"<td style='padding:16px;border-bottom:1px solid #e5e7eb;text-align:center'>"
        f"<span style='background:#dcfce7;color:#15803d;font-weight:700;padding:4px 10px;border-radius:999px;font-size:14px'>▼ {pct:.1f}%</span></td>"
        f"<td style='padding:16px;border-bottom:1px solid #e5e7eb;text-align:right;color:#15803d;font-weight:600'>{_fmt(-d.delta_vs_initial, d.currency)}</td>"
        f"</tr>"
    )


def build_html_email(
    drops: list[PriceDrop],
    run_date: date,
    grupo_drops: list[PriceDropGrupo] | None = None,
    restock_alerts: list[RestockAlert] | None = None,
    history_map: dict[str, list[tuple[str, float]]] | None = None,
) -> str:
    rows = ""
    for d in drops:
        history = (history_map or {}).get(d.product_id, [])
        spark = _sparkline_base64(history) if history else None
        rows += _html_fila_drop(d, spark)

    tabla_drops = ""
    if drops:
        tabla_drops = f"""
        <tr>
          <td style='padding:24px 40px'>
            <table width='100%' cellpadding='0' cellspacing='0' style='border-collapse:collapse;border:1px solid #e5e7eb;border-radius:8px;overflow:hidden'>
              <thead>
                <tr style='background:#f9fafb'>
                  <th style='padding:12px 16px;text-align:left;font-size:12px;color:#6b7280;text-transform:uppercase;letter-spacing:.05em;border-bottom:1px solid #e5e7eb'>Producto</th>
                  <th style='padding:12px 16px;text-align:right;font-size:12px;color:#6b7280;text-transform:uppercase;letter-spacing:.05em;border-bottom:1px solid #e5e7eb'>Inicial</th>
                  <th style='padding:12px 16px;text-align:right;font-size:12px;color:#6b7280;text-transform:uppercase;letter-spacing:.05em;border-bottom:1px solid #e5e7eb'>Anterior</th>
                  <th style='padding:12px 16px;text-align:right;font-size:12px;color:#6b7280;text-transform:uppercase;letter-spacing:.05em;border-bottom:1px solid #e5e7eb'>Precio actual</th>
                  <th style='padding:12px 16px;text-align:center;font-size:12px;color:#6b7280;text-transform:uppercase;letter-spacing:.05em;border-bottom:1px solid #e5e7eb'>Bajó</th>
                  <th style='padding:12px 16px;text-align:right;font-size:12px;color:#6b7280;text-transform:uppercase;letter-spacing:.05em;border-bottom:1px solid #e5e7eb'>Ahorro total</th>
                </tr>
              </thead>
              <tbody>{rows}</tbody>
            </table>
          </td>
        </tr>"""

    tabla_grupos = _html_tabla_grupos(grupo_drops) if grupo_drops else ""
    tabla_restocks = _html_tabla_restocks(restock_alerts) if restock_alerts else ""

    return f"""<!DOCTYPE html>
<html lang='es'>
<head><meta charset='UTF-8'><meta name='viewport' content='width=device-width,initial-scale=1'></head>
<body style='margin:0;padding:0;background:#f3f4f6;font-family:Arial,sans-serif'>
  <table width='100%' cellpadding='0' cellspacing='0' style='background:#f3f4f6;padding:32px 0'>
    <tr><td align='center'>
      <table width='640' cellpadding='0' cellspacing='0' style='background:#ffffff;border-radius:12px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.08)'>
        <tr>
          <td style='background:linear-gradient(135deg,#1e40af,#2563eb);padding:32px 40px;text-align:center'>
            <div style='font-size:32px;margin-bottom:8px'>💰</div>
            <h1 style='margin:0;color:#ffffff;font-size:24px;font-weight:700'>Bajadas de Precio</h1>
            <p style='margin:8px 0 0;color:#bfdbfe;font-size:14px'>{run_date.strftime("%d de %B de %Y")}</p>
          </td>
        </tr>
        <tr>
          <td style='padding:24px 40px 0'>
            <p style='margin:0;color:#374151;font-size:15px'>{_html_resumen(drops, grupo_drops, restock_alerts)}</p>
          </td>
        </tr>
        {tabla_drops}
        {tabla_grupos}
        {tabla_restocks}
        <tr>
          <td style='padding:0 40px 32px;text-align:center'>
            <p style='margin:0;font-size:12px;color:#9ca3af'>Generado por priceCompare • {run_date.isoformat()}</p>
          </td>
        </tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""


def save_report(markdown: str, reports_dir: Path, run_date: date) -> Path:
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / f"{run_date.isoformat()}.md"
    path.write_text(markdown, encoding="utf-8")
    return path
