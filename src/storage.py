from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  url TEXT NOT NULL,
  initial_price REAL NOT NULL,
  initial_date TEXT NOT NULL,
  currency TEXT NOT NULL,
  target_price REAL,
  active INTEGER DEFAULT 1,
  purchased INTEGER DEFAULT 0,
  purchased_at TEXT
);

CREATE TABLE IF NOT EXISTS price_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  product_id TEXT NOT NULL,
  price REAL NOT NULL,
  available INTEGER,
  captured_at TEXT NOT NULL,
  FOREIGN KEY (product_id) REFERENCES products(id)
);

CREATE INDEX IF NOT EXISTS idx_history_product_date
  ON price_history(product_id, captured_at);
"""


@dataclass
class StoredProduct:
    id: str
    name: str
    url: str
    initial_price: float
    initial_date: str
    currency: str
    target_price: float | None
    purchased: bool = False
    purchased_at: str | None = None


class Storage:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        cols = {row[1] for row in self.conn.execute("PRAGMA table_info(price_history)").fetchall()}
        if "available" not in cols:
            self.conn.execute("ALTER TABLE price_history ADD COLUMN available INTEGER")
        prod_cols = {row[1] for row in self.conn.execute("PRAGMA table_info(products)").fetchall()}
        if "purchased" not in prod_cols:
            self.conn.execute("ALTER TABLE products ADD COLUMN purchased INTEGER DEFAULT 0")
        if "purchased_at" not in prod_cols:
            self.conn.execute("ALTER TABLE products ADD COLUMN purchased_at TEXT")

    def close(self) -> None:
        self.conn.close()

    def get_product(self, product_id: str) -> StoredProduct | None:
        row = self.conn.execute(
            "SELECT id, name, url, initial_price, initial_date, currency, target_price, "
            "COALESCE(purchased, 0) AS purchased, purchased_at "
            "FROM products WHERE id = ?",
            (product_id,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_product(row)

    def all_products(self) -> list[StoredProduct]:
        rows = self.conn.execute(
            "SELECT id, name, url, initial_price, initial_date, currency, target_price, "
            "COALESCE(purchased, 0) AS purchased, purchased_at FROM products"
        ).fetchall()
        return [self._row_to_product(row) for row in rows]

    @staticmethod
    def _row_to_product(row: sqlite3.Row) -> StoredProduct:
        d = dict(row)
        d["purchased"] = bool(d.get("purchased") or 0)
        return StoredProduct(**d)

    def set_purchased(self, product_id: str, purchased: bool) -> bool:
        """Marca/desmarca un producto como comprado. Retorna True si existía."""
        ts = datetime.now(timezone.utc).isoformat() if purchased else None
        cur = self.conn.execute(
            "UPDATE products SET purchased = ?, purchased_at = ? WHERE id = ?",
            (1 if purchased else 0, ts, product_id),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def register_new_product(
        self,
        product_id: str,
        name: str,
        url: str,
        initial_price: float,
        currency: str,
        target_price: float | None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            "INSERT INTO products (id, name, url, initial_price, initial_date, "
            "currency, target_price, active) VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
            (product_id, name, url, initial_price, now, currency, target_price),
        )
        self.conn.commit()

    def append_price(self, product_id: str, price: float, available: bool | None = None) -> None:
        now = datetime.now(timezone.utc).isoformat()
        if available is None:
            avail_int = None
        else:
            avail_int = 1 if available else 0
        self.conn.execute(
            "INSERT INTO price_history (product_id, price, available, captured_at) VALUES (?, ?, ?, ?)",
            (product_id, price, avail_int, now),
        )
        self.conn.commit()

    def last_price(self, product_id: str) -> float | None:
        row = self.conn.execute(
            "SELECT price FROM price_history WHERE product_id = ? "
            "ORDER BY captured_at DESC LIMIT 1",
            (product_id,),
        ).fetchone()
        return float(row["price"]) if row else None

    def last_availability(self, product_id: str) -> bool | None:
        """Retorna la última disponibilidad conocida, ignorando registros con NULL."""
        row = self.conn.execute(
            "SELECT available FROM price_history WHERE product_id = ? AND available IS NOT NULL "
            "ORDER BY captured_at DESC LIMIT 1",
            (product_id,),
        ).fetchone()
        if row is None:
            return None
        return bool(row["available"])

    def min_price(self, product_id: str) -> tuple[float, str] | None:
        """Retorna (precio_mínimo, captured_at) del historial, o None si no hay datos."""
        row = self.conn.execute(
            "SELECT price, captured_at FROM price_history WHERE product_id = ? "
            "ORDER BY price ASC, captured_at DESC LIMIT 1",
            (product_id,),
        ).fetchone()
        if row is None:
            return None
        return float(row["price"]), row["captured_at"]

    def price_history(self, product_id: str, limit: int = 90) -> list[tuple[str, float]]:
        """Retorna lista de (captured_at, price) ordenada cronológicamente."""
        rows = self.conn.execute(
            "SELECT captured_at, price FROM price_history WHERE product_id = ? "
            "ORDER BY captured_at DESC LIMIT ?",
            (product_id, limit),
        ).fetchall()
        return [(row["captured_at"], float(row["price"])) for row in reversed(rows)]

    def price_changes(self, product_id: str) -> list[tuple[str, float]]:
        """Retorna solo los puntos donde el precio cambió respecto al registro anterior.

        El primer registro siempre se incluye. Útil para mostrar un histórico
        compacto sin filas redundantes de días con el mismo precio.
        """
        rows = self.conn.execute(
            "SELECT captured_at, price FROM price_history WHERE product_id = ? "
            "ORDER BY captured_at ASC",
            (product_id,),
        ).fetchall()
        changes: list[tuple[str, float]] = []
        last_price: float | None = None
        for row in rows:
            price = float(row["price"])
            if last_price is None or price != last_price:
                changes.append((row["captured_at"], price))
                last_price = price
        return changes

    def precio_hace_dias(self, product_id: str, dias: int = 30) -> float | None:
        """Primer precio registrado dentro de la ventana de los últimos `dias` días."""
        corte = (datetime.now(timezone.utc) - timedelta(days=dias)).isoformat()
        row = self.conn.execute(
            "SELECT price FROM price_history WHERE product_id = ? AND captured_at >= ? "
            "ORDER BY captured_at ASC LIMIT 1",
            (product_id, corte),
        ).fetchone()
        return float(row["price"]) if row else None

    def update_product_metadata(
        self,
        product_id: str,
        name: str,
        url: str,
        currency: str,
        target_price: float | None,
    ) -> bool:
        """Sincroniza la metadata editable de un producto. NUNCA toca initial_price
        ni initial_date (baseline histórico inmutable). Retorna True si existía."""
        cur = self.conn.execute(
            "UPDATE products SET name = ?, url = ?, currency = ?, target_price = ? WHERE id = ?",
            (name, url, currency, target_price, product_id),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def resumen_historial(self, product_id: str) -> tuple[int, str | None]:
        """Retorna (número de registros, última captura) del historial de un producto."""
        row = self.conn.execute(
            "SELECT COUNT(*) AS n, MAX(captured_at) AS ultima FROM price_history WHERE product_id = ?",
            (product_id,),
        ).fetchone()
        return int(row["n"]), row["ultima"]

    def purge_product(self, product_id: str) -> bool:
        """Borra el producto y todo su historial de la BD (huérfanos o eliminación
        con purga explícita). Retorna True si el producto existía."""
        self.conn.execute("DELETE FROM price_history WHERE product_id = ?", (product_id,))
        cur = self.conn.execute("DELETE FROM products WHERE id = ?", (product_id,))
        self.conn.commit()
        return cur.rowcount > 0

    def get_last_prices(self, product_ids: list[str]) -> dict[str, float | None]:
        if not product_ids:
            return {}
        placeholders = ",".join("?" * len(product_ids))
        rows = self.conn.execute(
            f"SELECT product_id, price FROM price_history "
            f"WHERE (product_id, captured_at) IN ("
            f"  SELECT product_id, MAX(captured_at) FROM price_history "
            f"  WHERE product_id IN ({placeholders}) GROUP BY product_id"
            f")",
            product_ids,
        ).fetchall()
        result: dict[str, float | None] = dict.fromkeys(product_ids)
        for row in rows:
            result[row["product_id"]] = float(row["price"])
        return result
