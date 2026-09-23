import json
from pathlib import Path
from typing import Any


class PresentationsStore:
    def __init__(self, json_path: str = "presentations.json") -> None:
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
        completed = guild_data.get("completed_users")
        if not isinstance(completed, list):
            guild_data["completed_users"] = []
        return guild_data

    def set_channel(self, guild_id: int, channel_id: int) -> None:
        self._guild_data(guild_id)["channel_id"] = str(channel_id)
        self._save()

    def get_channel_id(self, guild_id: int) -> int | None:
        guild_data = self._guilds.get(str(guild_id))
        if not isinstance(guild_data, dict):
            return None

        try:
            return int(guild_data["channel_id"])
        except (KeyError, TypeError, ValueError):
            return None

    def set_enabled(self, guild_id: int, enabled: bool) -> None:
        self._guild_data(guild_id)["enabled"] = enabled
        self._save()

    def is_enabled(self, guild_id: int) -> bool:
        guild_data = self._guilds.get(str(guild_id))
        if not isinstance(guild_data, dict):
            return False
        return guild_data.get("enabled") is True

    def has_completed(self, guild_id: int, user_id: int) -> bool:
        guild_data = self._guilds.get(str(guild_id))
        if not isinstance(guild_data, dict):
            return False

        completed = guild_data.get("completed_users")
        return isinstance(completed, list) and str(user_id) in completed

    def mark_completed(self, guild_id: int, user_ids: list[int]) -> int:
        """Marca usuarios como presentados y devuelve cuántos eran nuevos."""
        completed: list[str] = self._guild_data(guild_id)["completed_users"]
        known = set(completed)
        added = 0

        for user_id in user_ids:
            user_key = str(user_id)
            if user_key in known:
                continue
            completed.append(user_key)
            known.add(user_key)
            added += 1

        if added:
            self._save()
        return added

    def record_presentations(
        self,
        guild_id: int,
        messages: dict[int, tuple[int, int]],
    ) -> int:
        """Marca como presentados a los autores y guarda su primer mensaje (canal, mensaje).

        No sobreescribe mensajes ya guardados. Devuelve cuántos usuarios eran nuevos.
        """
        guild_data = self._guild_data(guild_id)
        completed: list[str] = guild_data["completed_users"]
        stored_messages = guild_data.get("presentation_messages")
        if not isinstance(stored_messages, dict):
            stored_messages = {}
            guild_data["presentation_messages"] = stored_messages

        known = set(completed)
        added = 0
        changed = False

        for user_id, (channel_id, message_id) in messages.items():
            user_key = str(user_id)
            if user_key not in known:
                completed.append(user_key)
                known.add(user_key)
                added += 1
                changed = True

            if user_key not in stored_messages:
                stored_messages[user_key] = {
                    "channel_id": str(channel_id),
                    "message_id": str(message_id),
                }
                changed = True

        if changed:
            self._save()
        return added

    def get_presentation_message(
        self,
        guild_id: int,
        user_id: int,
    ) -> tuple[int, int] | None:
        guild_data = self._guilds.get(str(guild_id))
        if not isinstance(guild_data, dict):
            return None

        stored_messages = guild_data.get("presentation_messages")
        if not isinstance(stored_messages, dict):
            return None

        payload = stored_messages.get(str(user_id))
        if not isinstance(payload, dict):
            return None

        try:
            return int(payload["channel_id"]), int(payload["message_id"])
        except (KeyError, TypeError, ValueError):
            return None

    def forget_presentation_message(self, guild_id: int, user_id: int) -> None:
        guild_data = self._guilds.get(str(guild_id))
        if not isinstance(guild_data, dict):
            return

        stored_messages = guild_data.get("presentation_messages")
        if isinstance(stored_messages, dict) and stored_messages.pop(str(user_id), None):
            self._save()

    def get_completed_user_ids(self, guild_id: int) -> set[int]:
        guild_data = self._guilds.get(str(guild_id))
        if not isinstance(guild_data, dict):
            return set()

        completed = guild_data.get("completed_users")
        if not isinstance(completed, list):
            return set()

        user_ids: set[int] = set()
        for user_key in completed:
            try:
                user_ids.add(int(user_key))
            except (TypeError, ValueError):
                continue
        return user_ids

    def completed_count(self, guild_id: int) -> int:
        guild_data = self._guilds.get(str(guild_id))
        if not isinstance(guild_data, dict):
            return 0

        completed = guild_data.get("completed_users")
        return len(completed) if isinstance(completed, list) else 0
