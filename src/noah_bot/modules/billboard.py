import json
import re
import secrets
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

# En Windows hace falta el paquete `tzdata`; si aun así falla, se usa la hora del equipo.
try:
    LOCAL_TIMEZONE = ZoneInfo("Europe/Madrid")
except Exception:
    LOCAL_TIMEZONE = datetime.now().astimezone().tzinfo or timezone.utc
KIND_MOVIE = "movie"
KIND_ANNOUNCEMENT = "announcement"

_TIME_PATTERN = re.compile(
    r"^\s*(?P<hour>\d{1,2})\s*(?:[:.h]\s*(?P<minute>\d{2})?)?\s*h?\s*$",
    re.IGNORECASE,
)


def local_today(now: datetime | None = None) -> date:
    return (now or datetime.now(timezone.utc)).astimezone(LOCAL_TIMEZONE).date()


def parse_local_time(text: str) -> tuple[int, int]:
    """Convierte «22:00», «22.30», «22h» o «22» en (hora, minuto).

    Lanza `ValueError` con un mensaje para el usuario si no se entiende.
    """
    match = _TIME_PATTERN.match(text or "")
    if match is None:
        raise ValueError("No entiendo la hora. Escríbela así: `22:00` o `21:30`.")

    hour = int(match["hour"])
    minute = int(match["minute"] or 0)
    if hour > 23 or minute > 59:
        raise ValueError("La hora no es válida (usa 00:00 – 23:59).")
    return hour, minute


def combine_local(day: date, hour: int, minute: int) -> datetime:
    """Fecha y hora de Madrid como datetime con zona horaria."""
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=LOCAL_TIMEZONE)


def item_start(item: dict[str, Any]) -> datetime | None:
    raw = item.get("starts_at")
    if not isinstance(raw, str):
        return None
    try:
        value = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def item_attendees(item: dict[str, Any]) -> list[int]:
    attendees = item.get("attendees")
    if not isinstance(attendees, list):
        return []
    result: list[int] = []
    for user_id in attendees:
        try:
            result.append(int(user_id))
        except (TypeError, ValueError):
            continue
    return result


class BillboardStore:
    """Cartelera por servidor: canal del panel, rol editor y sesiones/anuncios."""

    def __init__(
        self,
        json_path: str = "billboard.json",
        posters_dir: str = "billboard_posters",
    ) -> None:
        self.json_path = Path(json_path)
        self.posters_dir = Path(posters_dir)
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

    def _items(self, guild_id: int) -> dict[str, dict[str, Any]]:
        guild_data = self._guild_data(guild_id)
        items = guild_data.get("items")
        if not isinstance(items, dict):
            items = {}
            guild_data["items"] = items
        return items

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

    # Sesiones y anuncios

    def list_items(self, guild_id: int, kind: str | None = None) -> list[dict[str, Any]]:
        """Devuelve copias ordenadas: primero por fecha, los que no tienen al final."""
        items = [
            {**item, "id": item_id}
            for item_id, item in self._items(guild_id).items()
            if isinstance(item, dict) and (kind is None or item.get("kind") == kind)
        ]
        far_future = datetime.max.replace(tzinfo=timezone.utc)
        items.sort(
            key=lambda item: (item_start(item) or far_future, str(item.get("title", "")))
        )
        return items

    def get_item(self, guild_id: int, item_id: str) -> dict[str, Any] | None:
        item = self._items(guild_id).get(item_id)
        return {**item, "id": item_id} if isinstance(item, dict) else None

    def add_item(
        self,
        guild_id: int,
        *,
        kind: str,
        title: str,
        starts_at: datetime | None,
        description: str = "",
        language: str = "",
        created_by: int | None = None,
    ) -> str:
        items = self._items(guild_id)
        item_id = secrets.token_hex(3)
        while item_id in items:
            item_id = secrets.token_hex(3)

        items[item_id] = {
            "kind": kind,
            "title": title,
            "starts_at": starts_at.astimezone(timezone.utc).isoformat()
            if starts_at is not None
            else None,
            "description": description,
            "language": language,
            "poster": None,
            "attendees": [],
            "reminded": False,
            "reminder_message": None,
            "created_by": str(created_by) if created_by else None,
        }
        self._save()
        return item_id

    def update_item(
        self,
        guild_id: int,
        item_id: str,
        *,
        title: str,
        starts_at: datetime | None,
        description: str,
        language: str = "",
    ) -> bool:
        item = self._items(guild_id).get(item_id)
        if not isinstance(item, dict):
            return False

        new_start = (
            starts_at.astimezone(timezone.utc).isoformat() if starts_at is not None else None
        )
        if new_start != item.get("starts_at"):
            # Si cambia la hora, el recordatorio se vuelve a enviar a la nueva hora.
            item["reminded"] = False
        item.update(
            title=title,
            starts_at=new_start,
            description=description,
            language=language,
        )
        self._save()
        return True

    def remove_item(self, guild_id: int, item_id: str) -> dict[str, Any] | None:
        item = self._items(guild_id).pop(item_id, None)
        if not isinstance(item, dict):
            return None
        self._save()
        self._delete_poster_file(item.get("poster"))
        return {**item, "id": item_id}

    def toggle_attendee(self, guild_id: int, item_id: str, user_id: int) -> bool | None:
        """Apunta o desapunta. Devuelve si queda apuntado, o None si no existe."""
        item = self._items(guild_id).get(item_id)
        if not isinstance(item, dict):
            return None

        attendees = item_attendees(item)
        if user_id in attendees:
            attendees.remove(user_id)
            joined = False
        else:
            attendees.append(user_id)
            joined = True
        item["attendees"] = [str(attendee) for attendee in attendees]
        self._save()
        return joined

    def set_attendees(self, guild_id: int, item_id: str, user_ids: list[int]) -> bool:
        """Sustituye la lista de apuntados (lo usa el editor), sin repetidos."""
        item = self._items(guild_id).get(item_id)
        if not isinstance(item, dict):
            return False
        item["attendees"] = [str(user_id) for user_id in dict.fromkeys(user_ids)]
        self._save()
        return True

    def mark_reminded(
        self, guild_id: int, item_id: str, channel_id: int | None, message_id: int | None
    ) -> None:
        item = self._items(guild_id).get(item_id)
        if not isinstance(item, dict):
            return
        item["reminded"] = True
        item["reminder_message"] = (
            [str(channel_id), str(message_id)] if channel_id and message_id else None
        )
        self._save()

    # Pósters (se guardan en disco: las URLs de Discord caducan)

    def poster_path(self, item: dict[str, Any]) -> Path | None:
        poster = item.get("poster")
        if not isinstance(poster, str) or not poster:
            return None
        path = self.posters_dir / poster
        return path if path.is_file() else None

    def set_poster(
        self, guild_id: int, item_id: str, data: bytes | None, extension: str = ".png"
    ) -> bool:
        item = self._items(guild_id).get(item_id)
        if not isinstance(item, dict):
            return False

        self._delete_poster_file(item.get("poster"))
        item["poster"] = None
        if data is not None:
            relative = f"{guild_id}/{item_id}-{secrets.token_hex(2)}{extension}"
            path = self.posters_dir / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            item["poster"] = relative
        self._save()
        return True

    def _delete_poster_file(self, poster: Any) -> None:
        if not isinstance(poster, str) or not poster:
            return
        path = self.posters_dir / poster
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
