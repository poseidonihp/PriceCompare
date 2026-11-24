"""Regresiones del descuento que el script window.TDS de Tayrona calcula en el navegador."""
from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from src.config import Config, EmailConfig, ScraperConfig, TelegramConfig
from src.extractors import extract_offer
from src.scraper import fetch_offer


tayronaProductUrl = 'https://www.tayronaaccesorios.com/products/brazalete-tigre-rojo-volcan-8mm'
firstVariantId = '48841990471962'
secondVariantId = '48841990504730'
defaultVariants = {firstVariantId: [7900000, 0], secondVariantId: [9900000, 0]}
colombiaTimezone = timezone(timedelta(hours=-5))
basePrice = 79000
discountedPrice = 55300


def _rule(**overrides: object) -> dict:
    rule = {
        'type': 'all', 'tpl': 'default', 'cols': [], 'pct': 30, 'label': '-30%',
        'start': '', 'end': '', 'skipSale': False,
    }
    rule.update(overrides)
    return rule


def _tds_script(rules: object, variants: object = None, template: str = '') -> str:
    product = {'t': template, 'c': ['8mm-ellos', 'all'], 'v': defaultVariants if variants is None else variants}
    return (
        '<script>\nwindow.TDS = {\n  fmt: "${{amount_no_decimals_with_comma_separator}}",\n'
        f'  rules: {json.dumps(rules)}\n}};\nwindow.TDS.product = {json.dumps(product)};\n'
        '(function () { var T = window.TDS; if (!T.rules.length) return; })();\n</script>'
    )


def _variant_select(selected: str | None = firstVariantId) -> str:
    options = ''.join(
        f'<option value="{variant_id}"{" selected" if variant_id == selected else ""}>Talla</option>'
        for variant_id in (firstVariantId, secondVariantId)
    )
    return (
        '<div class="t4s-section-main-product"><form data-type="add-to-cart-form">'
        f'<select name="id">{options}</select></form></div>'
    )


def _page(script: str, select: str | None = None) -> str:
    return (
        '<head><meta property="og:price:amount" content="79.000">'
        '<meta property="og:price:currency" content="COP"></head>'
        f'<body><main>{_variant_select() if select is None else select}'
        f'<button>Agregar al carrito</button></main>{script}</body>'
    )


def _offer_price(html: str, **kwargs: object) -> tuple[float | None, str | None]:
    offer = extract_offer(html, page_url=tayronaProductUrl, expected_currency='COP', **kwargs)
    return offer.price, offer.method


class TdsDiscountTests(unittest.TestCase):
    """Comprueba que el precio coincide con el que la página muestra tras ejecutar el script."""

    def test_active_rule_discounts_selected_variant(self) -> None:
        offer = extract_offer(_page(_tds_script([_rule()])), page_url=tayronaProductUrl, expected_currency='COP')
        self.assertEqual(
            (offer.price, offer.available, offer.currency, offer.method),
            (discountedPrice, True, 'COP', 'tds'),
        )

    def test_rules_outside_dates_or_without_percentage_keep_base_price(self) -> None:
        for rule in (_rule(end='2020-01-31'), _rule(start='2999-01-01'), _rule(pct=0), _rule(pct='30')):
            with self.subTest(rule=rule):
                self.assertEqual(_offer_price(_page(_tds_script([rule]))), (basePrice, 'meta'))

    def test_date_limits_use_colombia_time_and_whole_end_day(self) -> None:
        rule = _rule(start='2026-11-28 10:00', end='2026-11-30')
        cases = (
            (datetime(2026, 11, 28, 9, 59, 59, tzinfo=colombiaTimezone), basePrice),
            (datetime(2026, 11, 28, 10, 0, tzinfo=colombiaTimezone), discountedPrice),
            (datetime(2026, 11, 30, 23, 59, 59, tzinfo=colombiaTimezone), discountedPrice),
            (datetime(2026, 12, 1, 0, 0, tzinfo=colombiaTimezone), basePrice),
        )
        for now, expected in cases:
            with self.subTest(now=now), patch('src.extractors._tds_now', return_value=now):
                self.assertEqual(_offer_price(_page(_tds_script([rule])))[0], expected)

    def test_unparseable_dates_do_not_limit_the_rule(self) -> None:
        rule = _rule(start='pronto', end='2026-11-30 10:00:00')
        self.assertEqual(_offer_price(_page(_tds_script([rule]))), (discountedPrice, 'tds'))

    def test_skip_sale_ignores_variants_with_compare_price(self) -> None:
        on_sale = {firstVariantId: [7900000, 9900000]}
        self.assertEqual(_offer_price(_page(_tds_script([_rule(skipSale=True)], on_sale)))[0], basePrice)
        self.assertEqual(_offer_price(_page(_tds_script([_rule()], on_sale)))[0], discountedPrice)

    def test_collection_and_template_rules(self) -> None:
        cases = (
            (_rule(type='col', cols=['8mm-ellos']), '', discountedPrice),
            (_rule(type='col', cols=['otra-coleccion']), '', basePrice),
            (_rule(type='tpl', tpl='default'), '', discountedPrice),
            (_rule(type='tpl', tpl='default'), 'promo', basePrice),
            (_rule(type='tpl', tpl='promo'), 'promo', discountedPrice),
            (_rule(type='desconocido'), '', basePrice),
        )
        for rule, template, expected in cases:
            with self.subTest(rule=rule, template=template):
                self.assertEqual(_offer_price(_page(_tds_script([rule], template=template)))[0], expected)

    def test_highest_matching_percentage_wins(self) -> None:
        rules = [_rule(pct=20), _rule(pct=30), _rule(type='col', cols=['otra-coleccion'], pct=50)]
        self.assertEqual(_offer_price(_page(_tds_script(rules)))[0], discountedPrice)

    def test_selected_option_determines_variant(self) -> None:
        cases = ((secondVariantId, 69300), (None, discountedPrice))
        for selected, expected in cases:
            with self.subTest(selected=selected):
                html = _page(_tds_script([_rule()]), _variant_select(selected))
                self.assertEqual(_offer_price(html)[0], expected)

    def test_missing_variant_field_uses_first_variant(self) -> None:
        html = _page(_tds_script([_rule()], {secondVariantId: [9900000, 0], firstVariantId: [7900000, 0]}), '')
        self.assertEqual(_offer_price(html)[0], 69300)

    def test_custom_selector_is_not_discounted_twice(self) -> None:
        html = _page(_tds_script([_rule()])) + '<span id="precio-rebajado">$55.300</span>'
        self.assertEqual(_offer_price(html, selector='#precio-rebajado'), (discountedPrice, 'selector'))
        self.assertEqual(_offer_price(html, selector='#no-existe'), (discountedPrice, 'tds'))

    def test_malformed_data_keeps_base_price(self) -> None:
        scripts = (
            '<script>window.TDS = { rules: [ {"pct": 30, </script>',
            '<script>window.TDS = { fmt: "" };</script>',
            _tds_script([_rule()], variants=[7900000, 0]),
            _tds_script([_rule()], variants={firstVariantId: [7900000]}),
            _tds_script([_rule()], variants={firstVariantId: ['79000', 0]}),
            _tds_script([_rule()], variants={'otra-variante': [7900000, 0]}),
            _tds_script(['regla', None, _rule(pct=100)]),
        )
        for script in scripts:
            with self.subTest(script=script):
                self.assertEqual(_offer_price(_page(script)), (basePrice, 'meta'))

    def test_rounding_matches_javascript(self) -> None:
        html = _page(_tds_script([_rule(pct=50)], {firstVariantId: [1001, 0]}))
        self.assertEqual(_offer_price(html)[0], 5.01)

    def test_unmatched_size_variant_gets_no_price(self) -> None:
        self.assertEqual(_offer_price(_page(_tds_script([_rule()])), variant='XL'), (None, None))

    def test_pages_without_script_keep_base_price(self) -> None:
        self.assertEqual(_offer_price(_page('<script>var rules = [1];</script>')), (basePrice, 'meta'))

    def test_http_page_reports_tds_without_browser(self) -> None:
        config = Config(
            TelegramConfig(False, '', ''), EmailConfig(False, '', 587, '', '', ''),
            ScraperConfig(1, 1, 'local-tests'),
        )
        with (
            patch('src.scraper._fetch_http_con_reintentos', return_value=_page(_tds_script([_rule()]))),
            patch('src.scraper._fetch_playwright') as browser,
        ):
            offer = fetch_offer(tayronaProductUrl, None, config, expected_currency='COP')
        self.assertEqual((offer.price, offer.available, offer.method), (discountedPrice, True, 'http:tds'))
        browser.assert_not_called()


if __name__ == '__main__':
    unittest.main()
