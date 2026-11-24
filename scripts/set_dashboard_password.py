"""Configura el usuario y la contraseña del dashboard en config.json.

Uso:
  .venv\\Scripts\\python.exe scripts\\set_dashboard_password.py [--usuario admin]

Guarda solo el hash scrypt y regenera la clave de sesión: las sesiones abiertas se cierran.
"""
from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.auth import hash_password, new_session_secret
from src.config import CONFIG_PATH, load_dashboard_config, save_dashboard_credentials

MIN_PASSWORD_LENGTH = 10


def main() -> int:
    parser = argparse.ArgumentParser(description="Configura el login del dashboard en config.json")
    parser.add_argument("--usuario", default=None, help="Usuario del dashboard (por defecto, el actual o 'admin')")
    args = parser.parse_args()

    try:
        username = (args.usuario or load_dashboard_config().username).strip()
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    if not username:
        print("ERROR: el usuario no puede estar vacío.", file=sys.stderr)
        return 1

    password = getpass.getpass(f"Contraseña para '{username}': ")
    if len(password) < MIN_PASSWORD_LENGTH:
        print(f"ERROR: la contraseña debe tener al menos {MIN_PASSWORD_LENGTH} caracteres.", file=sys.stderr)
        return 1
    if getpass.getpass("Repite la contraseña: ") != password:
        print("ERROR: las contraseñas no coinciden.", file=sys.stderr)
        return 1

    save_dashboard_credentials(username, hash_password(password), new_session_secret())
    print(f"Login del dashboard guardado en {CONFIG_PATH} para el usuario '{username}'.")
    print("Aplica de inmediato: las sesiones abiertas deberán iniciar sesión otra vez.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
