"""Stickers: mensajes fijos que Noah mantiene siempre al final de un canal.

Los stickers en sí (su contenido) están definidos en código, en `commands/stickers.py`.
Aquí solo se guarda en `stickers.json` qué sticker va en qué canal y cuál es el
mensaje publicado, para poder borrarlo al volver a mandarlo abajo.
"""

import json
import os
import threading
from pathlib import Path
from typing import Any


class StickersStore:
    def __init__(self, json_path: str = "stickers.json") -> None:
        self._path = Path(json_path)
        self._lock = threading.RLock()
        self._state = self._load()

    def _load(self) -> dict[str, Any]:
        state: dict[str, Any] = {}
        if self._path.exists():
            try:
                state = json.loads(self._path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                state = {}
        state.setdefault("guilds", {})
        return state

    def _save(self) -> None:
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self._path)

    def get(self, guild_id: int, channel_id: int) -> dict[str, Any] | None:
        """`{"sticker": clave, "message_id": id | None}` o `None` si el canal no tiene."""
        with self._lock:
            entry = self._state["guilds"].get(str(guild_id), {}).get(str(channel_id))
            return dict(entry) if entry is not None else None

    def channels(self, guild_id: int) -> dict[int, dict[str, Any]]:
        with self._lock:
            guild = self._state["guilds"].get(str(guild_id), {})
            return {int(channel_id): dict(entry) for channel_id, entry in guild.items()}

    def guild_ids(self) -> list[int]:
        with self._lock:
            return [int(guild_id) for guild_id, channels in self._state["guilds"].items() if channels]

    def set(self, guild_id: int, channel_id: int, sticker: str) -> dict[str, Any] | None:
        """Pone `sticker` en el canal. Devuelve lo que había antes (para borrar su mensaje)."""
        with self._lock:
            guild = self._state["guilds"].setdefault(str(guild_id), {})
            previous = guild.get(str(channel_id))
            guild[str(channel_id)] = {
                "sticker": sticker,
                "message_id": previous.get("message_id") if previous else None,
            }
            self._save()
            return dict(previous) if previous is not None else None

    def remove(self, guild_id: int, channel_id: int) -> dict[str, Any] | None:
        with self._lock:
            guild = self._state["guilds"].get(str(guild_id), {})
            entry = guild.pop(str(channel_id), None)
            if entry is not None:
                self._save()
            return entry

    def set_message(self, guild_id: int, channel_id: int, message_id: int | None) -> None:
        with self._lock:
            entry = self._state["guilds"].get(str(guild_id), {}).get(str(channel_id))
            if entry is None:
                return
            entry["message_id"] = message_id
            self._save()
