"""Helper CLI para agregar un producto nuevo a products.json.

Uso:
  python scripts/add_product.py <id> <nombre> <url> [--selector CSS] [--target 800] [--currency USD] [--grupo ID_GRUPO]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import Product, load_products, save_products


def main() -> int:
    parser = argparse.ArgumentParser(description="Agrega un producto a products.json")
    parser.add_argument("id", help="ID único del producto (ej: 'tv-samsung-55')")
    parser.add_argument("name", help="Nombre legible del producto")
    parser.add_argument("url", help="URL del producto")
    parser.add_argument("--selector", default=None, help="CSS selector opcional para forzar extracción")
    parser.add_argument("--target", type=float, default=None, help="Precio objetivo opcional")
    parser.add_argument("--currency", default="USD", help="Moneda (USD, COP, etc.)")
    parser.add_argument("--grupo", default=None, help="ID de grupo para comparar el mismo producto en varios sitios")
    args = parser.parse_args()

    products = load_products()
    if any(p.id == args.id for p in products):
        print(f"Ya existe un producto con id '{args.id}'. Editá products.json manualmente.")
        return 1

    products.append(Product(
        id=args.id,
        name=args.name,
        url=args.url,
        selector=args.selector,
        target_price=args.target,
        currency=args.currency,
        active=True,
        grupo=args.grupo,
    ))
    save_products(products)
    grupo_info = f" (grupo: {args.grupo})" if args.grupo else ""
    print(f"Agregado: {args.id} ({args.name}){grupo_info}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
