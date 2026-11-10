"""Agenda de eventos deportivos: datos y utilidades (sin nada de Discord)."""

import io
import json
import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps

from noah_bot.modules.billboard import item_attendees, item_start

DURATION_MIN = 10
DURATION_MAX = 24 * 60
CAPACITY_MAX = 999
PHOTO_MAX_SIDE = 1600
THUMB_SIDE = 160

_DURATION_CLOCK = re.compile(r"^(?P<hours>\d{1,2}):(?P<minutes>\d{2})$")
_DURATION_HOURS = re.compile(
    r"^(?P<hours>\d{1,2}(?:[.,]\d+)?)\s*h(?:oras?|rs?)?\s*"
    r"(?:(?P<minutes>\d{1,2})\s*(?:m|min|mins|minutos?)?)?$"
)
_DURATION_MINUTES = re.compile(r"^(?P<minutes>\d{1,4})\s*(?:m|min|mins|minutos?)?$")


def parse_duration(text: str) -> int:
    """Convierte «1h30», «90», «1:30», «2 horas» o «45 min» en minutos.

    Lanza `ValueError` con un mensaje para el usuario si no se entiende.
    """
    value = " ".join((text or "").lower().split())
    minutes: int | None = None
    if match := _DURATION_CLOCK.match(value):
        minutes = int(match["hours"]) * 60 + int(match["minutes"])
    elif match := _DURATION_HOURS.match(value):
        minutes = round(float(match["hours"].replace(",", ".")) * 60)
        minutes += int(match["minutes"] or 0)
    elif match := _DURATION_MINUTES.match(value):
        minutes = int(match["minutes"])

    if minutes is None:
        raise ValueError("No entiendo la duración. Escríbela así: `1h30`, `90` o `2h`.")
    if not DURATION_MIN <= minutes <= DURATION_MAX:
        raise ValueError("La duración tiene que estar entre 10 minutos y 24 horas.")
    return minutes


def format_duration(minutes: int) -> str:
    hours, rest = divmod(int(minutes), 60)
    if not hours:
        return f"{rest} min"
    return f"{hours} h" + (f" {rest} min" if rest else "")


def parse_capacity(text: str) -> int | None:
    """Plazas máximas; vacío o 0 significa sin límite."""
    value = (text or "").strip()
    if not value:
        return None
    if not value.isdigit():
        raise ValueError("Las plazas tienen que ser un número (o déjalo vacío para no poner límite).")
    capacity = int(value)
    if capacity == 0:
        return None
    if capacity > CAPACITY_MAX:
        raise ValueError(f"Como mucho {CAPACITY_MAX} plazas.")
    return capacity


def event_capacity(item: dict[str, Any]) -> int | None:
    try:
        capacity = int(item.get("capacity") or 0)
    except (TypeError, ValueError):
        return None
    return capacity or None


def event_duration(item: dict[str, Any]) -> timedelta:
    try:
        minutes = int(item.get("duration") or 0)
    except (TypeError, ValueError):
        minutes = 0
    return timedelta(minutes=minutes or 60)


def event_end(item: dict[str, Any]) -> datetime | None:
    start = item_start(item)
    return start + event_duration(item) if start is not None else None


def is_full(item: dict[str, Any]) -> bool:
    capacity = event_capacity(item)
    return capacity is not None and len(item_attendees(item)) >= capacity


def event_geo(item: dict[str, Any]) -> tuple[float, float] | None:
    geo = item.get("geo")
    if not isinstance(geo, list) or len(geo) != 2:
        return None
    try:
        return float(geo[0]), float(geo[1])
    except (TypeError, ValueError):
        return None


def prepare_photo(data: bytes) -> tuple[bytes, bytes]:
    """Devuelve (foto en JPEG a un tamaño razonable, miniatura cuadrada).

    Lanza `ValueError` si no es una imagen válida.
    """
    try:
        with Image.open(io.BytesIO(data)) as source:
            image = ImageOps.exif_transpose(source).convert("RGB")
    except Exception as error:  # Pillow lanza errores de varios tipos.
        raise ValueError("No he podido leer la imagen. Prueba con un png o jpg.") from error

    photo = image.copy()
    photo.thumbnail((PHOTO_MAX_SIDE, PHOTO_MAX_SIDE))
    photo_buffer = io.BytesIO()
    photo.save(photo_buffer, format="JPEG", quality=85, optimize=True)

    thumb = ImageOps.fit(image, (THUMB_SIDE, THUMB_SIDE), method=Image.Resampling.LANCZOS)
    thumb_buffer = io.BytesIO()
    thumb.save(thumb_buffer, format="JPEG", quality=82, optimize=True)
    return photo_buffer.getvalue(), thumb_buffer.getvalue()


class SportsStore:
    """Eventos deportivos por servidor: canal del panel, rol editor y eventos."""

    def __init__(
        self,
        json_path: str = "sports_events.json",
        media_dir: str = "sports_media",
    ) -> None:
        self.json_path = Path(json_path)
        self.media_dir = Path(media_dir)
        self._guilds: dict[str, dict[str, Any]] = self._load()

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self.json_path.exists():
            return {}
        try:
            with self.json_path.open("r", encoding="utf-8") as file:
                payload = json.load(file)
        except (OSError, json.JSONDecodeError):
            return {}

        guilds = payload.get("guilds") if isinstance(payload, dict) else None
        if not isinstance(guilds, dict):
            return {}
        return {
            str(guild_key): guild_data
            for guild_key, guild_data in guilds.items()
            if isinstance(guild_data, dict)
        }

    def _save(self) -> None:
        with self.json_path.open("w", encoding="utf-8") as file:
            json.dump({"guilds": self._guilds}, file, indent=2, ensure_ascii=False)

    def _guild_data(self, guild_id: int) -> dict[str, Any]:
        return self._guilds.setdefault(str(guild_id), {})

    def _get_int(self, guild_id: int, key: str) -> int | None:
        guild_data = self._guilds.get(str(guild_id))
        if not isinstance(guild_data, dict):
            return None
        try:
            return int(guild_data[key])
        except (KeyError, TypeError, ValueError):
            return None

    def _events(self, guild_id: int) -> dict[str, dict[str, Any]]:
        guild_data = self._guild_data(guild_id)
        events = guild_data.get("events")
        if not isinstance(events, dict):
            events = {}
            guild_data["events"] = events
        return events

    def guild_ids(self) -> list[int]:
        guild_ids: list[int] = []
        for guild_key in self._guilds:
            try:
                guild_ids.append(int(guild_key))
            except ValueError:
                continue
        return guild_ids

    # Configuración

    def set_editor_role(self, guild_id: int, role_id: int | None) -> None:
        self._guild_data(guild_id)["editor_role_id"] = str(role_id) if role_id else None
        self._save()

    def get_editor_role_id(self, guild_id: int) -> int | None:
        return self._get_int(guild_id, "editor_role_id")

    def set_panel(
        self, guild_id: int, channel_id: int | None, message_id: int | None
    ) -> None:
        guild_data = self._guild_data(guild_id)
        guild_data["panel_channel_id"] = str(channel_id) if channel_id else None
        guild_data["panel_message_id"] = str(message_id) if message_id else None
        self._save()

    def get_panel(self, guild_id: int) -> tuple[int | None, int | None]:
        return (
            self._get_int(guild_id, "panel_channel_id"),
            self._get_int(guild_id, "panel_message_id"),
        )

    # Eventos

    def list_events(self, guild_id: int) -> list[dict[str, Any]]:
        """Copias ordenadas por fecha."""
        events = [
            {**event, "id": event_id}
            for event_id, event in self._events(guild_id).items()
            if isinstance(event, dict)
        ]
        far_future = datetime.max.replace(tzinfo=timezone.utc)
        events.sort(
            key=lambda event: (item_start(event) or far_future, str(event.get("title", "")))
        )
        return events

    def get_event(self, guild_id: int, event_id: str) -> dict[str, Any] | None:
        event = self._events(guild_id).get(event_id)
        return {**event, "id": event_id} if isinstance(event, dict) else None

    def add_event(
        self,
        guild_id: int,
        *,
        title: str,
        starts_at: datetime,
        duration: int,
        location: str,
        created_by: int | None = None,
    ) -> str:
        events = self._events(guild_id)
        event_id = secrets.token_hex(3)
        while event_id in events:
            event_id = secrets.token_hex(3)

        events[event_id] = {
            "title": title,
            "starts_at": starts_at.astimezone(timezone.utc).isoformat(),
            "duration": duration,
            "location": location,
            "geo": None,
            "map": None,
            "description": "",
            "capacity": None,
            "photo": None,
            "thumb": None,
            "attendees": [],
            "reminded": False,
            "reminder_message": None,
            "created_by": str(created_by) if created_by else None,
        }
        self._save()
        return event_id

    def update_basics(
        self,
        guild_id: int,
        event_id: str,
        *,
        title: str,
        starts_at: datetime,
        duration: int,
        location: str,
    ) -> bool:
        event = self._events(guild_id).get(event_id)
        if not isinstance(event, dict):
            return False

        new_start = starts_at.astimezone(timezone.utc).isoformat()
        if new_start != event.get("starts_at"):
            # Si cambia la hora, el recordatorio se vuelve a enviar a la nueva hora.
            event["reminded"] = False
        if location != event.get("location"):
            # El mapa antiguo ya no vale: se genera otro.
            self._delete_media(event.get("map"))
            event["map"] = None
            event["geo"] = None
        event.update(title=title, starts_at=new_start, duration=duration, location=location)
        self._save()
        return True

    def update_details(
        self, guild_id: int, event_id: str, *, description: str, capacity: int | None
    ) -> bool:
        event = self._events(guild_id).get(event_id)
        if not isinstance(event, dict):
            return False
        event.update(description=description, capacity=capacity)
        self._save()
        return True

    def remove_event(self, guild_id: int, event_id: str) -> dict[str, Any] | None:
        event = self._events(guild_id).pop(event_id, None)
        if not isinstance(event, dict):
            return None
        self._save()
        for key in ("photo", "thumb", "map"):
            self._delete_media(event.get(key))
        return {**event, "id": event_id}

    def join(self, guild_id: int, event_id: str, user_id: int) -> str:
        """Apunta a alguien. Devuelve «joined», «already», «full» o «missing»."""
        event = self._events(guild_id).get(event_id)
        if not isinstance(event, dict):
            return "missing"
        attendees = item_attendees(event)
        if user_id in attendees:
            return "already"
        if is_full(event):
            return "full"
        attendees.append(user_id)
        event["attendees"] = [str(attendee) for attendee in attendees]
        self._save()
        return "joined"

    def leave(self, guild_id: int, event_id: str, user_id: int) -> bool:
        event = self._events(guild_id).get(event_id)
        if not isinstance(event, dict):
            return False
        attendees = item_attendees(event)
        if user_id not in attendees:
            return False
        attendees.remove(user_id)
        event["attendees"] = [str(attendee) for attendee in attendees]
        self._save()
        return True

    def set_attendees(self, guild_id: int, event_id: str, user_ids: list[int]) -> bool:
        """Sustituye la lista de apuntados (lo usa el editor; puede pasarse del límite)."""
        event = self._events(guild_id).get(event_id)
        if not isinstance(event, dict):
            return False
        event["attendees"] = [str(user_id) for user_id in dict.fromkeys(user_ids)]
        self._save()
        return True

    def mark_reminded(
        self, guild_id: int, event_id: str, channel_id: int | None, message_id: int | None
    ) -> None:
        event = self._events(guild_id).get(event_id)
        if not isinstance(event, dict):
            return
        event["reminded"] = True
        event["reminder_message"] = (
            [str(channel_id), str(message_id)] if channel_id and message_id else None
        )
        self._save()

    # Imágenes (se guardan en disco: las URLs de Discord caducan)

    def media_path(self, event: dict[str, Any], key: str) -> Path | None:
        relative = event.get(key)
        if not isinstance(relative, str) or not relative:
            return None
        path = self.media_dir / relative
        return path if path.is_file() else None

    def _write_media(self, guild_id: int, event_id: str, kind: str, data: bytes, ext: str) -> str:
        relative = f"{guild_id}/{event_id}-{kind}-{secrets.token_hex(2)}{ext}"
        path = self.media_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return relative

    def set_photo(
        self, guild_id: int, event_id: str, photo: tuple[bytes, bytes] | None
    ) -> bool:
        """Guarda (foto, miniatura) ya preparadas con `prepare_photo`, o las quita."""
        event = self._events(guild_id).get(event_id)
        if not isinstance(event, dict):
            return False

        self._delete_media(event.get("photo"))
        self._delete_media(event.get("thumb"))
        event["photo"] = event["thumb"] = None
        if photo is not None:
            event["photo"] = self._write_media(guild_id, event_id, "photo", photo[0], ".jpg")
            event["thumb"] = self._write_media(guild_id, event_id, "thumb", photo[1], ".jpg")
        self._save()
        return True

    def set_map(
        self,
        guild_id: int,
        event_id: str,
        location: str,
        geo: tuple[float, float] | None,
        image: bytes | None,
    ) -> bool:
        """Guarda el mapa si la ubicación sigue siendo la misma que se buscó."""
        event = self._events(guild_id).get(event_id)
        if not isinstance(event, dict) or event.get("location") != location:
            return False

        self._delete_media(event.get("map"))
        event["geo"] = list(geo) if geo is not None else None
        event["map"] = (
            self._write_media(guild_id, event_id, "map", image, ".png") if image else None
        )
        self._save()
        return True

    def _delete_media(self, relative: Any) -> None:
        if not isinstance(relative, str) or not relative:
            return
        try:
            (self.media_dir / relative).unlink(missing_ok=True)
        except OSError:
            pass
