"""Pruebas del login del dashboard: helpers, configuración y un servidor uvicorn real local."""
from __future__ import annotations

import json
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import requests
import uvicorn

from src import auth, config, dashboard
from src.config import DashboardConfig

PASSWORD = 'clave-de-prueba'
SECRET = 'secreto-de-prueba'
SERVER_START_TIMEOUT_SECONDS = 10


class AuthHelpersTests(unittest.TestCase):
    def test_password_hash_roundtrip_and_malformed_hashes(self) -> None:
        stored = auth.hash_password(PASSWORD)
        self.assertTrue(stored.startswith('scrypt$'))
        self.assertTrue(auth.verify_password(PASSWORD, stored))
        self.assertFalse(auth.verify_password('otra-clave', stored))
        self.assertNotEqual(stored, auth.hash_password(PASSWORD))
        for malformed in ('', 'basura', 'scrypt$1$1$1$$', 'md5$16384$8$1$c2Fs$a2V5', 'scrypt$x$8$1$c2Fs$a2V5'):
            self.assertFalse(auth.verify_password(PASSWORD, malformed), malformed)

    def test_session_token_rejects_tampering_and_expiry(self) -> None:
        token = auth.create_session_token('admin', SECRET, days=1, now=1000)
        self.assertEqual(auth.read_session_token(token, SECRET, now=1000), 'admin')
        self.assertIsNone(auth.read_session_token(token, 'otro-secreto', now=1000))
        self.assertIsNone(auth.read_session_token(token, SECRET, now=1000 + 2 * auth.SECONDS_PER_DAY))
        forged_payload = auth._b64encode(json.dumps({'u': 'admin', 'exp': 10 ** 12}).encode())
        self.assertIsNone(auth.read_session_token(forged_payload + '.' + token.split('.')[1], SECRET, now=1000))
        for invalid in (None, '', 'sin-punto', token + 'x', 'ñ.ñ', '.'):
            self.assertIsNone(auth.read_session_token(invalid, SECRET, now=1000), invalid)
        self.assertIsNone(auth.read_session_token(token, '', now=1000))

    def test_rate_limiter_blocks_after_max_failures_and_resets(self) -> None:
        limiter = auth.LoginRateLimiter(max_failures=3, lockout_seconds=60)
        limiter.register_failure('1.1.1.1', now=0)
        limiter.register_failure('1.1.1.1', now=1)
        self.assertEqual(limiter.blocked_seconds('1.1.1.1', now=2), 0)
        limiter.register_failure('1.1.1.1', now=2)
        self.assertEqual(limiter.blocked_seconds('1.1.1.1', now=2), 60)
        self.assertEqual(limiter.blocked_seconds('2.2.2.2', now=2), 0)
        self.assertEqual(limiter.blocked_seconds('1.1.1.1', now=63), 0)
        limiter.register_failure('1.1.1.1', now=70)
        limiter.reset('1.1.1.1')
        limiter.register_failure('1.1.1.1', now=71)
        limiter.register_failure('1.1.1.1', now=72)
        self.assertEqual(limiter.blocked_seconds('1.1.1.1', now=72), 0)


class DashboardConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self._config_path = Path(tmp.name) / 'config.json'
        self._others = {'telegram': {'enabled': False}, 'email': {'enabled': False}, 'scraper': {'max_workers': 1}}
        self._config_path.write_text(json.dumps(self._others), encoding='utf-8')
        path_patch = patch('src.config.CONFIG_PATH', self._config_path)
        path_patch.start()
        self.addCleanup(path_patch.stop)

    def test_missing_section_uses_defaults_and_is_not_configured(self) -> None:
        settings = config.load_dashboard_config()
        self.assertEqual((settings.host, settings.port, settings.session_days), ('127.0.0.1', 8000, 30))
        self.assertFalse(settings.configured)

    def test_example_config_loads_unconfigured(self) -> None:
        with patch('src.config.CONFIG_PATH', config.ROOT / 'config.example.json'):
            settings = config.load_dashboard_config()
        self.assertEqual(settings.port, 8000)
        self.assertFalse(settings.configured)

    def test_saving_credentials_keeps_other_sections(self) -> None:
        stored = auth.hash_password(PASSWORD)
        config.save_dashboard_credentials('julian', stored, 'secreto-1')
        raw = json.loads(self._config_path.read_text(encoding='utf-8'))
        self.assertEqual({key: raw[key] for key in self._others}, self._others)
        settings = config.load_dashboard_config()
        self.assertTrue(settings.configured)
        self.assertEqual(settings.username, 'julian')
        self.assertTrue(auth.verify_password(PASSWORD, settings.password_hash))
        config.save_dashboard_credentials('julian', stored, 'secreto-2')
        self.assertEqual(config.load_dashboard_config().session_secret, 'secreto-2')
        self.assertFalse(self._config_path.with_suffix('.json.tmp').exists())


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


class DashboardLoginServerTests(unittest.TestCase):
    """Levanta el dashboard real con datos temporales y lo consulta por HTTP."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self._settings = DashboardConfig(
            username='admin', password_hash=auth.hash_password(PASSWORD), session_secret=SECRET,
        )
        for target, value in (
            ('src.dashboard.load_dashboard_config', lambda: self._settings),
            ('src.dashboard.load_products', lambda: []),
            ('src.dashboard.DB_PATH', Path(tmp.name) / 'prices.db'),
            ('src.dashboard._login_limiter', auth.LoginRateLimiter()),
        ):
            active = patch(target, value)
            active.start()
            self.addCleanup(active.stop)
        port = _free_port()
        self._base = f'http://127.0.0.1:{port}'
        server = uvicorn.Server(uvicorn.Config(
            dashboard.app, host='127.0.0.1', port=port, log_config=None, access_log=False,
        ))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        self.addCleanup(thread.join, SERVER_START_TIMEOUT_SECONDS)
        self.addCleanup(setattr, server, 'should_exit', True)
        deadline = time.monotonic() + SERVER_START_TIMEOUT_SECONDS
        while not server.started:
            if time.monotonic() > deadline or not thread.is_alive():
                self.fail('El servidor del dashboard no arrancó')
            time.sleep(0.05)

    def _login(self, session: requests.Session, password: str = PASSWORD, **headers: str) -> requests.Response:
        return session.post(
            self._base + '/api/login', json={'username': 'admin', 'password': password}, headers=headers,
        )

    def test_protected_routes_require_session(self) -> None:
        self.assertEqual(requests.get(self._base + '/api/products').status_code, 401)
        self.assertEqual(requests.delete(self._base + '/api/products/sample').status_code, 401)
        root = requests.get(self._base + '/', allow_redirects=False)
        self.assertEqual(root.status_code, 303)
        self.assertEqual(root.headers['location'], '/login')
        login_page = requests.get(self._base + '/login')
        self.assertEqual(login_page.status_code, 200)
        self.assertIn('id="login-form"', login_page.text)

    def test_login_logout_cycle(self) -> None:
        session = requests.Session()
        with self.assertLogs('priceCompare.dashboard', 'WARNING'):
            self.assertEqual(self._login(session, 'incorrecta').status_code, 401)
        self.assertNotIn(dashboard.SESSION_COOKIE, session.cookies)
        response = self._login(session)
        self.assertEqual(response.status_code, 200)
        set_cookie = response.headers['set-cookie']
        self.assertIn('HttpOnly', set_cookie)
        self.assertIn('SameSite=lax', set_cookie)
        self.assertNotIn('Secure', set_cookie)
        self.assertEqual(session.get(self._base + '/api/products').json(), [])
        self.assertEqual(session.get(self._base + '/').status_code, 200)
        self.assertEqual(session.post(self._base + '/api/logout').status_code, 200)
        self.assertEqual(session.get(self._base + '/api/products').status_code, 401)

    def test_cookie_is_secure_behind_https_proxy(self) -> None:
        response = self._login(requests.Session(), **{'X-Forwarded-Proto': 'https', 'X-Forwarded-For': '203.0.113.9'})
        self.assertEqual(response.status_code, 200)
        self.assertIn('Secure', response.headers['set-cookie'])

    def test_tampered_or_foreign_cookie_is_rejected(self) -> None:
        foreign = auth.create_session_token('admin', 'otro-secreto', days=1)
        for value in ('x.y', foreign, auth.create_session_token('intruso', SECRET, days=1)):
            response = requests.get(self._base + '/api/products', cookies={dashboard.SESSION_COOKIE: value})
            self.assertEqual(response.status_code, 401, value)

    def test_lockout_after_repeated_failures(self) -> None:
        session = requests.Session()
        with self.assertLogs('priceCompare.dashboard', 'WARNING') as logs:
            for _attempt in range(auth.MAX_FAILED_ATTEMPTS):
                self.assertEqual(self._login(session, 'incorrecta').status_code, 401)
            blocked = self._login(session)
        self.assertEqual(blocked.status_code, 429)
        self.assertGreater(int(blocked.headers['retry-after']), 0)
        self.assertNotIn(dashboard.SESSION_COOKIE, session.cookies)
        self.assertTrue(any('bloqueada' in line for line in logs.output))
        self.assertFalse(any(PASSWORD in line or 'incorrecta' in line for line in logs.output))

    def test_fails_closed_without_configured_password(self) -> None:
        self._settings = DashboardConfig()
        self.assertEqual(requests.get(self._base + '/api/products').status_code, 503)
        self.assertEqual(requests.get(self._base + '/', allow_redirects=False).status_code, 503)
        self.assertEqual(self._login(requests.Session()).status_code, 503)


if __name__ == '__main__':
    unittest.main()
