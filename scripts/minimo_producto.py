"""Consulta el mínimo histórico de precio de uno o varios productos.

Uso:
  python scripts/minimo_producto.py <product_id> [<product_id> ...]
  python scripts/minimo_producto.py --todos
  python scripts/minimo_producto.py <product_id> --guardar
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from src.config import DB_PATH, REPORTS_DIR


LINEA = "-" * 55


def _fmt(valor: float, moneda: str) -> str:
    return f"{moneda} {valor:,.0f}"


def _consultar_producto(cur: sqlite3.Cursor, product_id: str) -> str | None:
    cur.execute(
        "SELECT id, name, initial_price, currency FROM products WHERE id = ?",
        (product_id,),
    )
    fila = cur.fetchone()
    if fila is None:
        return None

    pid, nombre, precio_inicial, moneda = fila

    cur.execute(
        """
        SELECT price, captured_at
        FROM price_history
        WHERE product_id = ? AND price = (SELECT MIN(price) FROM price_history WHERE product_id = ?)
        ORDER BY captured_at ASC
        LIMIT 1
        """,
        (pid, pid),
    )
    fila_min = cur.fetchone()

    cur.execute(
        "SELECT price FROM price_history WHERE product_id = ? ORDER BY captured_at DESC LIMIT 1",
        (pid,),
    )
    fila_actual = cur.fetchone()

    precio_actual = fila_actual[0] if fila_actual else None
    precio_min = fila_min[0] if fila_min else None
    fecha_min = fila_min[1][:10] if fila_min else "—"

    lineas = [
        LINEA,
        f"Producto         : {nombre} ({pid})",
        f"Precio inicial   : {_fmt(precio_inicial, moneda)}",
    ]

    if precio_actual is not None:
        lineas.append(f"Precio actual    : {_fmt(precio_actual, moneda)}")
    else:
        lineas.append("Precio actual    : sin datos")

    if precio_min is not None:
        ahorro = precio_inicial - precio_min
        pct = (ahorro / precio_inicial * 100) if precio_inicial else 0
        lineas.append(f"MÍNIMO HISTÓRICO : {_fmt(precio_min, moneda)}  ({fecha_min})")
        lineas.append(f"Ahorro vs inicial: {_fmt(ahorro, moneda)} ({pct:.1f}%)")
    else:
        lineas.append("MÍNIMO HISTÓRICO : sin historial registrado")

    lineas.append(LINEA)
    return "\n".join(lineas)


def _listar_ids(cur: sqlite3.Cursor) -> list[str]:
    cur.execute("SELECT id FROM products ORDER BY id")
    return [r[0] for r in cur.fetchall()]


def _resolver_ids(cur: sqlite3.Cursor, args: argparse.Namespace) -> list[str] | None:
    if args.todos:
        ids = _listar_ids(cur)
        if not ids:
            print("No hay productos registrados en la base de datos.")
            return None
        return ids
    return args.ids


def _reportar_errores(errores: list[str]) -> None:
    con = sqlite3.connect(DB_PATH)
    disponibles = _listar_ids(con.cursor())
    con.close()
    for pid in errores:
        print(f"Producto no encontrado: '{pid}'")
    print(f"IDs disponibles: {', '.join(disponibles)}")


def _guardar_reporte(salida: str, ids: list[str], todos: bool) -> None:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    sufijo = "todos" if todos else "_".join(ids)
    nombre_archivo = REPORTS_DIR / f"minimo_{sufijo}_{date.today()}.md"
    nombre_archivo.write_text(salida, encoding="utf-8")
    print(f"\nReporte guardado en: {nombre_archivo}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Consulta el mínimo histórico de precio de un producto")
    parser.add_argument("ids", nargs="*", help="ID(s) de producto a consultar")
    parser.add_argument("--todos", action="store_true", help="Consultar todos los productos")
    parser.add_argument("--guardar", action="store_true", help="Guardar el resultado en reports/")
    args = parser.parse_args()

    if not args.ids and not args.todos:
        parser.print_help()
        return 1

    if not DB_PATH.exists():
        print(f"No existe la base de datos en {DB_PATH}. Corré python -m src.main al menos una vez.")
        return 1

    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    ids = _resolver_ids(cur, args)
    if ids is None:
        con.close()
        return 1

    bloques: list[str] = []
    errores: list[str] = []
    for pid in ids:
        resultado = _consultar_producto(cur, pid)
        (bloques if resultado is not None else errores).append(resultado or pid)
    con.close()

    if errores:
        _reportar_errores(errores)

    salida = "\n".join(bloques)
    if salida:
        print(salida)

    if args.guardar and bloques:
        _guardar_reporte(salida, ids, args.todos)

    return 0 if not errores or bloques else 1


if __name__ == "__main__":
    sys.exit(main())
