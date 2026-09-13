"""Genera las credenciales de Spotify para Noah.

EJECUTAR EN TU PC, no en el servidor: el login de Spotify redirige a
http://127.0.0.1:5588/login, o sea al localhost de la maquina que corre esto.

    uv run python auth_local.py

Te imprime una URL, la abres, te logueas, y se genera `credentials.json` en
esta carpeta. Ese fichero es el que le mandas a Noah adjunto con
`.noah spotify auth` (Noah borra el mensaje al momento). Trata el fichero como
una contrasena: da acceso a tu cuenta. No lo subas al repo.
"""

from pathlib import Path

from librespot.core import Session


CREDENTIALS_FILE = Path("credentials.json")


def print_auth_url(url: str) -> None:
    print("\nAbre esta URL en tu navegador y loguea tu cuenta de Spotify:\n")
    print(url)
    print()


def main() -> None:
    if CREDENTIALS_FILE.exists():
        print(f"Ya existe {CREDENTIALS_FILE}. Borralo si quieres rehacer el login.")

    session = Session.Builder().oauth(print_auth_url).create()
    print(f"\nListo. Cuenta: {session.username()}")
    print(f"Fichero generado: {CREDENTIALS_FILE.resolve()}")
    print("Adjuntaselo a Noah con `.noah spotify auth` (borra el mensaje el solo).")
    session.close()


if __name__ == "__main__":
    main()
