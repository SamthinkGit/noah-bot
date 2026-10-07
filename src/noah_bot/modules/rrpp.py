import json
import time
from pathlib import Path
from typing import Any


DEFAULT_RRPP_EMOJI = "📩"


class RrppStore:
    def __init__(self, json_path: str = "rrpp.json") -> None:
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
        guild_data = self._guilds.setdefault(str(guild_id), {})
        if not isinstance(guild_data.get("messages"), dict):
            guild_data["messages"] = {}
        return guild_data

    def _get_int(self, guild_id: int, key: str) -> int | None:
        guild_data = self._guilds.get(str(guild_id))
        if not isinstance(guild_data, dict):
            return None

        try:
            return int(guild_data[key])
        except (KeyError, TypeError, ValueError):
            return None

    def set_role(self, guild_id: int, role_id: int | None) -> None:
        self._guild_data(guild_id)["role_id"] = str(role_id) if role_id else None
        self._save()

    def get_role_id(self, guild_id: int) -> int | None:
        return self._get_int(guild_id, "role_id")

    def set_channel(self, guild_id: int, channel_id: int | None) -> None:
        self._guild_data(guild_id)["channel_id"] = str(channel_id) if channel_id else None
        self._save()

    def get_channel_id(self, guild_id: int) -> int | None:
        return self._get_int(guild_id, "channel_id")

    def set_emoji(self, guild_id: int, emoji: str) -> None:
        self._guild_data(guild_id)["emoji"] = emoji
        self._save()

    def get_emoji(self, guild_id: int) -> str:
        guild_data = self._guilds.get(str(guild_id))
        emoji = guild_data.get("emoji") if isinstance(guild_data, dict) else None
        return emoji if isinstance(emoji, str) and emoji else DEFAULT_RRPP_EMOJI

    def add_message(
        self,
        guild_id: int,
        channel_id: int,
        message_id: int,
        quoted_user_id: int,
        emoji: str,
    ) -> None:
        self._guild_data(guild_id)["messages"][str(message_id)] = {
            "channel_id": str(channel_id),
            "quoted_user_id": str(quoted_user_id),
            "emoji": emoji,
            "created_at": time.time(),
            "notified_users": [],
        }
        self._save()

    def get_message(self, guild_id: int, message_id: int) -> dict[str, Any] | None:
        guild_data = self._guilds.get(str(guild_id))
        if not isinstance(guild_data, dict):
            return None

        messages = guild_data.get("messages")
        if not isinstance(messages, dict):
            return None

        payload = messages.get(str(message_id))
        return payload if isinstance(payload, dict) else None

    def list_messages(self, guild_id: int) -> list[tuple[int, dict[str, Any]]]:
        """Devuelve los mensajes RRPP activos, del más reciente al más antiguo."""
        guild_data = self._guilds.get(str(guild_id))
        messages = guild_data.get("messages") if isinstance(guild_data, dict) else None
        if not isinstance(messages, dict):
            return []

        result: list[tuple[int, dict[str, Any]]] = []
        for message_key, payload in messages.items():
            if not isinstance(payload, dict):
                continue
            try:
                result.append((int(message_key), payload))
            except (TypeError, ValueError):
                continue

        result.sort(key=lambda item: item[1].get("created_at", 0), reverse=True)
        return result

    def remove_message(self, guild_id: int, message_id: int) -> bool:
        guild_data = self._guilds.get(str(guild_id))
        messages = guild_data.get("messages") if isinstance(guild_data, dict) else None
        if not isinstance(messages, dict) or messages.pop(str(message_id), None) is None:
            return False

        self._save()
        return True

    def mark_notified(self, guild_id: int, message_id: int, user_id: int) -> bool:
        """Registra que se ha avisado por este usuario. Devuelve False si ya estaba."""
        payload = self.get_message(guild_id, message_id)
        if payload is None:
            return False

        notified = payload.get("notified_users")
        if not isinstance(notified, list):
            notified = []
            payload["notified_users"] = notified

        user_key = str(user_id)
        if user_key in notified:
            return False

        notified.append(user_key)
        self._save()
        return True

    def unmark_notified(self, guild_id: int, message_id: int, user_id: int) -> None:
        payload = self.get_message(guild_id, message_id)
        notified = payload.get("notified_users") if payload is not None else None
        if isinstance(notified, list) and str(user_id) in notified:
            notified.remove(str(user_id))
            self._save()
