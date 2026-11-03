"""Regresiones de precios y disponibilidad por talla con el marcado observado en Mango."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from bs4 import BeautifulSoup

from src.config import Config, EmailConfig, ScraperConfig, TelegramConfig
from src.extractors import extract_availability, extract_offer, extract_price_and_availability
from src.scraper import fetch_offer


mangoProductUrl = 'https://shop.mango.com/co/es/p/hombre/camisetas/basicas/camiseta-slim-fit-180gsm/37011432/01/00'
regularPriceMarkup = '''
<span aria-hidden="true" itemprop="offers" itemscope itemtype="https://schema.org/Offer">
  <meta itemprop="priceCurrency" content="COP"><meta itemprop="price" content="99900">
  <span class="SinglePrice-module__sample__crossed">$ 99.900</span>
</span>
'''
currentPriceMarkup = '''
<span aria-hidden="true" itemprop="offers" itemscope itemtype="https://schema.org/Offer">
  <meta itemprop="priceCurrency" content="COP"><meta itemprop="price" content="79920">
  <span class="SinglePrice-module__sample__finalPrice">$ 79.920</span>
</span>
'''
availableSizeMarkup = '<button id="product.sizeSelector.available.19" aria-disabled="false">XS</button>'
unavailableSizeMarkup = '<button id="product.sizeSelector.unavailable.20" aria-disabled="true">S No disponible ¡Lo quiero!</button>'


def _mango_html(content: str) -> str:
    return (
        f'<link rel="canonical" href="{mangoProductUrl}">'
        f'<main><article itemtype="https://schema.org/Product" itemscope>{content}</article></main>'
    )


class MangoExtractionTests(unittest.TestCase):
    """Comprueba las señales de stock y precio sin contactar tiendas."""

    def test_mixed_sizes_are_available_at_current_price(self) -> None:
        html = _mango_html(regularPriceMarkup + currentPriceMarkup + unavailableSizeMarkup + availableSizeMarkup)
        offer = extract_offer(html, page_url=mangoProductUrl, expected_currency='COP')
        self.assertEqual((offer.price, offer.available, offer.currency, offer.method), (79920, True, 'COP', 'mango'))

    def test_all_sizes_unavailable_override_enabled_add_button(self) -> None:
        html = _mango_html(currentPriceMarkup + unavailableSizeMarkup + '<button aria-disabled="false">Añadir</button>')
        offer = extract_offer(html, page_url=mangoProductUrl)
        self.assertEqual((offer.price, offer.available), (79920, False))

    def test_size_availability_respects_disabled_controls(self) -> None:
        controls = (
            availableSizeMarkup.replace('aria-disabled="false"', 'aria-disabled="true"'),
            availableSizeMarkup.replace('<button ', '<button disabled '),
            '<fieldset disabled>' + availableSizeMarkup + '</fieldset>',
        )
        for control in controls:
            with self.subTest(control=control):
                self.assertIs(extract_offer(_mango_html(currentPriceMarkup + control)).available, False)

    def test_hidden_and_related_sizes_do_not_prove_availability(self) -> None:
        for attributes in ('hidden', 'aria-hidden="true"', 'style="display:none"', 'class="related-products"'):
            with self.subTest(attributes=attributes):
                hidden_size = f'<section {attributes}>{availableSizeMarkup}</section>'
                html = _mango_html(currentPriceMarkup + unavailableSizeMarkup + hidden_size)
                self.assertIs(extract_offer(html).available, False)

    def test_missing_sizes_keep_unknown_availability(self) -> None:
        for controls in ('', '<button>Añadir</button>', '<div hidden>' + availableSizeMarkup + '</div>'):
            with self.subTest(controls=controls):
                html = _mango_html(currentPriceMarkup + controls)
                self.assertIsNone(extract_offer(html).available)

    def test_other_product_sizes_and_prices_are_ignored(self) -> None:
        related = '<section class="related-products">' + currentPriceMarkup.replace('79920', '10') + availableSizeMarkup + '</section>'
        html = _mango_html(currentPriceMarkup + unavailableSizeMarkup + related)
        html += '<article itemtype="https://schema.org/Product">' + availableSizeMarkup + '</article>'
        offer = extract_offer(html)
        self.assertEqual((offer.price, offer.available), (79920, False))

    def test_regular_price_without_discount_is_supported(self) -> None:
        html = _mango_html(regularPriceMarkup.replace('__crossed', '__container') + availableSizeMarkup)
        offer = extract_offer(html)
        self.assertEqual((offer.price, offer.available, offer.currency), (99900, True, 'COP'))

    def test_ambiguous_prices_are_not_guessed(self) -> None:
        for prices in (
            regularPriceMarkup + currentPriceMarkup.replace('__finalPrice', '__container'),
            currentPriceMarkup + currentPriceMarkup.replace('79920', '69900'),
        ):
            with self.subTest(prices=prices):
                self.assertIsNone(extract_offer(_mango_html(prices + availableSizeMarkup)).price)

    def test_invalid_current_price_does_not_fall_back_to_old_price(self) -> None:
        for raw in ('0', '-1', 'NaN', 'Infinity', 'invalid'):
            with self.subTest(raw=raw):
                html = _mango_html(regularPriceMarkup + currentPriceMarkup.replace('79920', raw) + availableSizeMarkup)
                self.assertIsNone(extract_offer(html).price)

    def test_custom_selector_preserves_mango_stock(self) -> None:
        html = _mango_html(currentPriceMarkup + availableSizeMarkup + '<span id="custom-price">69900</span>')
        offer = extract_offer(html, '#custom-price')
        self.assertEqual((offer.price, offer.available, offer.method), (69900, True, 'selector'))
        self.assertEqual(extract_offer(html, '#missing-price').price, 79920)

    def test_legacy_interfaces_use_canonical_mango_url(self) -> None:
        html = _mango_html(currentPriceMarkup + availableSizeMarkup)
        self.assertEqual(extract_price_and_availability(html), (79920, True))
        self.assertIs(extract_availability(BeautifulSoup(html, 'lxml')), True)

    def test_other_domains_keep_generic_extraction(self) -> None:
        html = _mango_html(currentPriceMarkup + availableSizeMarkup)
        for page_url in ('https://example.test/product', 'https://shop.mango.com.example.test/product'):
            with self.subTest(page_url=page_url):
                offer = extract_offer(html, page_url=page_url)
                self.assertEqual((offer.price, offer.available, offer.method), (79920, None, 'meta'))

    def test_http_returns_mango_offer_without_browser(self) -> None:
        config = Config(
            TelegramConfig(False, '', ''), EmailConfig(False, '', 587, '', '', ''),
            ScraperConfig(1, 1, 'local-tests'),
        )
        html = _mango_html(regularPriceMarkup + currentPriceMarkup + availableSizeMarkup)
        with (
            patch('src.scraper._fetch_http_con_reintentos', return_value=html),
            patch('src.scraper._fetch_playwright') as browser,
        ):
            offer = fetch_offer(mangoProductUrl, None, config, expected_currency='COP')
        self.assertEqual((offer.price, offer.available, offer.currency, offer.method), (79920, True, 'COP', 'http:mango'))
        browser.assert_not_called()


if __name__ == '__main__':
    unittest.main()
