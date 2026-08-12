from __future__ import annotations

import json
import re
from typing import Iterable

from bs4 import BeautifulSoup

PRICE_NUMERIC_RE = re.compile(r"(?<![\w.])(\d{1,3}(?:[.,]\d{3})+|\d+)(?:[.,]\d{1,2})?(?![\w])")

_BUY_KEYWORDS = (
    "agregar al carrito", "añadir al carrito", "add to cart",
    "comprar ahora", "buy now", "comprar", "pedir ahora",
    "order now", "añadir", "agregar",
)
_IN_STOCK_STR = "in stock"
_OUT_OF_STOCK_STR = "out of stock"
_OUT_OF_STOCK_KEYWORDS = (
    "agotado", "sin stock", _OUT_OF_STOCK_STR, "no disponible",
    "sold out", "unavailable", "no hay existencias",
)
_IN_STOCK_LABELS = {"instock", _IN_STOCK_STR, "http://schema.org/instock"}
_OUT_OF_STOCK_LABELS = {"outofstock", _OUT_OF_STOCK_STR, "http://schema.org/outofstock"}
KEYWORDS_PRICE = ("precio", "price", "total", "valor")


def _normalize_price(raw: str) -> float | None:
    """Convierte una cadena tipo '1.234,56' o '$1,234.56 USD' a float.

    Si el texto trae varios números (un selector que captura el precio repetido,
    o un bloque 'antes/ahora'), se normaliza cada token por separado y se devuelve
    el menor. Concatenar los dígitos —como hacía el strip directo— producía precios
    gigantes (ej. '1.599.900 1.599.900' → 15999001599900).
    """
    if raw is None:
        return None
    tokens = [m.group(0) for m in PRICE_NUMERIC_RE.finditer(str(raw))]
    if len(tokens) > 1:
        valores = [v for v in (_normalize_single(t) for t in tokens) if v is not None]
        if valores:
            return min(valores)
    return _normalize_single(str(raw))


def _normalize_single(raw: str) -> float | None:
    """Normaliza un único número ya aislado (un solo precio, sin texto vecino con dígitos)."""
    s = str(raw).strip()
    s = re.sub(r"[^\d.,\-]", "", s)
    if not s:
        return None

    has_comma = "," in s
    has_dot = "." in s

    if has_comma and has_dot:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif has_comma:
        decimals = s.split(",")[-1]
        if len(decimals) == 2:
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif has_dot:
        parts = s.split(".")
        if len(parts) > 2 or (len(parts) == 2 and len(parts[-1]) == 3):
            s = s.replace(".", "")

    try:
        value = float(s)
    except ValueError:
        return None
    if value <= 0:
        return None
    return value


def _extract_price_values(obj) -> Iterable[float]:
    """Recorre recursivamente un dict/list de JSON-LD buscando campos price."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key.lower() in {"price", "lowprice", "highprice"}:
                if isinstance(value, (int, float)):
                    yield float(value)
                elif isinstance(value, str):
                    normalized = _normalize_price(value)
                    if normalized is not None:
                        yield normalized
            else:
                yield from _extract_price_values(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _extract_price_values(item)


def extract_from_selector(soup: BeautifulSoup, selector: str) -> float | None:
    element = soup.select_one(selector)
    if element is None:
        return None
    return _normalize_price(element.get_text(" ", strip=True))


def extract_from_json_ld(soup: BeautifulSoup) -> float | None:
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        if not script.string:
            continue
        try:
            data = json.loads(script.string)
        except json.JSONDecodeError:
            continue
        prices = list(_extract_price_values(data))
        if prices:
            return min(prices)
    return None


def extract_from_meta(soup: BeautifulSoup) -> float | None:
    candidates = [
        {"property": "product:price:amount"},
        {"property": "og:price:amount"},
        {"itemprop": "price"},
        {"name": "twitter:data1"},
    ]
    for attrs in candidates:
        tag = soup.find("meta", attrs=attrs)
        if tag and tag.get("content"):
            price = _normalize_price(tag["content"])
            if price is not None:
                return price
    itemprop = soup.find(attrs={"itemprop": "price"})
    if itemprop:
        candidate = itemprop.get("content") or itemprop.get_text(" ", strip=True)
        price = _normalize_price(candidate)
        if price is not None:
            return price
    return None


def extract_from_regex(soup: BeautifulSoup) -> float | None:
    """Última opción: buscar precios cerca de palabras como 'precio'."""
    text = soup.get_text(" ", strip=True).lower()
    candidates: list[float] = []
    for match in PRICE_NUMERIC_RE.finditer(text):
        start = max(0, match.start() - 40)
        end = min(len(text), match.end() + 20)
        context = text[start:end]
        if any(k in context for k in KEYWORDS_PRICE):
            value = _normalize_price(match.group(0))
            if value is not None:
                candidates.append(value)
    if not candidates:
        return None
    return min(candidates)


def _parse_schema_availability(val: str) -> bool | None:
    v = val.lower().strip()
    if any(label in v for label in _IN_STOCK_LABELS):
        return True
    if any(label in v for label in _OUT_OF_STOCK_LABELS):
        return False
    return None


def _availability_from_json_ld(soup: BeautifulSoup) -> bool | None:
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        if not script.string:
            continue
        try:
            data = json.loads(script.string)
        except json.JSONDecodeError:
            continue
        result = _find_availability_in_json(data)
        if result is not None:
            return result
    return None


def _availability_from_meta(soup: BeautifulSoup) -> bool | None:
    meta = soup.find("meta", attrs={"property": "product:availability"}) or \
           soup.find("meta", attrs={"itemprop": "availability"})
    if meta and meta.get("content"):
        return _parse_schema_availability(meta["content"])
    return None


def _availability_from_elements(soup: BeautifulSoup) -> bool | None:
    # Recorre todos los elementos: si alguno tiene keyword de compra activa, gana sobre
    # cualquier texto de "sin stock" que pueda aparecer en productos relacionados o footer.
    found_out = False
    for tag in soup.find_all(["button", "a", "input"]):
        text = (tag.get_text(" ", strip=True) or tag.get("value") or "").lower()
        if any(k in text for k in _BUY_KEYWORDS):
            return True
        if any(k in text for k in _OUT_OF_STOCK_KEYWORDS):
            found_out = True
    return False if found_out else None


def _availability_from_page_text(soup: BeautifulSoup) -> bool | None:
    page_text = soup.get_text(" ", strip=True).lower()
    # Priorizar señal de compra: un "agregar al carrito" visible pesa más que
    # texto de "sin stock" que puede estar en secciones de productos alternativos.
    if any(k in page_text for k in _BUY_KEYWORDS):
        return True
    if any(k in page_text for k in _OUT_OF_STOCK_KEYWORDS):
        return False
    return None


def extract_availability(soup: BeautifulSoup) -> bool | None:
    """Retorna True si comprable, False si agotado, None si no se pudo determinar."""
    for strategy in (
        _availability_from_json_ld,
        _availability_from_meta,
        _availability_from_elements,
        _availability_from_page_text,
    ):
        result = strategy(soup)
        if result is not None:
            return result
    return None


def _search_json_dict(d: dict) -> bool | None:
    for key, value in d.items():
        if key.lower() == "availability" and isinstance(value, str):
            result = _parse_schema_availability(value)
            if result is not None:
                return result
        result = _find_availability_in_json(value)
        if result is not None:
            return result
    return None


def _find_availability_in_json(obj) -> bool | None:
    if isinstance(obj, dict):
        return _search_json_dict(obj)
    if isinstance(obj, list):
        return next((r for r in (_find_availability_in_json(i) for i in obj) if r is not None), None)
    return None


def extract_price(html: str, selector: str | None = None) -> float | None:
    """Aplica la cascada de estrategias y retorna el primer precio encontrado."""
    soup = BeautifulSoup(html, "lxml")
    strategies = []
    if selector:
        strategies.append(lambda: extract_from_selector(soup, selector))
    strategies.extend([
        lambda: extract_from_json_ld(soup),
        lambda: extract_from_meta(soup),
        lambda: extract_from_regex(soup),
    ])
    for strategy in strategies:
        price = strategy()
        if price is not None:
            return price
    return None


def extract_price_and_availability(html: str, selector: str | None = None) -> tuple[float | None, bool | None]:
    """Extrae precio y disponibilidad del mismo parse de HTML."""
    soup = BeautifulSoup(html, "lxml")
    strategies = []
    if selector:
        strategies.append(lambda: extract_from_selector(soup, selector))
    strategies.extend([
        lambda: extract_from_json_ld(soup),
        lambda: extract_from_meta(soup),
        lambda: extract_from_regex(soup),
    ])
    price = None
    for strategy in strategies:
        price = strategy()
        if price is not None:
            break
    available = extract_availability(soup)
    return price, available
