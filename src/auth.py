"""Autenticación del dashboard: hash de contraseña, cookie de sesión firmada y límite de intentos.

Solo usa la biblioteca estándar para no sumar dependencias al despliegue en el mini PC.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import secrets
import threading
import time

HASH_SCHEME = "scrypt"
# Parámetros scrypt: ~16 MB de memoria por intento, dificulta la fuerza bruta offline
SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_KEY_BYTES = 32
SALT_BYTES = 16
SESSION_SECRET_BYTES = 32
MAX_FAILED_ATTEMPTS = 5
LOCKOUT_MINUTES = 15
LOCKOUT_SECONDS = LOCKOUT_MINUTES * 60
SECONDS_PER_DAY = 24 * 60 * 60


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int, key_bytes: int) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=key_bytes)


def hash_password(password: str) -> str:
    """Genera el hash scrypt con sal aleatoria.

    Formato: `scrypt$n$r$p$sal$clave`, con sal y clave en base64url.
    """
    salt = secrets.token_bytes(SALT_BYTES)
    key = _scrypt(password, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P, SCRYPT_KEY_BYTES)
    return "$".join([HASH_SCHEME, str(SCRYPT_N), str(SCRYPT_R), str(SCRYPT_P), _b64encode(salt), _b64encode(key)])


def verify_password(password: str, stored_hash: str) -> bool:
    """Compara la contraseña con el hash guardado en tiempo constante.

    Un hash vacío o mal formado nunca valida.
    """
    try:
        scheme, n, r, p, salt, expected = stored_hash.split("$")
        expected_key = _b64decode(expected)
        if scheme != HASH_SCHEME or not expected_key:
            return False
        key = _scrypt(password, _b64decode(salt), int(n), int(r), int(p), len(expected_key))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(key, expected_key)


def new_session_secret() -> str:
    """Genera una clave aleatoria para firmar las cookies de sesión."""
    return secrets.token_urlsafe(SESSION_SECRET_BYTES)


def _sign(payload: str, secret: str) -> bytes:
    digest = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).digest()
    return _b64encode(digest).encode("ascii")


def create_session_token(username: str, secret: str, days: int, now: float | None = None) -> str:
    """Firma con HMAC-SHA256 un token con usuario y vencimiento.

    :param now: instante de referencia en segundos epoch; por defecto, la hora actual.
    """
    current = time.time() if now is None else now
    body = {"u": username, "exp": int(current + days * SECONDS_PER_DAY)}
    payload = _b64encode(json.dumps(body, separators=(",", ":")).encode("utf-8"))
    return f"{payload}.{_sign(payload, secret).decode('ascii')}"


def read_session_token(token: str | None, secret: str, now: float | None = None) -> str | None:
    """Devuelve el usuario del token si la firma es válida y no venció; si no, None."""
    if not token or not secret:
        return None
    payload, _, signature = token.partition(".")
    if not hmac.compare_digest(signature.encode("utf-8"), _sign(payload, secret)):
        return None
    try:
        body = json.loads(_b64decode(payload))
        username, expires = body["u"], float(body["exp"])
    except (ValueError, KeyError, TypeError):
        return None
    current = time.time() if now is None else now
    if not isinstance(username, str) or expires <= current:
        return None
    return username


class LoginRateLimiter:
    """Bloquea temporalmente a un cliente tras varios intentos de login fallidos seguidos."""

    def __init__(self, max_failures: int = MAX_FAILED_ATTEMPTS, lockout_seconds: float = LOCKOUT_SECONDS) -> None:
        self._max_failures = max_failures
        self._lockout_seconds = lockout_seconds
        self._failures: dict[str, int] = {}
        self._blocked_until: dict[str, float] = {}
        self._lock = threading.Lock()

    def blocked_seconds(self, client: str, now: float | None = None) -> int:
        """Segundos de bloqueo restantes para el cliente; 0 si puede intentar."""
        current = time.time() if now is None else now
        with self._lock:
            remaining = self._blocked_until.get(client, 0.0) - current
        return max(0, math.ceil(remaining))

    def register_failure(self, client: str, now: float | None = None) -> None:
        """Cuenta un fallo; al llegar al máximo bloquea al cliente y reinicia el conteo."""
        current = time.time() if now is None else now
        with self._lock:
            failures = self._failures.get(client, 0) + 1
            if failures >= self._max_failures:
                self._blocked_until[client] = current + self._lockout_seconds
                failures = 0
            self._failures[client] = failures

    def reset(self, client: str) -> None:
        """Olvida fallos y bloqueos del cliente tras un login exitoso."""
        with self._lock:
            self._failures.pop(client, None)
            self._blocked_until.pop(client, None)
