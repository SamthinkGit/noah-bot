import json
from pathlib import Path
from typing import Any


class ConfessionsStore:
    """Configuración de confesiones por servidor.

    Nunca se guarda nada sobre quién envía cada confesión: solo los canales,
    el mensaje del panel y el contador para numerarlas.
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

    def get_count(self, guild_id: int) -> int:
        return self._get_int(guild_id, "count") or 0

    def next_number(self, guild_id: int) -> int:
        number = self.get_count(guild_id) + 1
        self._guild_data(guild_id)["count"] = number
        self._save()
        return number
