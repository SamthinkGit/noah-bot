import asyncio
import json
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import discord


FIRST_MESSAGES_LIMIT = 5
DAYS_KEPT = 14
SNIPPET_LIMIT = 90
SAVE_DELAY_SECONDS = 10.0


def local_day(moment: datetime | None = None) -> date:
    return (moment or datetime.now()).astimezone().date()


def message_snippet(message: discord.Message) -> str:
    text = " ".join(message.content.split())
    if not text:
        if message.attachments:
            text = "📎 adjunto"
        elif message.stickers:
            text = "🏷️ sticker"
        else:
            text = "…"
    if len(text) > SNIPPET_LIMIT:
        text = text[: SNIPPET_LIMIT - 1] + "…"
    return text


def message_entry(message: discord.Message) -> dict[str, Any]:
    return {
        "channel_id": str(message.channel.id),
        "message_id": str(message.id),
        "at": message.created_at.timestamp(),
        "text": message_snippet(message),
    }


class UserActivityStore:
    """Cuenta los mensajes de cada usuario por servidor: total, por día y por canal,
    sus primeros mensajes y el último. Se guarda con un pequeño retraso para no
    escribir el JSON en cada mensaje."""

    def __init__(self, json_path: str = "user_activity.json") -> None:
        self.json_path = Path(json_path)
        self._guilds: dict[str, dict[str, Any]] = self._load()
        self._save_handle: asyncio.TimerHandle | None = None

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

    def save(self) -> None:
        if self._save_handle is not None:
            self._save_handle.cancel()
            self._save_handle = None
        temp_path = self.json_path.with_suffix(".json.tmp")
        with temp_path.open("w", encoding="utf-8") as file:
            json.dump({"guilds": self._guilds}, file, indent=2, ensure_ascii=False)
        temp_path.replace(self.json_path)

    def _save_soon(self) -> None:
        if self._save_handle is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.save()
            return
        self._save_handle = loop.call_later(SAVE_DELAY_SECONDS, self.save)

    def _guild_data(self, guild_id: int) -> dict[str, Any]:
        guild_data = self._guilds.setdefault(str(guild_id), {})
        guild_data.setdefault("tracking_since", time.time())
        if not isinstance(guild_data.get("users"), dict):
            guild_data["users"] = {}
        return guild_data

    def _user_data(self, guild_id: int, user_id: int) -> dict[str, Any]:
        users = self._guild_data(guild_id)["users"]
        user = users.setdefault(str(user_id), {})
        user.setdefault("messages", 0)
        for key in ("days", "channels"):
            if not isinstance(user.get(key), dict):
                user[key] = {}
        if not isinstance(user.get("first_messages"), list):
            user["first_messages"] = []
        return user

    def record_message(self, message: discord.Message) -> None:
        if message.guild is None:
            return

        user = self._user_data(message.guild.id, message.author.id)
        entry = message_entry(message)
        user["messages"] = int(user.get("messages", 0)) + 1
        user["last"] = entry

        first = user["first_messages"]
        if len(first) < FIRST_MESSAGES_LIMIT:
            first.append(entry)

        day_key = local_day(message.created_at).isoformat()
        days: dict[str, int] = user["days"]
        days[day_key] = int(days.get(day_key, 0)) + 1
        oldest_kept = (local_day() - timedelta(days=DAYS_KEPT - 1)).isoformat()
        for old_key in [key for key in days if key < oldest_kept]:
            del days[old_key]

        channel_key = str(message.channel.id)
        channels: dict[str, int] = user["channels"]
        channels[channel_key] = int(channels.get(channel_key, 0)) + 1
        self._save_soon()

    def tracking_since(self, guild_id: int) -> float | None:
        guild_data = self._guilds.get(str(guild_id))
        if not isinstance(guild_data, dict):
            return None
        try:
            return float(guild_data["tracking_since"])
        except (KeyError, TypeError, ValueError):
            return None

    def get_user(self, guild_id: int, user_id: int) -> dict[str, Any]:
        """Copia de los datos del usuario (vacía si nunca ha escrito)."""
        guild_data = self._guilds.get(str(guild_id))
        users = guild_data.get("users") if isinstance(guild_data, dict) else None
        user = users.get(str(user_id)) if isinstance(users, dict) else None
        if not isinstance(user, dict):
            return {"messages": 0, "days": {}, "channels": {}, "first_messages": []}
        return json.loads(json.dumps(user))

    def merge_first_messages(
        self, guild_id: int, user_id: int, found: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Añade los primeros mensajes encontrados en el historial y marca al usuario
        como buscado para no repetir la búsqueda."""
        user = self._user_data(guild_id, user_id)
        by_id = {str(entry.get("message_id")): entry for entry in user["first_messages"]}
        for entry in found:
            by_id.setdefault(str(entry.get("message_id")), entry)
        merged = sorted(by_id.values(), key=lambda entry: float(entry.get("at", 0)))
        user["first_messages"] = merged[:FIRST_MESSAGES_LIMIT]
        user["history_scanned"] = True
        self.save()
        return list(user["first_messages"])
