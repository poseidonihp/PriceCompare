"""Regresiones de ofertas por color y talla con el JSON-LD observado en Uniqlo."""
from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from src.config import Config, EmailConfig, ScraperConfig, TelegramConfig
from src.extractors import extract_offer
from src.scraper import fetch_offer


uniqloProductUrl = 'https://www.uniqlo.com/us/en/products/E455365-000/00'
selectedVariantUrl = uniqloProductUrl + '?colorDisplayCode=00&sizeDisplayCode=001'
productName = 'SUPIMA® Cotton T-Shirt'


def _variant(color: str = '00', size: str = '001', price: object = '24.9', available: bool = True) -> dict:
    return {
        '@type': 'Product', 'name': f'{productName} - {color} / {size}',
        'offers': {
            '@type': 'Offer', 'price': price, 'priceCurrency': 'USD',
            'url': f'{uniqloProductUrl}?colorDisplayCode={color}&sizeDisplayCode={size}',
            'availability': 'https://schema.org/InStock' if available else 'https://schema.org/OutOfStock',
        },
    }


def _group(variants: object) -> dict:
    return {'@type': 'ProductGroup', 'name': productName, 'url': uniqloProductUrl, 'hasVariant': variants}


def _schema_html(nodes: list[dict]) -> str:
    return (
        f'<h1>{productName}</h1><script type="application/ld+json">'
        + json.dumps({'@context': 'https://schema.org', '@graph': nodes}) + '</script>'
    )


class UniqloExtractionTests(unittest.TestCase):
    """Verifica la variante configurada sin mezclar precios ni disponibilidad de otros productos."""

    def test_selected_variant_keeps_its_price_currency_and_stock(self) -> None:
        html = _schema_html([_group([
            _variant(color='09', price='14.9'), _variant(size='004', price='19.9'), _variant(),
        ])])
        offer = extract_offer(html, page_url=selectedVariantUrl, expected_currency='USD')
        self.assertEqual((offer.price, offer.available, offer.currency, offer.method), (24.9, True, 'USD', 'json_ld'))

    def test_unavailable_selected_size_does_not_use_available_sizes(self) -> None:
        html = _schema_html([_group([_variant(size='004', price='19.9'), _variant(available=False)])])
        offer = extract_offer(html, page_url=selectedVariantUrl)
        self.assertEqual((offer.price, offer.available), (24.9, False))

    def test_variant_has_priority_over_group_summary_offer(self) -> None:
        group = _group([_variant(available=False)])
        group['offers'] = {'price': 10, 'priceCurrency': 'USD', 'availability': 'InStock'}
        offer = extract_offer(_schema_html([group]), page_url=selectedVariantUrl)
        self.assertEqual((offer.price, offer.available), (24.9, False))

    def test_query_order_relative_urls_and_fragments_preserve_variant(self) -> None:
        variant = _variant()
        variant['offers']['url'] = '/us/en/products/E455365-000/00/?sizeDisplayCode=001&colorDisplayCode=00#offer'
        offer = extract_offer(_schema_html([_group([variant])]), page_url=selectedVariantUrl)
        self.assertEqual((offer.price, offer.available), (24.9, True))

    def test_missing_variant_does_not_borrow_other_color_or_size(self) -> None:
        for variant in (_variant(color='09'), _variant(size='004')):
            with self.subTest(variant=variant):
                offer = extract_offer(_schema_html([_group([variant])]), page_url=selectedVariantUrl)
                self.assertIsNone(offer.price)
                self.assertIsNone(offer.available)

    def test_missing_variant_parameters_do_not_choose_cheapest_size(self) -> None:
        html = _schema_html([_group([_variant(), _variant(size='004', price='19.9')])])
        self.assertIsNone(extract_offer(html, page_url=uniqloProductUrl).price)

    def test_url_on_variant_product_is_supported(self) -> None:
        variant = _variant()
        variant['url'] = variant['offers'].pop('url')
        offer = extract_offer(_schema_html([_group(variant)]), page_url=selectedVariantUrl)
        self.assertEqual((offer.price, offer.available), (24.9, True))

    def test_offer_list_is_filtered_before_price_selection(self) -> None:
        variant = _variant()
        variant['offers'] = [_variant(color='09', price='9.9')['offers'], variant['offers']]
        offer = extract_offer(_schema_html([_group([variant])]), page_url=selectedVariantUrl)
        self.assertEqual(offer.price, 24.9)

    def test_referenced_variant_and_offer_are_supported(self) -> None:
        variant = _variant()
        offer_data = {**variant['offers'], '@id': '#selected-offer'}
        variant.update({'@id': '#selected-variant', 'offers': {'@id': '#selected-offer'}})
        group = _group([{'@id': '#selected-variant'}])
        offer = extract_offer(_schema_html([group, variant, offer_data]), page_url=selectedVariantUrl)
        self.assertEqual((offer.price, offer.available), (24.9, True))

    def test_fragment_ids_do_not_identify_the_requested_variant(self) -> None:
        other_variant = _variant(color='09', price='9.9')
        other_variant['@id'] = '#other-variant'
        other_variant['offers']['@id'] = '#other-offer'
        html = _schema_html([_group([other_variant, _variant()])])
        self.assertEqual(extract_offer(html, page_url=selectedVariantUrl).price, 24.9)
        other_variant['offers'].pop('url')
        html = _schema_html([_group([other_variant])])
        self.assertIsNone(extract_offer(html, page_url=selectedVariantUrl).price)

    def test_empty_variant_list_preserves_direct_offers(self) -> None:
        group = _group([])
        group['offers'] = _variant()['offers']
        offer = extract_offer(_schema_html([group]), page_url=selectedVariantUrl)
        self.assertEqual((offer.price, offer.available), (24.9, True))

    def test_related_product_variants_do_not_supply_a_price(self) -> None:
        group = _group([_variant(color='09')])
        group['isRelatedTo'] = _group([_variant(price='9.9')])
        self.assertIsNone(extract_offer(_schema_html([group]), page_url=selectedVariantUrl).price)

    def test_variant_from_other_top_level_group_is_ignored(self) -> None:
        accessory = _group([_variant(price='9.9')])
        accessory['name'] = 'Other shirt'
        accessory['url'] = uniqloProductUrl.replace('455365', '999999')
        html = _schema_html([accessory, _group([_variant()])])
        self.assertEqual(extract_offer(html, page_url=selectedVariantUrl).price, 24.9)

    def test_invalid_selected_price_does_not_use_another_variant(self) -> None:
        for price in (0, -1, True, 'NaN', 'invalid'):
            with self.subTest(price=price):
                html = _schema_html([_group([_variant(price=price), _variant(size='004')])])
                self.assertIsNone(extract_offer(html, page_url=selectedVariantUrl).price)

    def test_malformed_variants_and_other_domains_are_ignored(self) -> None:
        for variants in (None, [], 'invalid', [None, 42], [{'@type': 'Offer', 'price': 10}]):
            with self.subTest(variants=variants):
                self.assertIsNone(extract_offer(_schema_html([_group(variants)]), page_url=selectedVariantUrl).price)
        variant = _variant()
        variant['offers']['url'] = selectedVariantUrl.replace('www.uniqlo.com', 'example.test')
        self.assertIsNone(extract_offer(_schema_html([_group([variant])]), page_url=selectedVariantUrl).price)

    def test_custom_selector_still_has_priority(self) -> None:
        html = '<span id="custom-price">29.9</span>' + _schema_html([_group([_variant()])])
        offer = extract_offer(html, '#custom-price', selectedVariantUrl)
        self.assertEqual((offer.price, offer.method), (29.9, 'selector'))

    def test_rendered_variant_stops_browser_retries(self) -> None:
        config = Config(
            TelegramConfig(False, '', ''), EmailConfig(False, '', 587, '', '', ''),
            ScraperConfig(1, 1, 'local-tests'),
        )
        html = _schema_html([_group([_variant(size='004', price='19.9'), _variant()])])
        with (
            patch('src.scraper._fetch_http_con_reintentos', return_value='<h1>Loading</h1>'),
            patch('src.scraper._fetch_playwright', return_value=html) as browser,
        ):
            offer = fetch_offer(selectedVariantUrl, None, config, expected_currency='USD')
        self.assertEqual((offer.price, offer.available, offer.method), (24.9, True, 'playwright_headless:json_ld'))
        browser.assert_called_once_with(selectedVariantUrl, config, headless=True)


if __name__ == '__main__':
    unittest.main()
