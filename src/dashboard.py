"""Dashboard web local para priceCompare.

Lanzar con:  python -m src.dashboard
Luego abrir: http://127.0.0.1:8000
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

try:
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse, JSONResponse
    from pydantic import BaseModel
    import uvicorn
except ImportError:
    raise SystemExit(
        "Faltan dependencias del dashboard. Instalar con:\n"
        "  pip install fastapi uvicorn"
    )

from .config import DB_PATH, Product, load_products, save_products
from .extractors import validate_selector
from .storage import Storage

app = FastAPI(title="priceCompare dashboard", docs_url=None, redoc_url=None)
stalePriceHours = 48


def _valid_product_url(value: str) -> bool:
    try:
        parsed = urlsplit(value.strip())
        return parsed.scheme in ('http', 'https') and bool(parsed.hostname)
    except ValueError:
        return False


def _capture_details(storage: Storage, product_id: str) -> dict:
    """Presenta la antigüedad y el último resultado sin atribuir al precio la hora del navegador."""
    status = storage.capture_status(product_id)
    stale = False
    if status.last_success_at:
        captured = datetime.fromisoformat(status.last_success_at)
        if captured.tzinfo is None:
            captured = captured.replace(tzinfo=timezone.utc)
        stale = datetime.now(timezone.utc) - captured > timedelta(hours=stalePriceHours)
    return {
        'last_success_at': status.last_success_at,
        'last_attempt_at': status.last_attempt_at,
        'last_status': status.last_status,
        'last_error': status.last_error,
        'extraction_method': status.extraction_method,
        'is_stale': stale,
    }


def _storage() -> Storage:
    return Storage(DB_PATH)


def _target_efectivo(p: Product, initial_price: float) -> float | None:
    """Resuelve el target absoluto o relativo ('-20%'). Misma lógica que main._effective_target."""
    if p.target_price is not None:
        return p.target_price
    if p.target_price_relative is not None:
        try:
            pct = float(p.target_price_relative.replace("%", "").strip()) / 100.0
            return initial_price * (1.0 + pct)
        except ValueError:
            return None
    return None


def _parsear_target(valor) -> float | str | None:
    """Normaliza el target del formulario: número, cadena numérica, relativo '-20%'
    o vacío (sin objetivo). Lanza ValueError si no es interpretable."""
    if valor is None:
        return None
    if isinstance(valor, (int, float)):
        return float(valor)
    texto = str(valor).strip()
    if not texto:
        return None
    if "%" in texto:
        return texto
    try:
        return float(texto.replace(",", "."))
    except ValueError as exc:
        raise ValueError(f"target_price inválido: {valor!r}") from exc


@app.get("/api/products")
def api_products() -> JSONResponse:
    """Lista productos con products.json como fuente de verdad, enriquecidos con
    datos de la BD si existen. Un producto recién agregado (sin corrida del
    pipeline todavía) aparece como pendiente de primera captura."""
    storage = _storage()
    try:
        result = []
        for p in load_products():
            sp = storage.get_product(p.id)
            base = {
                "id": p.id,
                "name": p.name,
                "url": p.url,
                "currency": p.currency,
                "selector": p.selector,
                "grupo": p.grupo,
                "target_price_relative": p.target_price_relative,
                "active": p.active,
                **_capture_details(storage, p.id),
            }
            if sp is None:
                result.append({
                    **base,
                    "initial_price": None,
                    "last_price": None,
                    "min_price": None,
                    "min_price_date": None,
                    "precio_hace_30d": None,
                    "target_price": p.target_price,
                    "available": None,
                    "purchased": False,
                    "purchased_at": None,
                    "history": [],
                })
                continue
            min_info = storage.min_price(p.id)
            history = storage.price_history(p.id, limit=60)
            result.append({
                **base,
                "initial_price": sp.initial_price,
                "last_price": storage.last_price(p.id),
                "min_price": min_info[0] if min_info else None,
                "min_price_date": min_info[1] if min_info else None,
                "precio_hace_30d": storage.precio_hace_dias(p.id, 30),
                "target_price": _target_efectivo(p, sp.initial_price),
                "available": storage.last_availability(p.id),
                "purchased": sp.purchased,
                "purchased_at": sp.purchased_at,
                # Un comprado no se consulta: su última captura envejece a propósito.
                "is_stale": base["is_stale"] and not sp.purchased,
                "history": [{"date": d[:10], "price": pr} for d, pr in history],
            })
    finally:
        storage.close()
    return JSONResponse(result)


@app.get("/api/products/{product_id}/history")
def api_history(product_id: str) -> JSONResponse:
    storage = _storage()
    try:
        history = storage.price_history(product_id, limit=365)
    finally:
        storage.close()
    return JSONResponse([{"date": d, "price": p} for d, p in history])


@app.get("/api/products/{product_id}/changes")
def api_changes(product_id: str) -> JSONResponse:
    """Histórico compacto: solo los días en que el precio cambió."""
    storage = _storage()
    try:
        product = storage.get_product(product_id)
        changes = storage.price_changes(product_id)
    finally:
        storage.close()
    if product is None:
        return JSONResponse({"error": "producto no encontrado"}, status_code=404)
    return JSONResponse({
        "id": product.id,
        "name": product.name,
        "currency": product.currency,
        "changes": [{"date": d, "price": p} for d, p in changes],
    })


class PurchasedPayload(BaseModel):
    purchased: bool


@app.post("/api/products/{product_id}/purchased")
def api_set_purchased(product_id: str, payload: PurchasedPayload) -> JSONResponse:
    storage = _storage()
    try:
        ok = storage.set_purchased(product_id, payload.purchased)
    finally:
        storage.close()
    if not ok:
        return JSONResponse({"error": "producto no encontrado"}, status_code=404)
    return JSONResponse({"id": product_id, "purchased": payload.purchased})


class ProductCreatePayload(BaseModel):
    id: str
    name: str
    url: str
    selector: str | None = None
    target_price: float | str | None = None
    currency: str = "COP"
    grupo: str | None = None


class ProductUpdatePayload(BaseModel):
    name: str
    url: str
    selector: str | None = None
    target_price: float | str | None = None
    currency: str = "COP"
    grupo: str | None = None


class ActivePayload(BaseModel):
    active: bool


@app.post("/api/products")
def api_create_product(payload: ProductCreatePayload) -> JSONResponse:
    """Agrega un producto a products.json. NO toca la BD: initial_price y el
    historial se crean en la próxima corrida del pipeline (invariante del diseño)."""
    pid = payload.id.strip()
    if not pid:
        return JSONResponse({"error": "el id no puede estar vacío"}, status_code=400)
    if not _valid_product_url(payload.url):
        return JSONResponse({"error": "la URL debe empezar con http(s)"}, status_code=400)
    products = load_products()
    if any(p.id == pid for p in products):
        return JSONResponse({"error": f"ya existe un producto con id '{pid}'"}, status_code=409)
    try:
        validate_selector(payload.selector)
        target = _parsear_target(payload.target_price)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    raw = {
        "id": pid,
        "name": payload.name.strip(),
        "url": payload.url.strip(),
        "selector": (payload.selector or "").strip() or None,
        "target_price": target,
        "currency": payload.currency.strip().upper() or "COP",
        "active": True,
    }
    grupo = (payload.grupo or "").strip()
    if grupo:
        raw["grupo"] = grupo
    products.append(Product.from_dict(raw))
    save_products(products)
    return JSONResponse({"id": pid, "mensaje": "producto agregado; se capturará en la próxima corrida"}, status_code=201)


@app.put("/api/products/{product_id}")
def api_update_product(product_id: str, payload: ProductUpdatePayload) -> JSONResponse:
    """Edita un producto en products.json y sincroniza la metadata en la BD.
    El id y el initial_price no son editables."""
    if not _valid_product_url(payload.url):
        return JSONResponse({"error": "la URL debe empezar con http(s)"}, status_code=400)
    products = load_products()
    prod = next((p for p in products if p.id == product_id), None)
    if prod is None:
        return JSONResponse({"error": "producto no encontrado"}, status_code=404)
    try:
        validate_selector(payload.selector)
        target = _parsear_target(payload.target_price)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    prod.name = payload.name.strip()
    prod.url = payload.url.strip()
    prod.selector = (payload.selector or "").strip() or None
    prod.currency = payload.currency.strip().upper() or prod.currency
    prod.grupo = (payload.grupo or "").strip() or None
    if isinstance(target, str):
        prod.target_price, prod.target_price_relative = None, target
    else:
        prod.target_price, prod.target_price_relative = target, None
    save_products(products)

    storage = _storage()
    try:
        sp = storage.get_product(product_id)
        if sp is not None:
            storage.update_product_metadata(
                product_id, prod.name, prod.url, prod.currency,
                _target_efectivo(prod, sp.initial_price),
            )
    finally:
        storage.close()
    return JSONResponse({"id": product_id, "mensaje": "producto actualizado"})


@app.patch("/api/products/{product_id}/active")
def api_toggle_active(product_id: str, payload: ActivePayload) -> JSONResponse:
    products = load_products()
    prod = next((p for p in products if p.id == product_id), None)
    if prod is None:
        return JSONResponse({"error": "producto no encontrado"}, status_code=404)
    prod.active = payload.active
    save_products(products)
    return JSONResponse({"id": product_id, "active": payload.active})


@app.delete("/api/products/{product_id}")
def api_delete_product(product_id: str, purgar_historial: bool = False) -> JSONResponse:
    """Quita el producto de products.json. Por defecto conserva el historial en la
    BD (quedará como huérfano purgable); con purgar_historial=true lo borra todo."""
    products = load_products()
    prod = next((p for p in products if p.id == product_id), None)
    if prod is None:
        return JSONResponse({"error": "producto no encontrado"}, status_code=404)
    save_products([p for p in products if p.id != product_id])
    purgado = False
    if purgar_historial:
        storage = _storage()
        try:
            purgado = storage.purge_product(product_id)
        finally:
            storage.close()
    return JSONResponse({"id": product_id, "eliminado": True, "historial_purgado": purgado})


@app.get("/api/huerfanos")
def api_huerfanos() -> JSONResponse:
    """Productos que existen en la BD pero ya no están en products.json."""
    ids_json = {p.id for p in load_products()}
    storage = _storage()
    try:
        result = []
        for sp in storage.all_products():
            if sp.id in ids_json:
                continue
            registros, ultima = storage.resumen_historial(sp.id)
            result.append({
                "id": sp.id,
                "name": sp.name,
                "currency": sp.currency,
                "registros": registros,
                "ultima_captura": ultima[:10] if ultima else None,
            })
    finally:
        storage.close()
    return JSONResponse(result)


@app.delete("/api/huerfanos/{product_id}")
def api_purgar_huerfano(product_id: str) -> JSONResponse:
    """Purga un huérfano de la BD. Se niega si el producto sigue en products.json."""
    if any(p.id == product_id for p in load_products()):
        return JSONResponse(
            {"error": "el producto sigue en products.json; elimínalo desde su tarjeta"},
            status_code=409,
        )
    storage = _storage()
    try:
        ok = storage.purge_product(product_id)
    finally:
        storage.close()
    if not ok:
        return JSONResponse({"error": "producto no encontrado en la BD"}, status_code=404)
    return JSONResponse({"id": product_id, "purgado": True})


@app.get("/api/estadisticas")
def api_estadisticas() -> JSONResponse:
    """Tendencias globales: top bajadas de los últimos 30 días y productos
    más cerca de su precio objetivo (excluye comprados y los que ya lo alcanzaron)."""
    storage = _storage()
    try:
        bajadas = []
        cerca = []
        for p in load_products():
            sp = storage.get_product(p.id)
            if sp is None or sp.purchased:
                continue
            last = storage.last_price(p.id)
            if last is None:
                continue
            hace_30d = storage.precio_hace_dias(p.id, 30)
            if hace_30d and hace_30d > 0 and last < hace_30d:
                bajadas.append({
                    "id": p.id, "name": p.name, "currency": p.currency,
                    "precio_hace_30d": hace_30d, "last_price": last,
                    "pct": (last - hace_30d) / hace_30d * 100,
                })
            target = _target_efectivo(p, sp.initial_price)
            if target and target > 0 and last > target:
                cerca.append({
                    "id": p.id, "name": p.name, "currency": p.currency,
                    "last_price": last, "target_price": target,
                    "pct_sobre_target": (last - target) / target * 100,
                })
    finally:
        storage.close()
    bajadas.sort(key=lambda x: x["pct"])
    cerca.sort(key=lambda x: x["pct_sobre_target"])
    return JSONResponse({"top_bajadas_30d": bajadas[:5], "cerca_del_target": cerca[:5]})


@app.get("/", response_class=HTMLResponse)
def dashboard() -> HTMLResponse:
    return HTMLResponse(_DASHBOARD_HTML)


_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>priceCompare — Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: system-ui, -apple-system, sans-serif; background: #f1f5f9; color: #1e293b; }
  header { background: linear-gradient(135deg,#1e40af,#2563eb); color: #fff; padding: 20px 32px; display: flex; align-items: center; gap: 12px; }
  header h1 { font-size: 22px; font-weight: 700; }
  header span { font-size: 13px; opacity: .75; margin-left: auto; }
  #summary { display: flex; gap: 16px; padding: 24px 32px 8px; flex-wrap: wrap; }
  .stat { background: #fff; border-radius: 10px; padding: 16px 24px; min-width: 140px; box-shadow: 0 1px 4px rgba(0,0,0,.07); }
  .stat .val { font-size: 28px; font-weight: 700; color: #2563eb; }
  .stat .lbl { font-size: 12px; color: #64748b; margin-top: 2px; }
  #filters { padding: 8px 32px 16px; display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
  .pill { border: 1.5px solid #cbd5e1; border-radius: 999px; padding: 5px 14px; font-size: 13px; cursor: pointer; background: #fff; color: #475569; transition: .15s; }
  .pill.active { background: #2563eb; border-color: #2563eb; color: #fff; }
  #search { margin-left: auto; border: 1.5px solid #cbd5e1; border-radius: 8px; padding: 6px 12px; font-size: 13px; width: 200px; outline: none; }
  #search:focus { border-color: #2563eb; }
  #grid { display: grid; grid-template-columns: repeat(auto-fill,minmax(320px,1fr)); gap: 20px; padding: 0 32px 40px; }
  .card { background: #fff; border-radius: 12px; box-shadow: 0 1px 4px rgba(0,0,0,.07); overflow: hidden; display: flex; flex-direction: column; }
  .card-head { padding: 16px 18px 10px; }
  .card-head .name { font-weight: 600; font-size: 14px; color: #1e293b; text-decoration: none; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
  .card-head .name:hover { color: #2563eb; }
  .badges { display: flex; gap: 6px; margin-top: 8px; flex-wrap: wrap; }
  .badge { border-radius: 999px; padding: 2px 9px; font-size: 11px; font-weight: 600; }
  .badge-drop  { background: #dcfce7; color: #15803d; }
  .badge-up    { background: #fef3c7; color: #92400e; }
  .badge-equal { background: #f1f5f9; color: #64748b; }
  .badge-target{ background: #eff6ff; color: #2563eb; }
  .badge-hit   { background: #dcfce7; color: #15803d; border: 1px solid #86efac; }
  .badge-out   { background: #fee2e2; color: #991b1b; }
  .badge-in    { background: #d1fae5; color: #065f46; }
  .badge-min   { background: #fef9c3; color: #854d0e; }
  .badge-bought{ background: #ede9fe; color: #6d28d9; }
  .card.bought { opacity: .7; }
  .price-row   { display: flex; align-items: baseline; gap: 8px; padding: 6px 18px 0; }
  .price-now   { font-size: 24px; font-weight: 700; color: #111827; }
  .price-init  { font-size: 13px; color: #94a3b8; text-decoration: line-through; }
  .price-pct   { font-size: 13px; font-weight: 600; }
  .price-pct.down { color: #16a34a; }
  .price-pct.up   { color: #dc2626; }
  .meta-row { padding: 4px 18px 10px; font-size: 12px; color: #64748b; display: flex; gap: 16px; flex-wrap: wrap; }
  .chart-wrap { padding: 0 12px 12px; height: 90px; }
  .card-foot { border-top: 1px solid #f1f5f9; padding: 10px 18px; display: flex; gap: 10px; }
  .card-foot a { font-size: 12px; color: #2563eb; text-decoration: none; }
  .card-foot a:hover { text-decoration: underline; }
  .empty { grid-column: 1/-1; text-align: center; color: #94a3b8; padding: 60px; font-size: 15px; }
  .link-btn { font-size: 12px; color: #2563eb; background: none; border: none; padding: 0; cursor: pointer; font-family: inherit; }
  .link-btn:hover { text-decoration: underline; }
  #modal, #modal-form { position: fixed; inset: 0; background: rgba(15,23,42,.55); display: none; align-items: center; justify-content: center; z-index: 50; padding: 20px; }
  #modal.open, #modal-form.open { display: flex; }
  .modal-box { background: #fff; border-radius: 12px; max-width: 520px; width: 100%; max-height: 80vh; display: flex; flex-direction: column; overflow: hidden; box-shadow: 0 10px 40px rgba(0,0,0,.2); }
  .modal-head { padding: 16px 20px; border-bottom: 1px solid #e2e8f0; display: flex; align-items: center; gap: 10px; }
  .modal-head h2 { font-size: 16px; font-weight: 600; flex: 1; }
  .modal-head button { background: none; border: none; font-size: 22px; cursor: pointer; color: #64748b; line-height: 1; }
  .modal-body { padding: 8px 20px 20px; overflow-y: auto; }
  .modal-body table { width: 100%; border-collapse: collapse; font-size: 14px; }
  .modal-body th, .modal-body td { padding: 8px 6px; text-align: left; border-bottom: 1px solid #f1f5f9; }
  .modal-body th { font-size: 12px; color: #64748b; font-weight: 600; text-transform: uppercase; }
  .modal-body td.price { font-weight: 600; font-family: ui-monospace, monospace; text-align: right; }
  .modal-body td.delta { text-align: right; font-size: 12px; font-weight: 600; font-family: ui-monospace, monospace; }
  .modal-body td.delta.down { color: #16a34a; }
  .modal-body td.delta.up   { color: #dc2626; }
  .modal-body .modal-empty { text-align: center; color: #94a3b8; padding: 30px 10px; }
  .form-row { margin-bottom: 12px; }
  .form-row label { display: block; font-size: 12px; color: #64748b; font-weight: 600; margin-bottom: 4px; }
  .form-row input, .form-row select { width: 100%; border: 1.5px solid #cbd5e1; border-radius: 8px; padding: 7px 10px; font-size: 14px; outline: none; font-family: inherit; }
  .form-row input:focus, .form-row select:focus { border-color: #2563eb; }
  .form-row input[readonly] { background: #f1f5f9; color: #94a3b8; }
  .form-row .hint { font-size: 11px; color: #94a3b8; margin-top: 3px; }
  .form-actions { display: flex; justify-content: flex-end; gap: 10px; margin-top: 16px; }
  .btn-primary { background: #2563eb; color: #fff; border: none; border-radius: 8px; padding: 8px 18px; font-size: 14px; font-weight: 600; cursor: pointer; }
  .btn-primary:hover { background: #1e40af; }
  #tendencias { display: flex; gap: 16px; padding: 8px 32px 0; flex-wrap: wrap; }
  .trend-panel { background: #fff; border-radius: 10px; padding: 14px 18px; box-shadow: 0 1px 4px rgba(0,0,0,.07); flex: 1; min-width: 300px; }
  .trend-panel h3 { font-size: 13px; color: #64748b; font-weight: 600; margin-bottom: 8px; }
  .trend-panel table { width: 100%; border-collapse: collapse; font-size: 13px; }
  .trend-panel td { padding: 5px 4px; border-bottom: 1px solid #f1f5f9; }
  .trend-panel td.price { text-align: right; font-family: ui-monospace, monospace; font-weight: 600; white-space: nowrap; }
  .trend-panel td.delta { text-align: right; font-weight: 600; font-family: ui-monospace, monospace; }
  .trend-panel td.delta.down { color: #16a34a; }
  .trend-panel td.delta.up { color: #dc2626; }
  .trend-panel a { color: #1e293b; text-decoration: none; }
  .trend-panel a:hover { color: #2563eb; }
  #huerfanos { padding: 0 32px 40px; }
  #huerfanos details { background: #fff; border-radius: 10px; padding: 12px 18px; box-shadow: 0 1px 4px rgba(0,0,0,.07); font-size: 13px; }
  #huerfanos summary { cursor: pointer; font-weight: 600; color: #64748b; }
  #huerfanos table { width: 100%; border-collapse: collapse; margin-top: 10px; }
  #huerfanos td { padding: 6px 4px; border-bottom: 1px solid #f1f5f9; color: #475569; }
  @media(max-width:600px){ #grid{padding:0 12px 32px} header{padding:16px} #summary{padding:16px 12px 0} #filters{padding:8px 12px 12px} #tendencias{padding:8px 12px 0} #huerfanos{padding:0 12px 32px} }
</style>
</head>
<body>
<header>
  <span style="font-size:22px">💰</span>
  <h1>priceCompare</h1>
  <span id="last-update"></span>
</header>
<div id="summary">
  <div class="stat"><div class="val" id="s-total">—</div><div class="lbl">productos</div></div>
  <div class="stat"><div class="val" id="s-drops" style="color:#16a34a">—</div><div class="lbl">por debajo del inicial</div></div>
  <div class="stat"><div class="val" id="s-target" style="color:#2563eb">—</div><div class="lbl">alcanzaron el objetivo</div></div>
  <div class="stat"><div class="val" id="s-out" style="color:#dc2626">—</div><div class="lbl">sin stock</div></div>
  <div class="stat"><div class="val" id="s-min" style="color:#d97706">—</div><div class="lbl">en mínimo histórico</div></div>
</div>
<div id="tendencias" style="display:none"></div>
<div id="filters">
  <button class="pill active" data-f="all">Todos</button>
  <button class="pill" data-f="drops">Bajadas</button>
  <button class="pill" data-f="target">En objetivo</button>
  <button class="pill" data-f="out">Sin stock</button>
  <button class="pill" data-f="inactive">Inactivos</button>
  <button class="pill" data-f="bought">Comprados</button>
  <button class="pill" id="btn-add" style="border-color:#2563eb;color:#2563eb" onclick="openForm()">➕ Agregar producto</button>
  <input id="search" type="search" placeholder="Buscar producto…">
</div>
<div id="grid"><div class="empty">Cargando…</div></div>
<div id="huerfanos" style="display:none"></div>

<div id="modal" onclick="if(event.target.id==='modal') closeHistory()">
  <div class="modal-box">
    <div class="modal-head">
      <h2 id="modal-title">Histórico de precios</h2>
      <button onclick="closeHistory()" aria-label="Cerrar">×</button>
    </div>
    <div class="modal-body" id="modal-body">Cargando…</div>
  </div>
</div>

<div id="modal-form" onclick="if(event.target.id==='modal-form') closeForm()">
  <div class="modal-box">
    <div class="modal-head">
      <h2 id="form-title">Agregar producto</h2>
      <button onclick="closeForm()" aria-label="Cerrar">×</button>
    </div>
    <div class="modal-body">
      <form id="product-form" onsubmit="submitForm(event)">
        <div class="form-row">
          <label for="f-id">ID</label>
          <input id="f-id" required pattern="[A-Za-z0-9_-]+" placeholder="ej: sony-wh1000xm6">
          <div class="hint">Solo letras, números, guiones. No se puede cambiar después.</div>
        </div>
        <div class="form-row">
          <label for="f-name">Nombre</label>
          <input id="f-name" required placeholder="Nombre del producto">
        </div>
        <div class="form-row">
          <label for="f-url">URL</label>
          <input id="f-url" required type="url" placeholder="https://…">
        </div>
        <div class="form-row">
          <label for="f-selector">Selector CSS (opcional)</label>
          <input id="f-selector" placeholder="ej: .price-now">
          <div class="hint">Solo si la extracción automática falla para este sitio.</div>
        </div>
        <div class="form-row">
          <label for="f-target">Precio objetivo (opcional)</label>
          <input id="f-target" placeholder="ej: 1500000 o -20%">
          <div class="hint">Absoluto (1500000) o relativo al inicial (-20%).</div>
        </div>
        <div class="form-row">
          <label for="f-currency">Moneda</label>
          <select id="f-currency">
            <option value="COP">COP</option>
            <option value="USD">USD</option>
            <option value="EUR">EUR</option>
          </select>
        </div>
        <div class="form-row">
          <label for="f-grupo">Grupo (opcional)</label>
          <input id="f-grupo" placeholder="Para comparar el mismo producto en varios sitios">
        </div>
        <div class="form-actions">
          <button type="button" class="pill" onclick="closeForm()">Cancelar</button>
          <button type="submit" class="btn-primary">Guardar</button>
        </div>
      </form>
    </div>
  </div>
</div>

<script>
let allProducts = [];
let charts = Object.create(null);
let activeFilter = 'all';

const _formatters = Object.create(null);
const captureStatusLabels = {success: 'Captura exitosa', error: 'Falló la consulta', rejected: 'Precio rechazado'};
const chartIds = new Map();

/**
 * Escapa contenido dinámico antes de insertarlo en HTML o atributos.
 * @param {unknown} value Contenido recibido.
 * @returns {string}
 */
function escapeHtml(value) {
  const element = document.createElement('span');
  element.textContent = String(value ?? '');
  return element.innerHTML.replaceAll('"', '&quot;').replaceAll("'", '&#39;');
}

/**
 * Restringe los enlaces de productos a páginas HTTP o HTTPS.
 * @param {string} value URL recibida.
 * @returns {string}
 */
function productHref(value) {
  try {
    const url = new URL(value);
    return ['http:', 'https:'].includes(url.protocol) ? escapeHtml(url.href) : '#';
  } catch {
    return '#';
  }
}

/**
 * Asigna un identificador único al gráfico sin interpolar datos del producto.
 * @param {string} productId Identificador del producto.
 * @returns {string}
 */
function chartId(productId) {
  if (!chartIds.has(productId)) chartIds.set(productId, 'chart-' + chartIds.size);
  return chartIds.get(productId);
}

/**
 * Presenta una fecha de captura en la zona horaria del navegador.
 * @param {string|null} value Fecha de captura.
 * @returns {string}
 */
function captureDate(value) {
  if (!value) return 'Sin captura';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? 'Fecha desconocida' : date.toLocaleString('es-CO');
}
function fmt(val, currency) {
  if (val == null) return '—';
  const cur = currency || 'COP';
  if (!_formatters[cur]) {
    try {
      _formatters[cur] = new Intl.NumberFormat(
        cur === 'COP' ? 'es-CO' : 'en-US',
        { style: 'currency', currency: cur, maximumFractionDigits: cur === 'COP' ? 0 : 2 }
      );
    } catch (e) {
      _formatters[cur] = { format: value => escapeHtml(cur) + ' ' + value.toLocaleString('es-CO') };
    }
  }
  return _formatters[cur].format(val);
}
function fmtNum(val, currency) {
  const cur = currency || 'COP';
  return val.toLocaleString(cur === 'COP' ? 'es-CO' : 'en-US',
    { maximumFractionDigits: cur === 'COP' ? 0 : 2 });
}
function pct(current, ref) {
  if (!ref) return null;
  return ((current - ref) / ref * 100).toFixed(1);
}

function buildCard(p) {
  const last = p.last_price;
  const init = p.initial_price;
  const diff = last != null ? pct(last, init) : null;
  const isDown = diff !== null && parseFloat(diff) < 0;
  const isUp   = diff !== null && parseFloat(diff) > 0;
  const hitTarget = p.target_price != null && last != null && last <= p.target_price;
  const isMin = p.min_price != null && last != null && last <= p.min_price;

  let badges = '';
  if (p.initial_price == null) badges += '<span class="badge badge-equal">⏳ Pendiente de captura</span>';
  if (p.purchased) badges += '<span class="badge badge-bought">🛍️ Comprado</span>';
  if (!p.active) badges += '<span class="badge badge-out">Inactivo</span>';
  if (p.available === false) badges += '<span class="badge badge-out">Sin stock</span>';
  if (p.available === true)  badges += '<span class="badge badge-in">En stock</span>';
  if (p.is_stale) badges += '<span class="badge badge-out">Precio desactualizado</span>';
  if (p.last_status === 'error') badges += '<span class="badge badge-out">Falló la consulta</span>';
  if (p.last_status === 'rejected') badges += '<span class="badge badge-out">Precio rechazado</span>';
  if (isDown) badges += `<span class="badge badge-drop">▼ ${Math.abs(diff)}%</span>`;
  if (isUp)   badges += `<span class="badge badge-up">▲ ${diff}%</span>`;
  if (hitTarget) badges += '<span class="badge badge-hit">✅ Objetivo</span>';
  if (isMin)  badges += '<span class="badge badge-min">🏆 Mínimo histórico</span>';

  const pctClass = isDown ? 'down' : isUp ? 'up' : '';
  const pctStr = diff !== null ? `${parseFloat(diff) > 0 ? '+' : ''}${diff}%` : '';

  let metaRows = '';
  metaRows += `<span>Última captura: ${escapeHtml(captureDate(p.last_success_at))}</span>`;
  if (p.last_attempt_at) {
    metaRows += `<span>Último intento: ${escapeHtml(captureDate(p.last_attempt_at))} · ${escapeHtml(captureStatusLabels[p.last_status] || 'Resultado desconocido')}</span>`;
  }
  if (p.last_error) metaRows += `<span>${escapeHtml(p.last_error)}</span>`;
  if (p.min_price != null) {
    const minDate = p.min_price_date ? ' (' + p.min_price_date.slice(0,10) + ')' : '';
    metaRows += `<span>Mín: ${fmt(p.min_price, p.currency)}${escapeHtml(minDate)}</span>`;
  }
  if (p.target_price != null) {
    metaRows += `<span>Objetivo: ${fmt(p.target_price, p.currency)}</span>`;
  }
  if (p.min_price != null && last != null && last > p.min_price) {
    metaRows += `<span>vs mín: +${pct(last, p.min_price)}%</span>`;
  }
  if (p.precio_hace_30d != null && last != null && last !== p.precio_hace_30d) {
    const t30 = pct(last, p.precio_hace_30d);
    const flecha = parseFloat(t30) < 0 ? '▼' : '▲';
    metaRows += `<span>30d: ${flecha} ${Math.abs(t30)}%</span>`;
  }

  const sparkId = chartId(p.id);

  return `
  <div class="card ${p.purchased ? 'bought' : ''}" data-id="${escapeHtml(p.id)}" data-active="${!!p.active}" data-down="${isDown}" data-target="${hitTarget}" data-out="${p.available===false}" data-bought="${!!p.purchased}">
    <div class="card-head">
      <a class="name" href="${productHref(p.url)}" target="_blank" rel="noopener">${escapeHtml(p.name)}</a>
      <div class="badges">${badges || '<span class="badge badge-equal">Sin cambio</span>'}</div>
    </div>
    <div class="price-row">
      <span class="price-now">${fmt(last, p.currency)}</span>
      <span class="price-init">${fmt(init, p.currency)}</span>
      ${pctStr ? `<span class="price-pct ${pctClass}">${pctStr}</span>` : ''}
    </div>
    ${metaRows ? `<div class="meta-row">${metaRows}</div>` : ''}
    <div class="chart-wrap"><canvas id="${sparkId}"></canvas></div>
    <div class="card-foot">
      <a href="${productHref(p.url)}" target="_blank" rel="noopener">Ver producto →</a>
      <button class="link-btn" data-action="history">Histórico</button>
      <button class="link-btn" style="margin-left:auto" title="Editar" data-action="edit">✏️</button>
      <button class="link-btn" title="${p.active ? 'Pausar monitoreo' : 'Reactivar monitoreo'}" data-action="active">${p.active ? '⏸️' : '▶️'}</button>
      <button class="link-btn" title="Eliminar" data-action="delete">🗑️</button>
      <button class="link-btn" title="${p.purchased ? 'Desmarcar comprado' : 'Marcar comprado'}" data-action="purchased">${p.purchased ? '↩️' : '🛍️'}</button>
    </div>
  </div>`;
}

function renderChart(p) {
  const sparkId = chartId(p.id);
  const canvas = document.getElementById(sparkId);
  if (!canvas || !p.history || p.history.length < 2) return;
  if (charts[p.id]) { charts[p.id].destroy(); }
  const labels = p.history.map(h => h.date);
  const data   = p.history.map(h => h.price);
  const isDown = p.last_price != null && p.last_price < p.initial_price;
  const color  = isDown ? '#16a34a' : '#2563eb';
  charts[p.id] = new Chart(canvas, {
    type: 'line',
    data: {
      labels,
      datasets: [{
        data,
        borderColor: color,
        borderWidth: 1.8,
        fill: true,
        backgroundColor: color + '18',
        pointRadius: 0,
        tension: 0.3,
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      plugins: { legend: { display: false }, tooltip: {
        callbacks: { label: ctx => fmt(ctx.parsed.y, p.currency) }
      }},
      scales: {
        x: { display: false },
        y: { display: false },
      }
    }
  });
}

function applyFilters() {
  const q = document.getElementById('search').value.toLowerCase();
  let visible = 0;
  document.querySelectorAll('.card').forEach(card => {
    const id   = card.dataset.id;
    const p    = allProducts.find(x => x.id === id);
    const name = (p?.name || '').toLowerCase();
    const matchQ = !q || name.includes(q) || id.includes(q);
    let matchF = true;
    if (activeFilter === 'all')      matchF = card.dataset.bought !== 'true';
    if (activeFilter === 'drops')    matchF = card.dataset.down === 'true' && card.dataset.out !== 'true' && card.dataset.bought !== 'true';
    if (activeFilter === 'target')   matchF = card.dataset.target === 'true' && card.dataset.bought !== 'true';
    if (activeFilter === 'out')      matchF = card.dataset.out === 'true' && card.dataset.bought !== 'true';
    if (activeFilter === 'inactive') matchF = card.dataset.active === 'false';
    if (activeFilter === 'bought')   matchF = card.dataset.bought === 'true';
    const show = matchQ && matchF;
    card.style.display = show ? '' : 'none';
    if (show) visible++;
  });
  if (visible === 0) {
    let empty = document.querySelector('.empty');
    if (!empty) {
      empty = document.createElement('div');
      empty.className = 'empty';
      document.getElementById('grid').appendChild(empty);
    }
    empty.textContent = 'Sin resultados.';
    empty.style.display = '';
  } else {
    const empty = document.querySelector('.empty');
    if (empty) empty.style.display = 'none';
  }
}

async function load() {
  try {
    const res = await fetch('/api/products');
    allProducts = await res.json();
  } catch(e) {
    document.getElementById('grid').innerHTML = '<div class="empty">Error cargando datos.</div>';
    return;
  }

  // Summary
  const total  = allProducts.length;
  const drops  = allProducts.filter(p => p.last_price != null && p.last_price < p.initial_price).length;
  const target = allProducts.filter(p => p.target_price != null && p.last_price != null && p.last_price <= p.target_price).length;
  const out    = allProducts.filter(p => p.available === false).length;
  const mins   = allProducts.filter(p => p.min_price != null && p.last_price != null && p.last_price <= p.min_price).length;
  document.getElementById('s-total').textContent = total;
  document.getElementById('s-drops').textContent = drops;
  document.getElementById('s-target').textContent = target;
  document.getElementById('s-out').textContent = out;
  document.getElementById('s-min').textContent = mins;
  const captures = allProducts.map(product => product.last_success_at).filter(Boolean).sort();
  document.getElementById('last-update').textContent = captures.length
    ? 'Captura más reciente: ' + captureDate(captures[captures.length - 1])
    : 'Sin capturas de precios';

  // Ordenar: primero bajadas, luego sin stock, luego el resto
  allProducts.sort((a, b) => {
    const score = p => {
      if (p.last_price != null && p.last_price < p.initial_price) return 0;
      if (p.available === false) return 1;
      return 2;
    };
    return score(a) - score(b);
  });

  const grid = document.getElementById('grid');
  grid.innerHTML = allProducts.map(buildCard).join('');
  allProducts.forEach(renderChart);
  applyFilters();
  loadTendencias();
  loadHuerfanos();
}

async function loadTendencias() {
  const cont = document.getElementById('tendencias');
  try {
    const res = await fetch('/api/estadisticas');
    if (!res.ok) throw new Error('error');
    const d = await res.json();
    let html = '';
    if (d.top_bajadas_30d.length) {
      html += '<div class="trend-panel"><h3>📉 Top bajadas del mes</h3><table>' +
        d.top_bajadas_30d.map(x =>
          `<tr><td><a href="#" data-action="history" data-id="${escapeHtml(x.id)}">${escapeHtml(x.name)}</a></td>` +
          `<td class="price">${fmt(x.last_price, x.currency)}</td>` +
          `<td class="delta down">${x.pct.toFixed(1)}%</td></tr>`).join('') +
        '</table></div>';
    }
    if (d.cerca_del_target.length) {
      html += '<div class="trend-panel"><h3>🎯 Más cerca del objetivo</h3><table>' +
        d.cerca_del_target.map(x =>
          `<tr><td><a href="#" data-action="history" data-id="${escapeHtml(x.id)}">${escapeHtml(x.name)}</a></td>` +
          `<td class="price">${fmt(x.last_price, x.currency)} / ${fmt(x.target_price, x.currency)}</td>` +
          `<td class="delta up">+${x.pct_sobre_target.toFixed(1)}%</td></tr>`).join('') +
        '</table></div>';
    }
    cont.innerHTML = html;
    cont.style.display = html ? '' : 'none';
  } catch (e) {
    cont.style.display = 'none';
  }
}

async function loadHuerfanos() {
  const cont = document.getElementById('huerfanos');
  try {
    const res = await fetch('/api/huerfanos');
    if (!res.ok) throw new Error('error');
    const items = await res.json();
    if (!items.length) { cont.style.display = 'none'; return; }
    cont.innerHTML = `<details><summary>🗃️ Huérfanos en la BD (${items.length}) — productos con historial que ya no están en products.json</summary><table>` +
      items.map(h =>
        `<tr><td>${escapeHtml(h.name)}</td><td>${escapeHtml(h.registros)} registro(s)</td>` +
        `<td>última captura: ${escapeHtml(h.ultima_captura || '—')}</td>` +
        `<td style="text-align:right"><button class="link-btn" data-action="purge" data-id="${escapeHtml(h.id)}">Purgar</button></td></tr>`).join('') +
      '</table></details>';
    cont.style.display = '';
  } catch (e) {
    cont.style.display = 'none';
  }
}

async function purgarHuerfano(id) {
  if (!confirm('¿Borrar definitivamente este producto y todo su historial de la base de datos?')) return;
  try {
    const res = await fetch('/api/huerfanos/' + encodeURIComponent(id), {method: 'DELETE'});
    if (!res.ok) throw new Error('error');
    loadHuerfanos();
  } catch (e) {
    alert('Error purgando el huérfano.');
  }
}

let editingId = null;

function openForm(id) {
  editingId = id || null;
  const p = id ? allProducts.find(x => x.id === id) : null;
  document.getElementById('form-title').textContent = p ? 'Editar producto' : 'Agregar producto';
  const fId = document.getElementById('f-id');
  fId.value = p ? p.id : '';
  fId.readOnly = !!p;
  fId.required = !p;
  document.getElementById('f-name').value = p ? p.name : '';
  document.getElementById('f-url').value = p ? p.url : '';
  document.getElementById('f-selector').value = (p && p.selector) || '';
  document.getElementById('f-target').value = p ? (p.target_price_relative ?? p.target_price ?? '') : '';
  document.getElementById('f-currency').value = p ? p.currency : 'COP';
  document.getElementById('f-grupo').value = (p && p.grupo) || '';
  document.getElementById('modal-form').classList.add('open');
  (p ? document.getElementById('f-name') : fId).focus();
}

function closeForm() {
  document.getElementById('modal-form').classList.remove('open');
  editingId = null;
}

async function submitForm(ev) {
  ev.preventDefault();
  const body = {
    name: document.getElementById('f-name').value.trim(),
    url: document.getElementById('f-url').value.trim(),
    selector: document.getElementById('f-selector').value.trim() || null,
    target_price: document.getElementById('f-target').value.trim() || null,
    currency: document.getElementById('f-currency').value,
    grupo: document.getElementById('f-grupo').value.trim() || null,
  };
  let url = '/api/products', method = 'POST';
  if (editingId) {
    url += '/' + encodeURIComponent(editingId);
    method = 'PUT';
  } else {
    body.id = document.getElementById('f-id').value.trim();
  }
  try {
    const res = await fetch(url, {
      method,
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      const d = await res.json().catch(() => ({}));
      alert(d.error || 'Error guardando el producto.');
      return;
    }
    closeForm();
    load();
  } catch (e) {
    alert('Error de red guardando el producto.');
  }
}

async function toggleActive(id) {
  const p = allProducts.find(x => x.id === id);
  if (!p) return;
  try {
    const res = await fetch('/api/products/' + encodeURIComponent(id) + '/active', {
      method: 'PATCH',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({active: !p.active}),
    });
    if (!res.ok) throw new Error('error');
    load();
  } catch (e) {
    alert('Error cambiando el estado del producto.');
  }
}

async function deleteProduct(id) {
  const p = allProducts.find(x => x.id === id);
  if (!p) return;
  if (!confirm('¿Eliminar "' + p.name + '" del monitoreo?')) return;
  let purgar = false;
  if (p.initial_price != null) {
    purgar = confirm('¿Borrar también su historial de precios de la base de datos? — Aceptar: borrar historial · Cancelar: conservarlo (quedará como huérfano purgable)');
  }
  try {
    const res = await fetch('/api/products/' + encodeURIComponent(id) + '?purgar_historial=' + purgar, {method: 'DELETE'});
    if (!res.ok) throw new Error('error');
    load();
  } catch (e) {
    alert('Error eliminando el producto.');
  }
}

document.querySelectorAll('.pill[data-f]').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.pill[data-f]').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    activeFilter = btn.dataset.f;
    applyFilters();
  });
});
document.getElementById('search').addEventListener('input', applyFilters);

async function togglePurchased(productId) {
  const p = allProducts.find(x => x.id === productId);
  if (!p) return;
  const target = !p.purchased;
  if (target && !confirm('¿Marcar "' + p.name + '" como comprado? Dejará de consultarse y de aparecer en reportes y notificaciones.')) return;
  try {
    const res = await fetch('/api/products/' + encodeURIComponent(productId) + '/purchased', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({purchased: target}),
    });
    if (!res.ok) throw new Error('error');
    p.purchased = target;
    // Re-render this card y aplicar filtros
    const grid = document.getElementById('grid');
    grid.innerHTML = allProducts.map(buildCard).join('');
    allProducts.forEach(renderChart);
    applyFilters();
  } catch (e) {
    alert('Error actualizando estado.');
  }
}

async function openHistory(productId) {
  const modal = document.getElementById('modal');
  const body  = document.getElementById('modal-body');
  const title = document.getElementById('modal-title');
  const p = allProducts.find(x => x.id === productId);
  title.textContent = 'Histórico — ' + (p?.name || productId);
  body.innerHTML = '<div class="modal-empty">Cargando…</div>';
  modal.classList.add('open');
  try {
    const res = await fetch('/api/products/' + encodeURIComponent(productId) + '/changes');
    if (!res.ok) throw new Error('error');
    const data = await res.json();
    if (!data.changes || data.changes.length === 0) {
      body.innerHTML = '<div class="modal-empty">Sin datos.</div>';
      return;
    }
    const rows = data.changes.map((c, i) => {
      const prev = i > 0 ? data.changes[i-1].price : null;
      let delta = '';
      if (prev != null) {
        const diff = c.price - prev;
        const cls  = diff < 0 ? 'down' : 'up';
        const sign = diff > 0 ? '+' : '';
        delta = `<td class="delta ${cls}">${sign}${fmtNum(diff, data.currency)}</td>`;
      } else {
        delta = '<td class="delta">—</td>';
      }
      const date = c.date.slice(0,10).split('-').reverse().join('/');
      return `<tr><td>${escapeHtml(date)}</td><td class="price">${fmt(c.price, data.currency)}</td>${delta}</tr>`;
    }).join('');
    body.innerHTML = `<table>
      <thead><tr><th>Fecha</th><th style="text-align:right">Precio</th><th style="text-align:right">Δ</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
  } catch (e) {
    body.innerHTML = '<div class="modal-empty">Error cargando histórico.</div>';
  }
}

function closeHistory() {
  document.getElementById('modal').classList.remove('open');
}

document.addEventListener('keydown', e => {
  if (e.key === 'Escape') { closeHistory(); closeForm(); }
});

const productActions = {history: openHistory, edit: openForm, active: toggleActive, delete: deleteProduct, purchased: togglePurchased, purge: purgarHuerfano};
document.addEventListener('click', event => {
  const control = event.target.closest('[data-action]');
  if (!control) return;
  const container = control.closest('[data-id]');
  const action = productActions[control.dataset.action];
  if (!container || typeof action !== 'function') return;
  event.preventDefault();
  action(container.dataset.id);
});

load();
</script>
</body>
</html>"""


if __name__ == "__main__":
    uvicorn.run("src.dashboard:app", host="127.0.0.1", port=8000, reload=False)
