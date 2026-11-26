import hashlib
import hmac
import json
import secrets
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo("Europe/Madrid")
MAX_DAILY_LIMIT = 50


class DailyQuota:
    """Cuántas confesiones lleva hoy cada persona, solo en memoria.

    No se guarda el id de nadie: cada persona se identifica con un hash con una clave
    aleatoria que se crea al arrancar el bot. Nada se escribe a disco, no se relaciona
    con ninguna confesión y todo se borra al cambiar el día (hora de Madrid) o al
    reiniciar el bot.
    """

    def __init__(self) -> None:
        self._secret = secrets.token_bytes(32)
        self._day: date | None = None
        self._counts: dict[str, int] = {}

    def _key(self, guild_id: int, user_id: int) -> str:
        return hmac.new(self._secret, f"{guild_id}:{user_id}".encode(), hashlib.sha256).hexdigest()

    def _roll(self) -> None:
        today = datetime.now(LOCAL_TZ).date()
        if today != self._day:
            self._day = today
            self._counts.clear()

    def used(self, guild_id: int, user_id: int) -> int:
        self._roll()
        return self._counts.get(self._key(guild_id, user_id), 0)

    def take(self, guild_id: int, user_id: int, limit: int | None) -> bool:
        """Gasta una confesión del día. `False` si ya llegó al límite."""
        self._roll()
        key = self._key(guild_id, user_id)
        used = self._counts.get(key, 0)
        if limit is not None and used >= limit:
            return False
        self._counts[key] = used + 1
        return True

    def give_back(self, guild_id: int, user_id: int) -> None:
        """Devuelve la confesión gastada si al final no se pudo publicar."""
        key = self._key(guild_id, user_id)
        if self._counts.get(key, 0) > 0:
            self._counts[key] -= 1


class ConfessionsStore:
    """Configuración de confesiones por servidor.

    Nunca se guarda nada sobre quién envía cada confesión: solo los canales,
    el mensaje del panel, el contador para numerarlas y el límite diario. Lo que lleva
    cada persona hoy está en `quota`, solo en memoria.
    """

    def __init__(self, json_path: str = "confessions.json") -> None:
        self.json_path = Path(json_path)
        self._guilds: dict[str, dict[str, Any]] = self._load()
        self.quota = DailyQuota()

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

    def set_channel(self, guild_id: int, channel_id: int | None) -> None:
        self._guild_data(guild_id)["channel_id"] = str(channel_id) if channel_id else None
        self._save()

    def get_channel_id(self, guild_id: int) -> int | None:
        return self._get_int(guild_id, "channel_id")

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

    def set_sticky(self, guild_id: int, enabled: bool) -> None:
        self._guild_data(guild_id)["sticky"] = enabled
        self._save()

    def is_sticky(self, guild_id: int) -> bool:
        guild_data = self._guilds.get(str(guild_id))
        return isinstance(guild_data, dict) and guild_data.get("sticky") is True

    def get_sticky_guild_ids(self) -> list[int]:
        guild_ids: list[int] = []
        for guild_key in self._guilds:
            try:
                guild_id = int(guild_key)
            except ValueError:
                continue
            if self.is_sticky(guild_id):
                guild_ids.append(guild_id)
        return guild_ids

    def get_count(self, guild_id: int) -> int:
        return self._get_int(guild_id, "count") or 0

    def next_number(self, guild_id: int) -> int:
        number = self.get_count(guild_id) + 1
        self._guild_data(guild_id)["count"] = number
        self._save()
        return number

    def set_daily_limit(self, guild_id: int, limit: int | None) -> None:
        """`None` o 0 = sin límite."""
        self._guild_data(guild_id)["daily_limit"] = limit if limit else None
        self._save()

    def get_daily_limit(self, guild_id: int) -> int | None:
        return self._get_int(guild_id, "daily_limit") or None
