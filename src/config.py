from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"
PRODUCTS_PATH = ROOT / "products.json"
DB_PATH = ROOT / "data" / "prices.db"
REPORTS_DIR = ROOT / "reports"
LOGS_DIR = ROOT / "logs"
HTML_CACHE_DIR = ROOT / "data" / "html_cache"


@dataclass
class Product:
    id: str
    name: str
    url: str
    selector: str | None
    target_price: float | None
    currency: str
    active: bool
    grupo: str | None = None
    # Objetivo relativo: ej. "-20%" → 20% por debajo del initial_price
    target_price_relative: str | None = None
    # Talla JSON-LD a seguir cuando la URL publica un Product por talla
    variant: str | None = None

    @classmethod
    def from_dict(cls, raw: dict) -> "Product":
        target_raw = raw.get("target_price")
        target_price: float | None = None
        target_relative: str | None = None
        if isinstance(target_raw, str) and "%" in target_raw:
            target_relative = target_raw
        elif isinstance(target_raw, (int, float)):
            target_price = float(target_raw)
        return cls(
            id=raw["id"],
            name=raw["name"],
            url=raw["url"],
            selector=raw.get("selector"),
            target_price=target_price,
            target_price_relative=target_relative,
            currency=raw.get("currency", "USD"),
            active=raw.get("active", True),
            grupo=raw.get("grupo"),
            variant=raw.get("variant"),
        )


@dataclass
class TelegramConfig:
    enabled: bool
    bot_token: str
    chat_id: str


@dataclass
class EmailConfig:
    enabled: bool
    smtp_host: str
    smtp_port: int
    from_addr: str
    to_addr: str
    app_password: str


@dataclass
class ScraperConfig:
    http_timeout_seconds: int
    playwright_timeout_seconds: int
    user_agent: str
    cache_html: bool = False
    # Si el precio extraído cae fuera de [initial * min_ratio, initial * max_ratio], se considera extracción rota
    sanity_min_ratio: float = 0.25
    sanity_max_ratio: float = 4.0
    # Reintentos HTTP adicionales al primer intento ante fallos transitorios (timeout, 5xx, 429)
    http_retries: int = 2
    # Espera entre reintentos: base * 2**intento segundos
    http_backoff_base_seconds: float = 1.0
    # Aplica playwright-stealth al contexto de Playwright (kill-switch sin tocar código)
    stealth_enabled: bool = True
    # Workers concurrentes para el scraping; 1 = comportamiento secuencial
    max_workers: int = 4


@dataclass
class DashboardConfig:
    host: str = "127.0.0.1"
    port: int = 8000
    username: str = "admin"
    # Hash scrypt generado por scripts/set_dashboard_password.py; vacío = login sin configurar
    password_hash: str = ""
    # Clave HMAC de la cookie de sesión; cambiarla invalida las sesiones abiertas
    session_secret: str = ""
    session_days: int = 30

    @property
    def configured(self) -> bool:
        """Indica si hay contraseña y clave de sesión para habilitar el login."""
        return bool(self.password_hash and self.session_secret)


@dataclass
class Config:
    telegram: TelegramConfig
    email: EmailConfig
    scraper: ScraperConfig


def load_config() -> Config:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"No existe {CONFIG_PATH}. Copia config.example.json a config.json y completa los valores."
        )
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        raw = json.load(f)
    s = raw["scraper"]
    return Config(
        telegram=TelegramConfig(**raw["telegram"]),
        email=EmailConfig(**raw["email"]),
        scraper=ScraperConfig(
            http_timeout_seconds=s["http_timeout_seconds"],
            playwright_timeout_seconds=s["playwright_timeout_seconds"],
            user_agent=s["user_agent"],
            cache_html=s.get("cache_html", False),
            sanity_min_ratio=s.get("sanity_min_ratio", 0.25),
            sanity_max_ratio=s.get("sanity_max_ratio", 4.0),
            http_retries=s.get("http_retries", 2),
            http_backoff_base_seconds=s.get("http_backoff_base_seconds", 1.0),
            stealth_enabled=s.get("stealth_enabled", True),
            max_workers=s.get("max_workers", 4),
        ),
    )


def _read_config_json() -> dict:
    """Lee config.json completo como dict; falla con un mensaje guía si no existe."""
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"No existe {CONFIG_PATH}. Copia config.example.json a config.json y completa los valores."
        )
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def load_dashboard_config() -> DashboardConfig:
    """Lee solo la sección `dashboard` de config.json; las claves ausentes usan su valor por defecto."""
    d = _read_config_json().get("dashboard") or {}
    defaults = DashboardConfig()
    return DashboardConfig(
        host=d.get("host", defaults.host),
        port=int(d.get("port", defaults.port)),
        username=d.get("username", defaults.username),
        password_hash=d.get("password_hash", defaults.password_hash),
        session_secret=d.get("session_secret", defaults.session_secret),
        session_days=int(d.get("session_days", defaults.session_days)),
    )


def save_dashboard_credentials(username: str, password_hash: str, session_secret: str) -> None:
    """Guarda usuario, hash y clave de sesión en config.json sin tocar las otras secciones."""
    raw = _read_config_json()
    section = raw.setdefault("dashboard", {})
    section.update(username=username, password_hash=password_hash, session_secret=session_secret)
    # Escritura atómica: config.json tiene secretos y no debe quedar a medias.
    tmp_path = CONFIG_PATH.with_suffix(".json.tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(raw, f, indent=2, ensure_ascii=False)
    os.replace(tmp_path, CONFIG_PATH)


def load_products() -> list[Product]:
    if not PRODUCTS_PATH.exists():
        raise FileNotFoundError(f"No existe {PRODUCTS_PATH}.")
    with PRODUCTS_PATH.open("r", encoding="utf-8") as f:
        raw = json.load(f)
    return [Product.from_dict(item) for item in raw]


def save_products(products: list[Product]) -> None:
    def _product_dict(p: Product) -> dict:
        target_val = p.target_price_relative if p.target_price_relative is not None else p.target_price
        d: dict = {
            "id": p.id,
            "name": p.name,
            "url": p.url,
            "selector": p.selector,
            "target_price": target_val,
            "currency": p.currency,
            "active": p.active,
        }
        if p.grupo is not None:
            d["grupo"] = p.grupo
        if p.variant is not None:
            d["variant"] = p.variant
        return d

    data = [_product_dict(p) for p in products]
    # Escritura atómica: evita dejar el JSON a medias si algo interrumpe
    # y la carrera trivial con la corrida programada del pipeline.
    tmp_path = PRODUCTS_PATH.with_suffix(".json.tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp_path, PRODUCTS_PATH)
