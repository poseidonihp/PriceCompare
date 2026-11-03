from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlsplit, urlunsplit

import soupsieve
from bs4 import BeautifulSoup, Tag

priceNumericPattern = re.compile(r'(?<![\w.])[-+]?(?:\d{1,3}(?:[.,]\d{3})+|\d+)(?:[.,]\d{1,2})?(?!\w)')
priceCleanupPattern = re.compile(r'[^\d.,+-]')
installmentPattern = re.compile(r'\b(?:\d+\s+(?:cuotas|installments|payments)|cuotas|installments|mensual|monthly)\b', re.I)
currentPricePattern = re.compile(r'\b(?:ahora|now|sale price|precio actual|precio de oferta)\b\s*:?\s*', re.I)
priceLabelPattern = re.compile(r'\b(?:precio|price|total|valor)\b\s*:?\s*(?:[A-Z]{3}\s*)?\$?\s*([-+\d][\d.,]*)', re.I)
currencyPattern = re.compile(r'\b([A-Z]{3})\b')
buyKeywords = ('agregar al carrito', 'añadir al carrito', 'add to cart', 'comprar ahora', 'buy now', 'comprar', 'pedir ahora', 'order now')
outOfStockKeywords = ('agotado', 'sin stock', 'out of stock', 'no disponible', 'sold out', 'unavailable', 'no hay existencias')
ignoredSections = ('related', 'recommend', 'similar', 'recomend', 'relacionad', 'cross-sell', 'upsell')
hiddenStylePattern = re.compile(r'(?:^|;)\s*(?:display\s*:\s*none|visibility\s*:\s*hidden)\b', re.I)
productType = 'Product'
jsonLdMethod = 'json_ld'
mangoAvailableSizePrefix = 'product.sizeSelector.available.'
mangoUnavailableSizePrefix = 'product.sizeSelector.unavailable.'
mangoCurrentPriceSelector = '[itemtype$="/Offer"]:has([class*="SinglePrice-"][class*="__finalPrice"]) [itemprop="price"]'


@dataclass(frozen=True)
class ExtractedOffer:
    """Precio, disponibilidad y moneda de una misma oferta, junto con su método de extracción."""

    price: float | None = None
    available: bool | None = None
    currency: str | None = None
    method: str | None = None


class ExtractionError(ValueError):
    """Configuración de extracción inválida para un producto."""


def validate_selector(selector: str | None) -> None:
    """Valida el selector opcional antes de consultar o guardar un producto.

    Args:
        selector: Selector CSS configurado.
    """
    if selector is None:
        return
    if not isinstance(selector, str):
        raise ExtractionError('El selector CSS debe ser texto.')
    try:
        soupsieve.compile(selector)
    except soupsieve.SelectorSyntaxError as exc:
        raise ExtractionError('Selector CSS inválido.') from exc


def _positive_price(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        price = float(value)
    except OverflowError:
        return None
    return price if math.isfinite(price) and price > 0 else None


def _normalize_single(raw: str) -> float | None:
    """Normaliza un importe aislado con separadores decimales o de miles."""
    number = priceCleanupPattern.sub('', raw.strip())
    if ',' in number and '.' in number:
        if number.rfind(',') > number.rfind('.'):
            number = number.replace('.', '').replace(',', '.')
        else:
            number = number.replace(',', '')
    elif ',' in number:
        number = number.replace(',', '.') if len(number.split(',')[-1]) in (1, 2) else number.replace(',', '')
    elif '.' in number:
        parts = number.split('.')
        if len(parts) > 2 or len(parts[-1]) == 3:
            number = number.replace('.', '')
    try:
        return _positive_price(float(number))
    except ValueError:
        return None


def _normalize_price(raw: str) -> float | None:
    """Descarta cuotas y exige un importe inequívoco o una etiqueta explícita de precio actual."""
    text = installmentPattern.split(str(raw), maxsplit=1)[0]
    current = currentPricePattern.search(text)
    if current is not None:
        text = text[current.end():]
    tokens = [match.group(0) for match in priceNumericPattern.finditer(text)]
    prices = [_normalize_single(token) for token in tokens]
    if not prices or any(price is None for price in prices):
        return None
    if current is not None:
        return prices[0]
    return prices[0] if len(set(prices)) == 1 else None


def _schema_type(node: dict, expected: str) -> bool:
    types = node.get('@type', [])
    if isinstance(types, str):
        types = [types]
    return isinstance(types, list) and any(str(kind).rsplit('/', 1)[-1].casefold() == expected.casefold() for kind in types)


def _schema_nodes(value: object) -> list[dict]:
    """Recorre entidades principales sin incorporar recomendaciones, accesorios ni listas de productos relacionados."""
    if isinstance(value, list):
        return [node for item in value for node in _schema_nodes(item)]
    if not isinstance(value, dict):
        return []
    nodes = [value]
    for key in ('@graph', 'mainEntity', '@included'):
        nodes.extend(_schema_nodes(value.get(key)))
    return nodes


def _json_nodes(soup: BeautifulSoup) -> list[dict]:
    nodes: list[dict] = []
    for script in soup.find_all('script', attrs={'type': 'application/ld+json'}):
        try:
            nodes.extend(_schema_nodes(json.loads(script.get_text())))
        except (ValueError, RecursionError):
            continue
    return nodes


def _normalized_match_url(url: str) -> str:
    """Normaliza el orden de parámetros sin perder la identidad de color o talla.

    Args:
        url: Dirección de producto u oferta.
    Returns:
        str
    """
    parts = urlsplit(url)
    query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip('/'), query, ''))


def _matches_url(node: dict, page_url: str) -> bool:
    if not page_url:
        return False
    expected = _normalized_match_url(page_url)
    for key in ('url', '@id'):
        value = node.get(key)
        if isinstance(value, str) and _normalized_match_url(urljoin(page_url, value)) == expected:
            return True
    return False


def _product_nodes(nodes: list[dict]) -> list[dict]:
    return [node for node in nodes if _schema_type(node, productType) or _schema_type(node, 'ProductGroup')]


def _select_product(nodes: list[dict], soup: BeautifulSoup, page_url: str) -> dict | None:
    products = _product_nodes(nodes)
    matching = [product for product in products if _matches_url(product, page_url)]
    if len(matching) == 1:
        return matching[0]
    heading = soup.find('h1')
    if heading is not None:
        name = heading.get_text(' ', strip=True).casefold()
        matching = [product for product in products if str(product.get('name', '')).strip().casefold() == name]
        if len(matching) == 1:
            return matching[0]
    return products[0] if len(products) == 1 else None


def _parse_schema_availability(value: object) -> bool | None:
    if not isinstance(value, str):
        return None
    label = value.lower().rstrip('/').rsplit('/', 1)[-1].replace(' ', '')
    if label == 'instock':
        return True
    if label in ('outofstock', 'soldout', 'discontinued'):
        return False
    return None


def _offer_price(offer: dict) -> float | None:
    raw = offer.get('price', offer.get('lowPrice'))
    if raw is None:
        specification = offer.get('priceSpecification')
        if isinstance(specification, dict):
            raw = specification.get('price')
    return _normalize_price(raw) if isinstance(raw, str) else _positive_price(raw)


def _offer_values(value: object, references: dict[str, dict]) -> list[dict]:
    if isinstance(value, list):
        return [offer for item in value for offer in _offer_values(item, references)]
    if not isinstance(value, dict):
        return []
    referenced = references.get(str(value.get('@id', '')), value)
    nested = referenced.get('offers')
    if isinstance(nested, (dict, list)):
        return _offer_values(nested, {})
    return [referenced]


def _offer_candidates(product: dict, nodes: list[dict]) -> list[ExtractedOffer]:
    """Construye candidatos conservando los datos propios de cada oferta."""
    references = {str(node['@id']): node for node in nodes if '@id' in node}
    candidates: list[ExtractedOffer] = []
    for offer in _offer_values(product.get('offers'), references):
        price = _offer_price(offer)
        if price is None:
            continue
        currency = offer.get('priceCurrency', product.get('priceCurrency'))
        currency = currency.strip().upper() if isinstance(currency, str) else None
        available = _parse_schema_availability(offer.get('availability', product.get('availability')))
        candidates.append(ExtractedOffer(price, available, currency, jsonLdMethod))
    return candidates


def _compatible_offers(candidates: list[ExtractedOffer], expected_currency: str | None) -> list[ExtractedOffer]:
    if expected_currency:
        compatible = [offer for offer in candidates if offer.currency in (None, expected_currency.upper())]
        return compatible or candidates
    return candidates


def _choose_offer(candidates: list[ExtractedOffer]) -> ExtractedOffer:
    if len({offer.currency for offer in candidates if offer.currency}) > 1:
        return ExtractedOffer()
    for state in (True, None, False):
        eligible = [offer for offer in candidates if offer.available is state]
        if eligible:
            return min(eligible, key=lambda offer: offer.price if offer.price is not None else math.inf)
    return ExtractedOffer()


def _variant_offer_candidates(product: dict, nodes: list[dict], page_url: str) -> list[ExtractedOffer]:
    """Selecciona ofertas de la variante solicitada dentro del producto principal.

    Args:
        product: Producto o grupo principal.
        nodes: Entidades estructuradas disponibles.
        page_url: URL con la variante solicitada.
    Returns:
        list[ExtractedOffer]
    """
    references = {str(node['@id']): node for node in nodes if '@id' in node}
    candidates: list[ExtractedOffer] = []
    for variant in _schema_nodes(product.get('hasVariant')):
        variant = references.get(str(variant.get('@id', '')), variant)
        if not _schema_type(variant, productType):
            continue
        matching_offers = [
            offer for offer in _offer_values(variant.get('offers'), references)
            if _matches_url({'url': offer.get('url')}, page_url)
        ]
        if matching_offers:
            candidates.extend(_offer_candidates({**variant, 'offers': matching_offers}, nodes))
        elif _matches_url({'url': variant.get('url')}, page_url):
            candidates.extend(_offer_candidates(variant, nodes))
    return candidates


def _json_offer(soup: BeautifulSoup, page_url: str, expected_currency: str | None) -> ExtractedOffer:
    nodes = _json_nodes(soup)
    product = _select_product(nodes, soup, page_url)
    if product is None:
        return ExtractedOffer()
    if isinstance(product.get('hasVariant'), (dict, list)) and product['hasVariant']:
        candidates = _variant_offer_candidates(product, nodes, page_url)
    else:
        candidates = _offer_candidates(product, nodes)
    return _choose_offer(_compatible_offers(candidates, expected_currency))


def _is_hidden(tag: Tag) -> bool:
    return tag.has_attr('hidden') or tag.get('aria-hidden') == 'true' or bool(hiddenStylePattern.search(str(tag.get('style', ''))))


def _is_ignored(tag: Tag, check_visibility: bool = True) -> bool:
    for ancestor in [tag, *tag.parents]:
        if not isinstance(ancestor, Tag):
            continue
        if ancestor.name in ('footer', 'nav', 'aside', 'script', 'style', 'template'):
            return True
        if check_visibility and _is_hidden(ancestor):
            return True
        identity = str(ancestor.get('id', '')) + ' ' + ' '.join(ancestor.get('class', []))
        if any(section in identity.lower() for section in ignoredSections):
            return True
    return False


def _page_scope(soup: BeautifulSoup) -> Tag:
    return soup.select_one('[itemtype$="/Product"]') or soup.find('main') or soup.body or soup


def _control_disabled(tag: Tag) -> bool:
    if tag.has_attr('disabled') or tag.get('aria-disabled') == 'true':
        return True
    return any(parent.name == 'fieldset' and parent.has_attr('disabled') for parent in tag.parents)


def _availability_from_elements(scope: Tag) -> bool | None:
    found_out = False
    for tag in scope.find_all(['button', 'a', 'input']):
        if _is_ignored(tag):
            continue
        text = str(tag.get('value') or tag.get_text(' ', strip=True)).lower()
        if any(keyword in text for keyword in buyKeywords):
            if not _control_disabled(tag):
                return True
            found_out = True
        if any(keyword in text for keyword in outOfStockKeywords):
            found_out = True
    return False if found_out else None


def _availability_from_page(scope: Tag) -> bool | None:
    available = _availability_from_elements(scope)
    if available is not None:
        return available
    text = ' '.join(str(node).lower() for node in scope.strings if not _is_ignored(node.parent))
    return False if any(keyword in text for keyword in outOfStockKeywords) else None


def _meta_availability(soup: Tag) -> bool | None:
    tag = soup.select_one('meta[property="product:availability"], [itemprop="availability"]')
    return _parse_schema_availability(tag.get('content') or tag.get('href')) if tag else None


def _meta_currency(soup: Tag) -> str | None:
    tag = soup.select_one('meta[property="product:price:currency"], meta[property="og:price:currency"], [itemprop="priceCurrency"]')
    if tag is None:
        return None
    value = str(tag.get('content') or tag.get_text(' ', strip=True)).strip().upper()
    return value if currencyPattern.fullmatch(value) else None


def extract_from_selector(soup: BeautifulSoup, selector: str) -> float | None:
    """Extrae el importe del selector configurado.

    Args:
        soup: Documento analizado.
        selector: Selector CSS.
    Returns:
        float | None
    """
    validate_selector(selector)
    element = soup.select_one(selector)
    if element is None:
        return None
    return _normalize_price(str(element.get('content') or element.get_text(' ', strip=True)))


def extract_from_json_ld(soup: BeautifulSoup) -> float | None:
    """Extrae el precio de una oferta del producto principal.

    Args:
        soup: Documento analizado.
    Returns:
        float | None
    """
    return _json_offer(soup, _canonical_url(soup), None).price


def _meta_offer(soup: BeautifulSoup) -> ExtractedOffer:
    for selector in ('meta[property="product:price:amount"]', 'meta[property="og:price:amount"]', '[itemprop="price"]'):
        for tag in soup.select(selector):
            if _is_ignored(tag, check_visibility=False):
                continue
            price = _normalize_price(str(tag.get('content') or tag.get_text(' ', strip=True)))
            if price is not None:
                return _fallback_offer(soup, price, 'meta', tag)
    return ExtractedOffer()


def extract_from_meta(soup: BeautifulSoup) -> float | None:
    """Extrae el precio declarado en metadatos del producto.

    Args:
        soup: Documento analizado.
    Returns:
        float | None
    """
    return _meta_offer(soup).price


def extract_from_regex(soup: BeautifulSoup) -> float | None:
    """Busca importes etiquetados en el contenido principal y rechaza resultados ambiguos.

    Args:
        soup: Documento analizado.
    Returns:
        float | None
    """
    scope = _page_scope(soup)
    text = ' '.join(str(node) for node in scope.strings if not _is_ignored(node.parent))
    text = installmentPattern.split(text, maxsplit=1)[0]
    prices = {_normalize_single(match.group(1)) for match in priceLabelPattern.finditer(text)}
    prices.discard(None)
    return next(iter(prices)) if len(prices) == 1 else None


def _canonical_url(soup: BeautifulSoup) -> str:
    tag = soup.select_one('link[rel="canonical"]')
    return str(tag.get('href', '')) if tag else ''


def _fallback_offer(soup: BeautifulSoup, price: float, method: str, element: Tag | None = None) -> ExtractedOffer:
    scope = None
    if element is not None:
        scope = element.find_parent(attrs={'itemtype': lambda itemtype: itemtype and str(itemtype).rstrip('/').endswith(('/Offer', '/Product'))})
        scope = scope or element.find_parent(['form', 'main'])
    metadata_scope = scope or soup
    available = _meta_availability(metadata_scope)
    if available is None:
        available = _availability_from_page(scope or _page_scope(soup))
    return ExtractedOffer(price, available, _meta_currency(metadata_scope), method)


def _mango_availability(scope: Tag) -> bool | None:
    """Considera disponible el color mostrado cuando alguna talla permite comprar.

    Args:
        scope: Contenedor del producto principal.
    Returns:
        bool | None
    """
    size_buttons = [
        button for button in scope.find_all('button')
        if str(button.get('id', '')).startswith((mangoAvailableSizePrefix, mangoUnavailableSizePrefix))
        and not _is_ignored(button)
    ]
    if not size_buttons:
        return None
    return any(
        str(button.get('id', '')).startswith(mangoAvailableSizePrefix) and not _control_disabled(button)
        for button in size_buttons
    )


def _mango_price(scope: Tag) -> tuple[float | None, str | None]:
    """Selecciona el precio vigente de Mango y descarta importes ambiguos.

    Args:
        scope: Contenedor del producto principal.
    Returns:
        tuple[float | None, str | None]
    """
    price_tags = [
        tag for tag in scope.select(mangoCurrentPriceSelector) or scope.select('[itemprop="price"]')
        if not _is_ignored(tag, check_visibility=False)
    ]
    prices = {_normalize_price(str(tag.get('content') or tag.get_text(' ', strip=True))) for tag in price_tags}
    if len(prices) != 1:
        return None, None
    offer_scope = price_tags[0].find_parent(attrs={'itemtype': 'https://schema.org/Offer'})
    return prices.pop(), _meta_currency(offer_scope if offer_scope is not None else scope)


def _mango_offer(soup: BeautifulSoup, page_url: str, selector: str | None) -> ExtractedOffer | None:
    """Relaciona el precio vigente con las tallas del color mostrado en Mango.

    Args:
        soup: Documento analizado.
        page_url: URL consultada.
        selector: Selector opcional de precio.
    Returns:
        ExtractedOffer | None
    """
    if urlparse(page_url).hostname != 'shop.mango.com':
        return None
    scope = _page_scope(soup)
    price, currency = _mango_price(scope)
    method = 'mango'
    if selector:
        selected_price = extract_from_selector(soup, selector)
        if selected_price is not None:
            price, method = selected_price, 'selector'
    return ExtractedOffer(price, _mango_availability(scope), currency, method)


def extract_offer(
    html: str, selector: str | None = None, page_url: str = '', expected_currency: str | None = None,
) -> ExtractedOffer:
    """Extrae una oferta coherente mediante reglas de tienda, selector, JSON-LD, metadatos o texto.

    Args:
        html: Documento recibido.
        selector: Selector opcional.
        page_url: URL consultada.
        expected_currency: Moneda configurada.
    Returns:
        ExtractedOffer
    """
    validate_selector(selector)
    soup = BeautifulSoup(html, 'lxml')
    page_url = page_url or _canonical_url(soup)
    mango = _mango_offer(soup, page_url, selector)
    if mango is not None:
        return mango
    structured = _json_offer(soup, page_url, expected_currency)
    if selector:
        price = extract_from_selector(soup, selector)
        if price is not None:
            if price == structured.price:
                return ExtractedOffer(price, structured.available, structured.currency, 'selector')
            return _fallback_offer(soup, price, 'selector', soup.select_one(selector))
    if structured.price is not None:
        if structured.available is None:
            return ExtractedOffer(structured.price, _availability_from_page(_page_scope(soup)), structured.currency, jsonLdMethod)
        return structured
    metadata = _meta_offer(soup)
    if metadata.price is not None:
        return metadata
    price = extract_from_regex(soup)
    if price is not None:
        return _fallback_offer(soup, price, 'regex')
    return ExtractedOffer()


def extract_availability(soup: BeautifulSoup) -> bool | None:
    """Consulta disponibilidad del producto principal.

    Args:
        soup: Documento analizado.
    Returns:
        bool | None
    """
    mango = _mango_offer(soup, _canonical_url(soup), None)
    if mango is not None:
        return mango.available
    structured = _json_offer(soup, _canonical_url(soup), None)
    if structured.available is not None:
        return structured.available
    available = _meta_availability(soup)
    return available if available is not None else _availability_from_page(_page_scope(soup))


def extract_price(html: str, selector: str | None = None) -> float | None:
    """Conserva la consulta de precio para consumidores existentes.

    Args:
        html: Documento recibido.
        selector: Selector opcional.
    Returns:
        float | None
    """
    return extract_offer(html, selector).price


def extract_price_and_availability(html: str, selector: str | None = None) -> tuple[float | None, bool | None]:
    """Conserva la interfaz de precio y disponibilidad.

    Args:
        html: Documento recibido.
        selector: Selector opcional.
    Returns:
        tuple[float | None, bool | None]
    """
    offer = extract_offer(html, selector)
    return offer.price, offer.available
