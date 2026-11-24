"""Pruebas del dashboard en Chromium con respuestas locales y sin escrituras reales."""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import Route, sync_playwright

from src.dashboard import _DASHBOARD_HTML, _LOGIN_HTML


class DashboardBrowserTests(unittest.TestCase):
    def setUp(self) -> None:
        self._playwright = sync_playwright().start()
        self.addCleanup(self._playwright.stop)
        if not Path(self._playwright.chromium.executable_path).exists():
            self.skipTest('Chromium no está instalado: playwright install chromium')
        self._browser = self._playwright.chromium.launch(headless=True)
        self.addCleanup(self._browser.close)
        self._page = self._browser.new_page()
        self._errors: list[str] = []
        self._page.on('pageerror', lambda error: self._errors.append(str(error)))
        self._name = '<img src=x onerror="window.injected=true"> Producto de prueba'
        self._product_id = 'sample\'"<&'
        self._product = {
            'id': self._product_id, 'name': self._name, 'url': 'javascript:window.injected=true',
            'currency': 'COP', 'initial_price': 100, 'last_price': 90, 'min_price': 80,
            'min_price_date': '2020-01-01', 'target_price': 75, 'available': True,
            'purchased': False, 'active': True, 'selector': None,
            'history': [{'date': '2020-01-01', 'price': 100}, {'date': '2020-01-02', 'price': 90}],
            'last_success_at': '2020-01-01T12:00:00+00:00',
            'last_attempt_at': '2020-01-03T00:00:00+00:00',
            'last_status': 'rejected', 'last_error': '<b>Precio sospechoso</b>', 'is_stale': True,
        }
        self._requests: list[tuple[str, str]] = []
        self._page.route('**/*', self._respond)
        self._page.goto('http://dashboard.test/')
        self._page.locator('.card').wait_for()

    def _respond(self, route: Route) -> None:
        path = urlsplit(route.request.url).path
        self._requests.append((route.request.method, path))
        if path == '/':
            route.fulfill(content_type='text/html', body=_DASHBOARD_HTML)
            return
        if path == '/login':
            route.fulfill(content_type='text/html', body=_LOGIN_HTML)
            return
        if path.endswith('.js'):
            route.fulfill(content_type='application/javascript', body='window.Chart = class { destroy() {} };')
            return
        responses = {
            '/api/products': [self._product],
            '/api/estadisticas': {
                'top_bajadas_30d': [{'id': self._product_id, 'name': self._name, 'currency': 'COP', 'last_price': 90, 'pct': -10}],
                'cerca_del_target': [],
            },
            '/api/huerfanos': [{'id': 'orphan', 'name': self._name, 'registros': 1, 'ultima_captura': '2020-01-01'}],
        }
        if path.endswith('/changes'):
            payload = {'currency': 'COP', 'changes': [{'date': '2020-01-01', 'price': 100}, {'date': '2020-01-02', 'price': 90}]}
        else:
            payload = responses.get(path, {})
        route.fulfill(content_type='application/json', body=json.dumps(payload))

    def test_untrusted_text_and_urls_are_inert(self) -> None:
        self.assertEqual(self._page.locator('.card .name').inner_text(), self._name)
        self.assertEqual(self._page.locator('.card .name').get_attribute('href'), '#')
        self.assertEqual(self._page.locator('.card').get_attribute('data-id'), self._product_id)
        self.assertEqual(self._page.locator('#grid img, #tendencias img, #huerfanos img').count(), 0)
        self.assertIsNone(self._page.evaluate('window.injected'))
        self.assertEqual(self._errors, [])

    def test_real_capture_dates_and_rejection_are_visible(self) -> None:
        card = self._page.locator('.card')
        self.assertIn('Precio desactualizado', card.inner_text())
        self.assertIn('Precio rechazado', card.inner_text())
        self.assertIn('<b>Precio sospechoso</b>', card.inner_text())
        self.assertIn('2020', self._page.locator('#last-update').inner_text())
        self.assertIn('Captura más reciente', self._page.locator('#last-update').inner_text())
        self.assertEqual(self._errors, [])

    def test_product_actions_preserve_ids_and_open_dialogs(self) -> None:
        self._page.locator('.card [data-action="history"]').click()
        self._page.locator('#modal-body table').wait_for()
        self.assertIn(self._name, self._page.locator('#modal-title').inner_text())
        self._page.keyboard.press('Escape')
        self._page.locator('.card [data-action="edit"]').click()
        self.assertEqual(self._page.locator('#f-id').input_value(), self._product_id)
        self.assertEqual(self._page.locator('#f-name').input_value(), self._name)
        self._page.keyboard.press('Escape')
        self._page.locator('.card [data-action="active"]').click()
        self._page.wait_for_function('document.querySelector(".card") !== null')
        self.assertTrue(any(method == 'PATCH' and path.endswith('/active') for method, path in self._requests))
        self.assertEqual(self._errors, [])

    def test_logout_clears_session_and_shows_login(self) -> None:
        self._page.locator('#btn-logout').click()
        self._page.wait_for_url('**/login')
        self._page.locator('#login-form').wait_for()
        self.assertIn(('POST', '/api/logout'), self._requests)
        self.assertEqual(self._errors, [])

    def test_expired_session_redirects_to_login_without_alerts(self) -> None:
        page = self._browser.new_page()
        dialogs: list[str] = []
        page.on('dialog', lambda dialog: (dialogs.append(dialog.message), dialog.dismiss()))

        def respond_expired(route: Route) -> None:
            path = urlsplit(route.request.url).path
            if path == '/':
                route.fulfill(content_type='text/html', body=_DASHBOARD_HTML)
            elif path == '/login':
                route.fulfill(content_type='text/html', body=_LOGIN_HTML)
            elif path.endswith('.js'):
                route.fulfill(content_type='application/javascript', body='window.Chart = class { destroy() {} };')
            else:
                route.fulfill(status=401, content_type='application/json', body='{"error": "sesión requerida"}')

        page.route('**/*', respond_expired)
        page.goto('http://dashboard.test/')
        page.wait_for_url('**/login')
        page.locator('#login-form').wait_for()
        self.assertEqual(dialogs, [])


if __name__ == '__main__':
    unittest.main()
