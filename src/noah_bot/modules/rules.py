"""Normas del servidor: la lista y los textos del panel de `.noah ruleboard`.

Se guarda en `rules.json` por servidor: las normas (emoji + texto), los textos del
panel (título, soporte, incumplimiento, firma), el color, el canal de tickets y dónde
está publicado el mensaje para poder editarlo en sitio cada vez que algo cambia.
"""

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

DEFAULT_RULES: tuple[tuple[str, str], ...] = (
    ("🔞", "Edad obligatoria entre 18 y 30 años."),
    ("💔", "Prohibido utilizar el servidor con la intención de ligar."),
    ("✉️", "Prohibido abrir MD sin pedir permiso."),
    ("🤝", "Respetar a las personas dentro y fuera del servidor."),
    ("🛡️", "Prohibido ridiculizar o menospreciar a otros miembros por cualquier motivo."),
    ("🫂", "Evita comportamientos que generen incomodidad o dificulten tu integración en la comunidad."),
    ("💬", "Prohibido abordar recurrentemente temas políticos o afines."),
    ("⛔", "No enviar contenido pornográfico o NSFW."),
    ("🔔", "No hacer spam."),
    ("📁", "Respetar el uso de los canales de texto."),
    ("⏳", "Mantener un mínimo de actividad."),
)

DEFAULT_COLOR = 0xE891B4

# Campos de texto que se cambian con `.noah ruleboard set <campo> <texto>`.
TEXT_FIELDS: dict[str, str] = {
    "titulo": "𝙉𝙊𝙍𝙈𝘼𝙎 𝘿𝙀 𝙇𝘼 𝘾𝙊𝙈𝙐𝙉𝙄𝘿𝘼𝘿",
    "soporte_titulo": "⊹ 𝙎𝙊𝙋𝙊𝙍𝙏𝙀 𝙔 𝘾𝙊𝙉𝙑𝙄𝙑𝙀𝙉𝘾𝙄𝘼",
    "soporte": (
        "ⓘ Ante cualquier problema dentro del servidor, tenéis a vuestra disposición el canal "
        "{tickets} para informar de cualquier actividad inapropiada."
    ),
    "incumplimiento_titulo": "⊹ 𝙄𝙉𝘾𝙐𝙈𝙋𝙇𝙄𝙈𝙄𝙀𝙉𝙏𝙊",
    "incumplimiento": (
        "El incumplimiento total o parcial de cualquiera de estas normas será valorado por la "
        "administración y podrá ser motivo de expulsión."
    ),
    "firma": "𝑨𝒎𝒊𝒈𝒐𝒔 𝑴𝒂𝒅𝒓𝒊𝒅 ♡",
}


class RulesError(ValueError):
    """Error con un mensaje listo para enseñar al usuario."""


class RulesStore:
    def __init__(self, json_path: str = "rules.json") -> None:
        self._path = Path(json_path)
        self._lock = threading.RLock()
        self._state = self._load()

    # ---------- persistencia ----------

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

    def _guild(self, guild_id: int) -> dict[str, Any]:
        guild = self._state["guilds"].get(str(guild_id))
        if guild is None:
            guild = {
                "rules": [{"emoji": emoji, "text": text} for emoji, text in DEFAULT_RULES],
                "texts": {},
                "color": DEFAULT_COLOR,
                "tickets_channel_id": None,
                "channel_id": None,
                "message_id": None,
                "updated_at": int(time.time()),
            }
            self._state["guilds"][str(guild_id)] = guild
        guild.setdefault("texts", {})
        return guild

    def _touch(self, guild: dict[str, Any]) -> None:
        guild["updated_at"] = int(time.time())
        self._save()

    # ---------- lectura ----------

    def get(self, guild_id: int) -> dict[str, Any]:
        """Copia del estado del servidor con los textos por defecto ya resueltos."""
        with self._lock:
            guild = self._guild(guild_id)
            data = json.loads(json.dumps(guild))
        data["texts"] = {key: data["texts"].get(key, default) for key, default in TEXT_FIELDS.items()}
        return data

    # ---------- normas ----------

    def _index(self, guild: dict[str, Any], number: int) -> int:
        rules = guild["rules"]
        if not 1 <= number <= len(rules):
            raise RulesError(f"No existe la norma {number}. Hay {len(rules)}.")
        return number - 1

    def add(self, guild_id: int, emoji: str, text: str, position: int | None = None) -> int:
        with self._lock:
            guild = self._guild(guild_id)
            rules = guild["rules"]
            index = len(rules) if position is None else max(0, min(len(rules), position - 1))
            rules.insert(index, {"emoji": emoji, "text": text})
            self._touch(guild)
            return index + 1

    def edit(self, guild_id: int, number: int, *, emoji: str | None = None, text: str | None = None) -> None:
        with self._lock:
            guild = self._guild(guild_id)
            rule = guild["rules"][self._index(guild, number)]
            if emoji is not None:
                rule["emoji"] = emoji
            if text is not None:
                rule["text"] = text
            self._touch(guild)

    def remove(self, guild_id: int, number: int) -> dict[str, str]:
        with self._lock:
            guild = self._guild(guild_id)
            rule = guild["rules"].pop(self._index(guild, number))
            self._touch(guild)
            return rule

    def move(self, guild_id: int, number: int, new_position: int) -> int:
        with self._lock:
            guild = self._guild(guild_id)
            rules = guild["rules"]
            rule = rules.pop(self._index(guild, number))
            index = max(0, min(len(rules), new_position - 1))
            rules.insert(index, rule)
            self._touch(guild)
            return index + 1

    def reset_rules(self, guild_id: int) -> None:
        with self._lock:
            guild = self._guild(guild_id)
            guild["rules"] = [{"emoji": emoji, "text": text} for emoji, text in DEFAULT_RULES]
            self._touch(guild)

    # ---------- textos y ajustes ----------

    def set_text(self, guild_id: int, field: str, value: str | None) -> None:
        """`value=None` vuelve al texto por defecto."""
        if field not in TEXT_FIELDS:
            raise RulesError(f"Campo desconocido: `{field}`.")
        with self._lock:
            guild = self._guild(guild_id)
            if value is None:
                guild["texts"].pop(field, None)
            else:
                guild["texts"][field] = value
            self._touch(guild)

    def set_color(self, guild_id: int, color: int) -> None:
        with self._lock:
            guild = self._guild(guild_id)
            guild["color"] = int(color)
            self._touch(guild)

    def set_tickets_channel(self, guild_id: int, channel_id: int | None) -> None:
        with self._lock:
            guild = self._guild(guild_id)
            guild["tickets_channel_id"] = channel_id
            self._touch(guild)

    def set_message(self, guild_id: int, channel_id: int | None, message_id: int | None) -> None:
        with self._lock:
            guild = self._guild(guild_id)
            guild["channel_id"] = channel_id
            guild["message_id"] = message_id
            self._save()
