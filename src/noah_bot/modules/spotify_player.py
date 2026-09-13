"""Reproduce el audio real de Spotify dentro de un canal de voz.

No convierte a Noah en un dispositivo de Spotify Connect: usa las credenciales
de la cuenta para descargar el audio del track y lo reproduce en paralelo, asi
que la musica sigue sonando tambien en tu PC o tu Alexa.
"""

from __future__ import annotations

import hmac
import importlib
import json
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import discord
import requests


CREDENTIALS_PATH = "spotify_credentials.json"
ADMINS_PATH = "spotify_admins.json"
DEFAULT_PASSWORD = "5441"
WEB_API_BASE = "https://api.spotify.com/v1"
PLAYBACK_SCOPE = "user-read-playback-state"
DEFAULT_VOLUME = 0.6
FFMPEG_READ_SIZE = 8192
REQUEST_TIMEOUT = 10

TRACK_PATTERN = re.compile(
    r"(?:spotify:track:|open\.spotify\.com/(?:intl-[a-z]{2}/)?track/)"
    r"(?P<track_id>[0-9A-Za-z]{22})"
)


class SpotifyError(RuntimeError):
    """Error generico del reproductor de Spotify."""


class SpotifyAuthError(SpotifyError):
    """Faltan credenciales o la sesion no se pudo abrir."""


def _import_librespot(module_name: str) -> Any:
    try:
        return importlib.import_module(module_name)
    except ImportError as exc:
        raise SpotifyAuthError(
            "Falta la dependencia `librespot`. Ejecuta `uv sync` en el servidor."
        ) from exc


def format_ms(milliseconds: int) -> str:
    total_seconds = max(0, int(milliseconds)) // 1000
    minutes, seconds = divmod(total_seconds, 60)
    return f"{minutes}:{seconds:02d}"


@dataclass(slots=True)
class TrackInfo:
    uri: str
    name: str
    artists: str
    album: str
    duration_ms: int

    @property
    def label(self) -> str:
        return f"{self.artists} — {self.name}"


@dataclass(slots=True)
class PlaybackState:
    uri: str | None
    name: str
    artists: str
    is_playing: bool
    progress_ms: int
    duration_ms: int
    device: str
    is_track: bool

    @property
    def label(self) -> str:
        return f"{self.artists} — {self.name}"


class TrackPlayback:
    """Un track sonando: ffmpeg + el hilo que le va metiendo el Ogg."""

    def __init__(self, info: TrackInfo, ogg_stream: Any, position_ms: int) -> None:
        self.info = info
        self.position_ms = max(0, int(position_ms))
        self._ogg = ogg_stream
        self._started_at = time.monotonic()
        self._paused_at: float | None = None
        self._stopped = threading.Event()
        self._ffmpeg = subprocess.Popen(
            self._build_ffmpeg_args(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        self._pump = threading.Thread(target=self._pump_audio, daemon=True)
        self._pump.start()

    def _build_ffmpeg_args(self) -> list[str]:
        args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "pipe:0"]

        if self.position_ms > 0:
            args += ["-ss", f"{self.position_ms / 1000:.3f}"]

        args += ["-f", "s16le", "-ar", "48000", "-ac", "2", "pipe:1"]
        return args

    def _pump_audio(self) -> None:
        try:
            while not self._stopped.is_set():
                chunk = self._ogg.read(FFMPEG_READ_SIZE)
                if not chunk:
                    break
                self._ffmpeg.stdin.write(chunk)
        except (BrokenPipeError, OSError, ValueError):
            pass
        finally:
            try:
                self._ffmpeg.stdin.close()
            except (BrokenPipeError, OSError, ValueError):
                pass

    def source(self, volume: float) -> discord.AudioSource:
        return discord.PCMVolumeTransformer(
            discord.PCMAudio(self._ffmpeg.stdout),
            volume=volume,
        )

    @property
    def elapsed_ms(self) -> int:
        reference = self._paused_at if self._paused_at is not None else time.monotonic()
        return self.position_ms + int((reference - self._started_at) * 1000)

    def pause(self) -> None:
        if self._paused_at is None:
            self._paused_at = time.monotonic()

    def resume(self) -> None:
        if self._paused_at is not None:
            self._started_at += time.monotonic() - self._paused_at
            self._paused_at = None

    def close(self) -> None:
        self._stopped.set()

        try:
            self._ffmpeg.kill()
        except OSError:
            pass

        try:
            self._ogg.close()
        except (OSError, ValueError, AttributeError):
            pass


class SpotifyAccessStore:
    """Quien puede tocar los comandos de Spotify.

    Se abre con una contrasena (`SPOTIFY_PASSWORD` en el .env, por defecto
    `5441`) y el usuario queda desbloqueado aunque se reinicie el bot.
    """

    def __init__(self, json_path: str = ADMINS_PATH) -> None:
        self.json_path = Path(json_path)
        self._users = self._load()

    def _load(self) -> set[int]:
        if not self.json_path.is_file():
            return set()

        try:
            payload = json.loads(self.json_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return set()

        if not isinstance(payload, dict):
            return set()

        return {
            int(user_id)
            for user_id in payload.get("users", [])
            if str(user_id).isdigit()
        }

    def _save(self) -> None:
        self.json_path.write_text(
            json.dumps({"users": sorted(self._users)}, indent=2),
            encoding="utf-8",
        )

    @property
    def password(self) -> str:
        return os.getenv("SPOTIFY_PASSWORD", DEFAULT_PASSWORD)

    def check_password(self, value: str) -> bool:
        return hmac.compare_digest(value.strip(), self.password)

    def is_allowed(self, user_id: int) -> bool:
        return user_id in self._users

    def unlock(self, user_id: int) -> bool:
        if user_id in self._users:
            return False

        self._users.add(user_id)
        self._save()
        return True

    def lock(self, user_id: int) -> bool:
        if user_id not in self._users:
            return False

        self._users.discard(user_id)
        self._save()
        return True

    def users(self) -> list[int]:
        return sorted(self._users)


@dataclass(slots=True)
class SpotifyGuildState:
    text_channel_id: int = 0
    mirror: bool = False
    volume: float = DEFAULT_VOLUME
    playback: TrackPlayback | None = None
    paused: bool = False


class SpotifyPlayer:
    """Sesion de librespot + acceso a la Web API con el token de esa sesion."""

    def __init__(self, credentials_path: str = CREDENTIALS_PATH) -> None:
        self.credentials_path = Path(credentials_path)
        self._session: Any = None
        self._lock = threading.Lock()

    def has_credentials(self) -> bool:
        return self.credentials_path.is_file()

    @property
    def is_connected(self) -> bool:
        return self._session is not None

    def save_credentials(self, raw: bytes) -> str:
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SpotifyAuthError("El fichero no es un JSON valido.") from exc

        if not isinstance(payload, dict):
            raise SpotifyAuthError("El JSON no tiene el formato de credentials.json.")

        username = payload.get("username")
        auth_type = payload.get("auth_type") or payload.get("type")
        auth_data = payload.get("auth_data") or payload.get("credentials")

        if not username or not auth_type or not auth_data:
            raise SpotifyAuthError(
                "Al JSON le faltan claves: `username`, `auth_type`/`type` y "
                "`auth_data`/`credentials`."
            )

        self.disconnect()
        self.credentials_path.write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )
        return str(username)

    def connect(self) -> str:
        with self._lock:
            if self._session is not None:
                return self._session.username()

            if not self.has_credentials():
                raise SpotifyAuthError(
                    "No hay credenciales. Usa `.noah spotify auth` por DM."
                )

            core = _import_librespot("librespot.core")

            try:
                self._session = (
                    core.Session.Builder()
                    .stored_file(str(self.credentials_path))
                    .create()
                )
            except Exception as exc:
                self._session = None
                raise SpotifyAuthError(
                    f"No se pudo abrir la sesion de Spotify: {exc}"
                ) from exc

            return self._session.username()

    def disconnect(self) -> None:
        with self._lock:
            session = self._session
            self._session = None

        if session is None:
            return

        try:
            session.close()
        except Exception:
            pass

    def username(self) -> str:
        return self._require_session().username()

    def _require_session(self) -> Any:
        if self._session is None:
            self.connect()
        return self._session

    def web_token(self, scope: str = PLAYBACK_SCOPE) -> str:
        session = self._require_session()

        try:
            return session.tokens().get(scope)
        except Exception as exc:
            raise SpotifyError(
                f"No se pudo obtener el token de la Web API: {exc}"
            ) from exc

    def current_playback(self) -> PlaybackState | None:
        token = self.web_token()
        response = requests.get(
            f"{WEB_API_BASE}/me/player",
            headers={"Authorization": f"Bearer {token}"},
            params={"additional_types": "track,episode"},
            timeout=REQUEST_TIMEOUT,
        )

        if response.status_code in (202, 204) or not response.content:
            return None

        if response.status_code != 200:
            raise SpotifyError(
                f"Web API respondio {response.status_code}: {response.text[:120]}"
            )

        data = response.json()
        item = data.get("item") or {}
        artists = ", ".join(
            artist.get("name", "")
            for artist in item.get("artists", [])
            if artist.get("name")
        )

        if not artists:
            artists = (item.get("show") or {}).get("name") or "—"

        return PlaybackState(
            uri=item.get("uri"),
            name=item.get("name") or "Desconocido",
            artists=artists,
            is_playing=bool(data.get("is_playing")),
            progress_ms=int(data.get("progress_ms") or 0),
            duration_ms=int(item.get("duration_ms") or 0),
            device=(data.get("device") or {}).get("name") or "—",
            is_track=item.get("type") == "track",
        )

    @staticmethod
    def resolve_track_uri(text: str) -> str:
        match = TRACK_PATTERN.search(text.strip())

        if match is None:
            raise SpotifyError(
                "Eso no es un track de Spotify. Pega el link o el URI "
                "(`spotify:track:...`)."
            )

        return f"spotify:track:{match.group('track_id')}"

    def open_track(self, uri: str, position_ms: int = 0) -> TrackPlayback:
        session = self._require_session()
        decoders = _import_librespot("librespot.audio.decoders")
        metadata = _import_librespot("librespot.metadata")

        try:
            track_id = metadata.TrackId.from_uri(uri)
            picker = decoders.VorbisOnlyAudioQuality(decoders.AudioQuality.VERY_HIGH)
            loaded = session.content_feeder().load(track_id, picker, False, None)
            ogg_stream = loaded.input_stream.stream()
        except SpotifyError:
            raise
        except Exception as exc:
            raise SpotifyError(f"No se pudo cargar el track: {exc}") from exc

        return TrackPlayback(
            _build_track_info(uri, loaded.track),
            ogg_stream,
            position_ms,
        )


def _build_track_info(uri: str, track: Any) -> TrackInfo:
    artists = ", ".join(artist.name for artist in track.artist if artist.name)

    return TrackInfo(
        uri=uri,
        name=track.name or "—",
        artists=artists or "—",
        album=(track.album.name if track.album else "") or "—",
        duration_ms=int(track.duration or 0),
    )
