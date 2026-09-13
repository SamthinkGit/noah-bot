"""Genera las credenciales de la Web API de Spotify para el modo espejo.

EJECUTAR EN TU PC, no en el servidor.

Hace falta porque desde diciembre de 2025 Spotify devuelve 429 a los tokens
sacados con el client_id de su app de escritorio, que es el que usa librespot.
El audio lo sigue sirviendo librespot; esto es solo para preguntar "que suena".

Antes de ejecutarlo:

1. Entra en https://developer.spotify.com/dashboard y crea una app.
2. En Redirect URIs pon exactamente:  http://127.0.0.1:8888/callback
   (con 127.0.0.1, Spotify ya no acepta `localhost`).
3. Copia el Client ID y el Client Secret.

Luego:

    uv run python auth_webapi.py

Genera `spotify_webapi.json`. Se lo adjuntas a Noah con `.noah spotify auth`.
Trata el fichero como una contrasena. No lo subas al repo.
"""

import json
import secrets
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import requests


REDIRECT_URI = "http://127.0.0.1:8888/callback"
SCOPES = "user-read-playback-state user-read-currently-playing"
OUTPUT_FILE = Path("spotify_webapi.json")

received: dict[str, str] = {}


class CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        query = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(query)
        received.update({key: value[0] for key, value in params.items()})

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(
            "<h2>Listo, ya puedes cerrar esta pestana.</h2>".encode("utf-8")
        )

    def log_message(self, *args: object) -> None:
        return


def main() -> None:
    client_id = input("Client ID: ").strip()
    client_secret = input("Client Secret: ").strip()

    if not client_id or not client_secret:
        print("Necesito los dos valores del dashboard.")
        return

    state = secrets.token_urlsafe(16)
    auth_url = "https://accounts.spotify.com/authorize?" + urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "scope": SCOPES,
            "redirect_uri": REDIRECT_URI,
            "state": state,
        }
    )

    print("\nAbre esta URL y autoriza la app:\n")
    print(auth_url)
    print()
    webbrowser.open(auth_url)

    server = HTTPServer(("127.0.0.1", 8888), CallbackHandler)
    server.handle_request()
    server.server_close()

    if received.get("state") != state:
        print("El `state` no coincide, abortado.")
        return

    if "code" not in received:
        print(f"Spotify no devolvio codigo: {received}")
        return

    response = requests.post(
        "https://accounts.spotify.com/api/token",
        data={
            "grant_type": "authorization_code",
            "code": received["code"],
            "redirect_uri": REDIRECT_URI,
        },
        auth=(client_id, client_secret),
        timeout=15,
    )

    if response.status_code != 200:
        print(f"Error {response.status_code}: {response.text}")
        return

    payload = response.json()

    if "refresh_token" not in payload:
        print(f"Spotify no devolvio refresh_token: {payload}")
        return

    OUTPUT_FILE.write_text(
        json.dumps(
            {
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": payload["refresh_token"],
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    print(f"\nListo. Fichero generado: {OUTPUT_FILE.resolve()}")
    print("Adjuntaselo a Noah con `.noah spotify auth` (borra el mensaje el solo).")


if __name__ == "__main__":
    main()
