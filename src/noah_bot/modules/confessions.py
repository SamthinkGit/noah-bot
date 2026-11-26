import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo("Europe/Madrid")
MAX_DAILY_LIMIT = 50


class ConfessionsStore:
    """Configuración de confesiones por servidor.

    Nunca se guarda qué ha escrito cada persona ni qué confesión es de quién: solo los
    canales, el mensaje del panel, el contador para numerarlas, el límite diario y
    cuántas confesiones lleva hoy cada persona (para aplicar ese límite). Ese consumo
    solo guarda el día de hoy: al cambiar de día se borra.
    """

    def __init__(self, json_path: str = "confessions.json") -> None:
        self.json_path = Path(json_path)
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

    # ---------- consumo diario por persona ----------

    def _usage_today(self, guild_id: int) -> dict[str, int]:
        """Confesiones de hoy por persona (`{user_id: n}`). Lo de otros días se descarta."""
        today = datetime.now(LOCAL_TZ).date().isoformat()
        guild_data = self._guild_data(guild_id)
        usage = guild_data.get("daily_usage")
        if not isinstance(usage, dict) or usage.get("day") != today or not isinstance(usage.get("users"), dict):
            usage = {"day": today, "users": {}}
            guild_data["daily_usage"] = usage
        return usage["users"]

    def used_today(self, guild_id: int, user_id: int) -> int:
        try:
            return int(self._usage_today(guild_id).get(str(user_id), 0))
        except (TypeError, ValueError):
            return 0

    def take_daily(self, guild_id: int, user_id: int, limit: int | None) -> bool:
        """Gasta una confesión del día de la persona. `False` si ya llegó al límite."""
        used = self.used_today(guild_id, user_id)
        if limit is not None and used >= limit:
            return False
        self._usage_today(guild_id)[str(user_id)] = used + 1
        self._save()
        return True

    def give_back_daily(self, guild_id: int, user_id: int) -> None:
        """Devuelve la confesión gastada si al final no se pudo publicar."""
        users = self._usage_today(guild_id)
        used = self.used_today(guild_id, user_id)
        if used <= 1:
            users.pop(str(user_id), None)
        else:
            users[str(user_id)] = used - 1
        self._save()
