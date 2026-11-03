"""Regresiones de extracción, alertas, persistencia y notificaciones sin servicios externos."""
from __future__ import annotations

import json
import math
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import requests
from bs4 import BeautifulSoup

from src import dashboard, main, reporter, scraper
from src.config import Config, EmailConfig, Product, ScraperConfig, TelegramConfig
from src.extractors import ExtractedOffer, ExtractionError, _normalize_price, extract_offer, validate_selector
from src.notifier import send_telegram
from src.storage import Storage


def _product(product_id: str = 'sample', **values: object) -> Product:
    return Product.from_dict({
        'id': product_id, 'name': product_id, 'url': 'https://example.test/product',
        'currency': 'COP', **values,
    })


def _config() -> Config:
    return Config(
        TelegramConfig(False, '', ''), EmailConfig(False, '', 587, '', '', ''),
        ScraperConfig(1, 1, 'local-tests', max_workers=2),
    )


def _schema_html(data: object) -> str:
    return '<script type="application/ld+json">' + json.dumps(data) + '</script>'


class ExtractionTests(unittest.TestCase):
    def test_price_formats_and_installments(self) -> None:
        cases = {
            '1.234,56': 1234.56,
            '$1,234.56 USD': 1234.56,
            '1599900': 1599900,
            '1.599.900 1.599.900': 1599900,
            'Antes 1.999.900 Ahora 1.599.900': 1599900,
            'Precio $1.599.900 - 12 cuotas de $133.325': 1599900,
            '12 cuotas de $133.325': None,
            '$100 $80': None,
            '-100': None,
            '0': None,
            'sin precio': None,
            '1' * 10000: None,
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw[:80]):
                self.assertEqual(_normalize_price(raw), expected)

    def test_primary_product_selected_by_url(self) -> None:
        html = _schema_html({'@graph': [
            {'@type': 'Product', 'url': 'https://example.test/accessory', 'offers': {'price': 50}},
            {'@type': 'Product', 'url': 'https://example.test/product', 'offers': {'price': 1500}},
        ]})
        self.assertEqual(extract_offer(html, page_url='https://example.test/product').price, 1500)

    def test_ambiguous_products_are_not_compared(self) -> None:
        html = _schema_html([
            {'@type': 'Product', 'offers': {'price': 1500}},
            {'@type': 'Product', 'offers': {'price': 50}},
        ])
        self.assertIsNone(extract_offer(html).price)

    def test_related_products_and_shipping_do_not_supply_prices(self) -> None:
        html = _schema_html({
            '@type': 'Product', 'offers': {'price': 1500, 'shippingDetails': {'price': 10}},
            'isRelatedTo': {'@type': 'Product', 'offers': {'price': 50}},
        })
        self.assertEqual(extract_offer(html).price, 1500)

    def test_offer_price_stock_and_currency_stay_together(self) -> None:
        html = _schema_html({'@type': 'Product', 'offers': [
            {'price': 1500, 'availability': 'https://schema.org/InStock', 'priceCurrency': 'COP'},
            {'price': 900, 'availability': 'https://schema.org/OutOfStock', 'priceCurrency': 'COP'},
            {'price': 1, 'availability': 'https://schema.org/InStock', 'priceCurrency': 'USD'},
        ]})
        offer = extract_offer(html, expected_currency='COP')
        self.assertEqual((offer.price, offer.available, offer.currency), (1500, True, 'COP'))

    def test_all_out_of_stock_offers_stay_unavailable(self) -> None:
        html = _schema_html({'@type': 'Product', 'offers': {'price': 900, 'availability': 'OutOfStock'}})
        offer = extract_offer(html)
        self.assertEqual((offer.price, offer.available), (900, False))

    def test_invalid_json_prices_are_rejected(self) -> None:
        for value in (0, -1, True, math.inf, math.nan, '0', '-100'):
            with self.subTest(value=value):
                self.assertIsNone(extract_offer(_schema_html({'@type': 'Product', 'offers': {'price': value}})).price)

    def test_lowercase_product_schema_and_referenced_offer(self) -> None:
        html = _schema_html({'@graph': [
            {'@type': 'product', 'offers': {'@id': '#offer'}},
            {'@id': '#offer', '@type': 'Offer', 'price': '6999900', 'priceCurrency': 'COP'},
        ]})
        self.assertEqual(extract_offer(html).price, 6999900)

    def test_metadata_hidden_from_screen_reader_remains_valid(self) -> None:
        html = '<main><meta itemprop="priceCurrency" content="COP"><span aria-hidden="true"><meta itemprop="price" content="99900"></span></main>'
        offer = extract_offer(html)
        self.assertEqual((offer.price, offer.currency), (99900, 'COP'))

    def test_disabled_and_hidden_buttons_do_not_mark_stock(self) -> None:
        for attributes in ('disabled', 'aria-disabled="true"', 'hidden', 'style="display:none"'):
            with self.subTest(attributes=attributes):
                html = '<meta itemprop="price" content="100"><main><button ' + attributes + '>Comprar</button><p>Agotado</p></main>'
                self.assertIs(extract_offer(html).available, False)

    def test_related_buy_button_is_ignored(self) -> None:
        html = '<main><meta itemprop="price" content="100"><button disabled>Comprar</button><section class="related-products"><button>Comprar</button></section></main>'
        self.assertIs(extract_offer(html).available, False)

    def test_metadata_stock_and_currency_are_scoped_to_offer(self) -> None:
        html = '<main><div itemtype="https://schema.org/Offer"><meta itemprop="price" content="100"><meta itemprop="priceCurrency" content="COP"><link itemprop="availability" href="https://schema.org/OutOfStock"></div><div class="related"><meta itemprop="priceCurrency" content="USD"><button>Comprar</button></div></main>'
        offer = extract_offer(html)
        self.assertEqual((offer.price, offer.available, offer.currency), (100, False, 'COP'))

    def test_regex_ignores_related_prices_and_installments(self) -> None:
        html = '<main><p>Precio $1500 - 12 cuotas de $125</p><section class="related"><p>Precio $50</p></section></main>'
        self.assertEqual(extract_offer(html).price, 1500)

    def test_selector_priority_and_content_attribute(self) -> None:
        html = '<meta id="chosen" content="1200">' + _schema_html({'@type': 'Product', 'offers': {'price': 1500}})
        self.assertEqual(extract_offer(html, '#chosen').price, 1200)

    def test_invalid_selector_is_rejected_before_network(self) -> None:
        with patch('src.scraper._fetch_http_con_reintentos') as fetch:
            with self.assertRaises(ExtractionError):
                scraper.fetch_offer('https://example.test', '[', _config())
            fetch.assert_not_called()
        validate_selector(None)
        validate_selector('.valid-price')


class PipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self._root = Path(temporary.name)
        self._storage = Storage(self._root / 'prices.db')
        self.addCleanup(self._storage.close)
        self._config = _config()

    def test_price_sequence_and_immutable_baseline(self) -> None:
        product = _product()
        prices = [100, 90, 90, 90, 89, 80, 100, 95, 120, 110]
        expected = [False, True, False, False, True, True, False, True, False, True]
        actual = []
        for price in prices:
            drop, *_ = main._process_product(product, price, True, self._storage, self._config, False)
            actual.append(drop is not None)
        self.assertEqual(actual, expected)
        self.assertEqual(self._storage.get_product(product.id).initial_price, 100)
        self.assertEqual(len(self._storage.price_history(product.id)), len(prices))

    def test_invalid_first_price_does_not_create_baseline(self) -> None:
        for value in (0, -1, math.nan, math.inf):
            with self.subTest(value=value):
                result = main._accept_offer(_product(), ExtractedOffer(value), self._storage, self._config, False)
                self.assertTrue(result[-1])
                self.assertIsNone(self._storage.get_product('sample'))
                self.assertEqual(self._storage.capture_status('sample').last_status, 'rejected')

    def test_suspicious_price_and_wrong_currency_preserve_history(self) -> None:
        product = _product()
        main._accept_offer(product, ExtractedOffer(100, True, 'COP', 'http:meta'), self._storage, self._config, False)
        last_success = self._storage.capture_status(product.id).last_success_at
        for offer in (ExtractedOffer(1, True, 'COP'), ExtractedOffer(80, True, 'USD')):
            self.assertTrue(main._accept_offer(product, offer, self._storage, self._config, False)[-1])
            self.assertEqual(self._storage.last_price(product.id), 100)
            self.assertEqual(self._storage.capture_status(product.id).last_success_at, last_success)

    def test_out_of_stock_product_never_wins_group(self) -> None:
        parent, other = _product('group', grupo='group'), _product('other', grupo='group')
        for product in (parent, other):
            main._process_product(product, 100, True, self._storage, self._config, False)
        results = {}
        for product, price, available in ((parent, 100, True), (other, 70, False)):
            _, _, previous, current = main._process_product(product, price, available, self._storage, self._config, False)
            results[product.id] = (previous, current)
        self.assertIsNone(main._process_grupo('group', [parent, other], results))
        self.assertEqual(self._storage.last_price(other.id), 70)

    def test_first_out_of_stock_capture_is_excluded_from_groups(self) -> None:
        result = main._process_product(_product(), 70, False, self._storage, self._config, False)
        self.assertIsNone(result[-1])
        self.assertEqual(self._storage.last_price('sample'), 70)

    def test_groups_do_not_repeat_unchanged_alerts(self) -> None:
        parent, other = _product('group', grupo='group'), _product('other', grupo='group')
        unchanged = {'group': (100, 100), 'other': (90, 90)}
        self.assertIsNone(main._process_grupo('group', [parent, other], unchanged))
        dropped = {'group': (100, 100), 'other': (90, 80)}
        self.assertIsNotNone(main._process_grupo('group', [parent, other], dropped))
        first_capture = {'group': (None, 100), 'other': (None, 80)}
        self.assertIsNone(main._process_grupo('group', [parent, other], first_capture))

    def test_restock_and_purchased_behavior_remain(self) -> None:
        product = _product()
        main._process_product(product, 100, False, self._storage, self._config, False)
        drop, restock, *_ = main._process_product(product, 100, True, self._storage, self._config, False)
        self.assertIsNone(drop)
        self.assertIsNotNone(restock)
        self._storage.set_purchased(product.id, True)
        result = main._process_product(product, 90, True, self._storage, self._config, False)
        self.assertEqual(result, (None, None, None, None))
        self.assertEqual(self._storage.last_price(product.id), 90)
        self.assertTrue(main._accept_offer(product, ExtractedOffer(1), self._storage, self._config, False)[-1])

    def test_invalid_selector_does_not_abort_other_products(self) -> None:
        products = [_product('bad', selector='['), _product('good')]
        with patch('src.scraper._fetch_http_con_reintentos', return_value='<meta itemprop="price" content="100">'):
            result = main._scrape_all(products, self._storage, self._config, None, False)
        self.assertTrue(result[-1])
        self.assertEqual(self._storage.capture_status('bad').last_status, 'error')
        self.assertEqual(self._storage.last_price('good'), 100)
        self.assertEqual(self._storage.capture_status('good').extraction_method, 'http:meta')

    def test_dry_run_does_not_write_attempt_or_history(self) -> None:
        with patch('src.main.fetch_offer', return_value=ExtractedOffer(100, True)):
            main._scrape_all([_product()], self._storage, self._config, None, True)
        self.assertIsNone(self._storage.get_product('sample'))
        self.assertIsNone(self._storage.capture_status('sample').last_attempt_at)

    def test_cli_rejects_invalid_selector_without_writing(self) -> None:
        from scripts.add_product import main as add_product

        with patch('src.config.PRODUCTS_PATH', self._root / 'products.json'):
            args = ['add_product.py', 'sample', 'Sample', 'https://example.test', '--selector', '[']
            with patch('sys.argv', args), patch('builtins.print'):
                self.assertEqual(add_product(), 1)
            self.assertFalse((self._root / 'products.json').exists())

    def test_legacy_database_keeps_prices_and_gets_attempt_table(self) -> None:
        main._process_product(_product(), 100, True, self._storage, self._config, False)
        initial = self._storage.get_product('sample')
        self._storage.conn.execute('DROP TABLE scrape_status')
        self._storage.conn.commit()
        upgraded = Storage(self._root / 'prices.db')
        try:
            self.assertEqual(upgraded.get_product('sample'), initial)
            self.assertIsNotNone(upgraded.capture_status('sample').last_success_at)
            upgraded.record_scrape_result('sample', 'error', 'Prueba')
            self.assertIsNotNone(upgraded.capture_status('sample').last_success_at)
        finally:
            upgraded.close()

    def test_dashboard_shows_stale_capture_and_latest_error(self) -> None:
        main._process_product(_product(), 100, True, self._storage, self._config, False)
        old_capture = '2020-01-01T00:00:00+00:00'
        self._storage.conn.execute('UPDATE price_history SET captured_at = ?', (old_capture,))
        self._storage.conn.commit()
        self._storage.record_scrape_result('sample', 'error', 'Falló la consulta.')
        result = dashboard._capture_details(self._storage, 'sample')
        self.assertTrue(result['is_stale'])
        self.assertEqual(result['last_success_at'], old_capture)
        self.assertEqual(result['last_status'], 'error')
        self.assertIsNotNone(result['last_attempt_at'])

    def test_dashboard_validates_selector_and_url_without_saving(self) -> None:
        with patch('src.dashboard.load_products', return_value=[]), patch('src.dashboard.save_products') as save:
            for url, selector in (('https://example.test', '['), ('javascript:alert(1)', None)):
                payload = dashboard.ProductCreatePayload(id='sample', name='Sample', url=url, selector=selector)
                self.assertEqual(dashboard.api_create_product(payload).status_code, 400)
            save.assert_not_called()

    def test_dashboard_crud_preserves_baseline_and_history(self) -> None:
        catalog = self._root / 'products.json'
        catalog.write_text('[]', encoding='utf-8')
        with patch('src.config.PRODUCTS_PATH', catalog), patch('src.dashboard.DB_PATH', self._root / 'prices.db'):
            payload = dashboard.ProductCreatePayload(id='sample', name='Sample', url='https://example.test/product')
            self.assertEqual(dashboard.api_create_product(payload).status_code, 201)
            pending = json.loads(dashboard.api_products().body)[0]
            self.assertIsNone(pending['initial_price'])
            self.assertIsNone(pending['last_success_at'])
            self.assertEqual(dashboard.api_create_product(payload).status_code, 409)
            main._accept_offer(_product(), ExtractedOffer(100, True, 'COP', 'http:meta'), self._storage, self._config, False)
            initial = self._storage.get_product('sample')
            update = dashboard.ProductUpdatePayload(name='Updated', url='https://example.test/product', target_price='-20%')
            self.assertEqual(dashboard.api_update_product('sample', update).status_code, 200)
            stored = self._storage.get_product('sample')
            self.assertEqual((stored.initial_price, stored.initial_date), (initial.initial_price, initial.initial_date))
            self.assertEqual(stored.target_price, 80)
            self.assertEqual(dashboard.api_toggle_active('sample', dashboard.ActivePayload(active=False)).status_code, 200)
            self.assertEqual(dashboard.api_set_purchased('sample', dashboard.PurchasedPayload(purchased=True)).status_code, 200)
            current = json.loads(dashboard.api_products().body)[0]
            self.assertFalse(current['active'])
            self.assertTrue(current['purchased'])
            self.assertEqual(current['last_status'], 'success')
            self.assertEqual(len(json.loads(dashboard.api_history('sample').body)), 1)
            self.assertEqual(dashboard.api_changes('sample').status_code, 200)
            self.assertEqual(dashboard.api_estadisticas().status_code, 200)
            self.assertEqual(dashboard.api_delete_product('sample').status_code, 200)
            self.assertEqual(len(json.loads(dashboard.api_huerfanos().body)), 1)
            self.assertEqual(dashboard.api_purgar_huerfano('sample').status_code, 200)
            self.assertIsNone(self._storage.get_product('sample'))
            self.assertIsNone(self._storage.capture_status('sample').last_attempt_at)

    def test_run_generates_report_with_simulated_notifications(self) -> None:
        product = _product()
        main._process_product(product, 100, True, self._storage, self._config, False)
        args = type('Arguments', (), {'product': None, 'dry_run': False, 'cache_html': False})()
        with (
            patch('src.main._parse_args', return_value=args),
            patch('src.main._configure_logging'),
            patch('src.main.load_config', return_value=self._config),
            patch('src.main.load_products', return_value=[product]),
            patch('src.main.DB_PATH', self._root / 'prices.db'),
            patch('src.main.REPORTS_DIR', self._root / 'reports'),
            patch('src.main.fetch_offer', return_value=ExtractedOffer(90, True, 'COP', 'http:meta')),
            patch('src.main.send_telegram', return_value=True) as telegram,
            patch('src.main.send_email', return_value=True) as email,
        ):
            self.assertEqual(main.run(), 0)
            telegram.assert_called_once()
            email.assert_called_once()
            self.assertEqual(len(list((self._root / 'reports').glob('*.md'))), 1)
            telegram.reset_mock()
            email.reset_mock()
            self.assertEqual(main.run(), 0)
            telegram.assert_not_called()
            email.assert_not_called()


class NotificationTests(unittest.TestCase):
    def test_telegram_error_does_not_log_token(self) -> None:
        response = requests.Response()
        response.status_code = 401
        response.url = 'https://api.telegram.org/botFAKE_SECRET_MARKER/sendMessage'
        config = TelegramConfig(True, 'FAKE_SECRET_MARKER', 'fake-chat')
        with patch('src.notifier.requests.post', return_value=response), self.assertLogs('src.notifier', level='ERROR') as logs:
            self.assertFalse(send_telegram('Prueba local', config))
        self.assertNotIn(config.bot_token, '\n'.join(logs.output))
        self.assertIn('401', '\n'.join(logs.output))

    def test_html_report_escapes_names_currency_and_links(self) -> None:
        drop = reporter.PriceDrop('sample', '<img src=x onerror=alert(1)>', 'javascript:alert(1)', '<b>COP</b>', 100, 120, 110, None)
        html = reporter.build_html_email([drop], date(2026, 9, 15))
        soup = BeautifulSoup(html, 'lxml')
        self.assertIsNone(soup.find('img'))
        self.assertNotIn('javascript:', html)
        self.assertIn('&lt;img', html)
        self.assertIn('8.3%', html)
        self.assertNotIn('▼ -10.0%', html)

    def test_disabled_notifications_never_contact_services(self) -> None:
        with patch('src.notifier.requests.post') as post:
            self.assertFalse(send_telegram('Prueba', TelegramConfig(False, '', '')))
            post.assert_not_called()


if __name__ == '__main__':
    unittest.main()
