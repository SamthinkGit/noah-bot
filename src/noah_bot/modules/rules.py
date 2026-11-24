"""Normas del servidor: la lista y los textos del panel de `.noah ruleboard`.

Se guarda en `rules.json` por servidor: las normas (icono o emoji + texto), los textos del
panel (título, soporte, incumplimiento, firma), el color, el canal de tickets y dónde
está publicado el mensaje para poder editarlo en sitio cada vez que algo cambia.
"""

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

# Icono del catálogo (ver `ruleboard_emojis.ICON_LABELS`) y texto de cada norma.
DEFAULT_RULES: tuple[tuple[str, str], ...] = (
    ("rating-18-plus", "Edad obligatoria entre 18 y 30 años."),
    ("heart-broken", "Prohibido utilizar el servidor con la intención de ligar."),
    ("mail", "Prohibido abrir MD sin pedir permiso."),
    ("heart-handshake", "Respetar a las personas dentro y fuera del servidor."),
    ("shield", "Prohibido ridiculizar o menospreciar a otros miembros por cualquier motivo."),
    ("users", "Evita comportamientos que generen incomodidad o dificulten tu integración en la comunidad."),
    ("message-circle", "Prohibido abordar recurrentemente temas políticos o afines."),
    ("ban", "No enviar contenido pornográfico o NSFW."),
    ("bell", "No hacer spam."),
    ("folder", "Respetar el uso de los canales de texto."),
    ("hourglass", "Mantener un mínimo de actividad."),
)

# Las primeras versiones guardaban emojis normales: se pasan a su icono equivalente.
UNICODE_TO_ICON: dict[str, str] = {
    "🔞": "rating-18-plus",
    "💔": "heart-broken",
    "✉️": "mail",
    "✉": "mail",
    "🤝": "heart-handshake",
    "🛡️": "shield",
    "🛡": "shield",
    "🫂": "users",
    "💬": "message-circle",
    "⛔": "ban",
    "🔔": "bell",
    "📁": "folder",
    "⏳": "hourglass",
}

DEFAULT_COLOR = 0x22D3EE  # azul cian (franja e iconos)
OLD_DEFAULT_COLORS = {0xE891B4}  # rosa de la primera versión

# Textos del panel que se cambian desde `.noah ruleboard config` (vacío = el original).
TEXT_FIELDS: dict[str, str] = {
    "titulo": "𝙉𝙊𝙍𝙈𝘼𝙎 𝘿𝙀 𝙇𝘼 𝘾𝙊𝙈𝙐𝙉𝙄𝘿𝘼𝘿",
    "soporte_titulo": "⊹ Soporte",
    "soporte": "¿Algún problema? Abre un ticket.",
    "incumplimiento_titulo": "",
    "incumplimiento": (
        "El incumplimiento total o parcial será valorado por la administración y podrá ser "
        "motivo de expulsión."
    ),
    "firma": "𝑨𝒎𝒊𝒈𝒐𝒔 𝑴𝒂𝒅𝒓𝒊𝒅 ♡",
}


def default_rules() -> list[dict[str, str | None]]:
    return [{"icon": icon, "emoji": None, "text": text} for icon, text in DEFAULT_RULES]


def _migrate_rule(rule: dict[str, Any]) -> None:
    if "icon" in rule:
        return
    icon = UNICODE_TO_ICON.get(rule.get("emoji") or "")
    rule["icon"] = icon
    if icon is not None or rule.get("emoji") == "•":
        rule["emoji"] = None


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
                "rules": default_rules(),
                "texts": {},
                "color": DEFAULT_COLOR,
                "tickets_channel_id": None,
                "channel_id": None,
                "message_id": None,
                "updated_at": int(time.time()),
            }
            self._state["guilds"][str(guild_id)] = guild
        guild.setdefault("texts", {})
        for rule in guild["rules"]:
            _migrate_rule(rule)
        if guild.get("color") in OLD_DEFAULT_COLORS:
            guild["color"] = DEFAULT_COLOR
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

    def colors(self) -> set[int]:
        """Colores de todos los paneles (para no borrar iconos que alguno use)."""
        with self._lock:
            return {int(guild.get("color", DEFAULT_COLOR)) for guild in self._state["guilds"].values()}

    # ---------- normas ----------

    def _index(self, guild: dict[str, Any], number: int) -> int:
        rules = guild["rules"]
        if not 1 <= number <= len(rules):
            raise RulesError(f"No existe la norma {number}. Hay {len(rules)}.")
        return number - 1

    def add(
        self,
        guild_id: int,
        text: str,
        *,
        icon: str | None = None,
        emoji: str | None = None,
        position: int | None = None,
    ) -> int:
        with self._lock:
            guild = self._guild(guild_id)
            rules = guild["rules"]
            index = len(rules) if position is None else max(0, min(len(rules), position - 1))
            rules.insert(index, {"icon": icon, "emoji": emoji, "text": text})
            self._touch(guild)
            return index + 1

    def edit(
        self, guild_id: int, number: int, *, text: str, icon: str | None = None, emoji: str | None = None
    ) -> None:
        with self._lock:
            guild = self._guild(guild_id)
            rule = guild["rules"][self._index(guild, number)]
            rule.update(icon=icon, emoji=emoji, text=text)
            self._touch(guild)

    def remove(self, guild_id: int, number: int) -> dict[str, Any]:
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
            guild["rules"] = default_rules()
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
