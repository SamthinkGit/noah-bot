"""Emojis de la aplicación para el panel de normas (`.noah ruleboard`).

Los componentes V2 solo admiten imágenes pequeñas en línea como emojis, así que Noah
sube como emojis propios de la aplicación (no gastan huecos del server):

- los iconos de línea del catálogo (Tabler Icons), pintados del color del panel;
- el icono del botón «Ir a tickets», en blanco;
- el logo del server recortado en círculo, para la firma del pie.

Se suben una vez y se reutilizan; si cambia el color o el icono del server se suben
los nuevos y se borran los que ya no se usan.
"""

import asyncio
import io
import logging
from pathlib import Path

import discord
from PIL import Image, ImageDraw

log = logging.getLogger(__name__)

ICONS_DIR = Path(__file__).resolve().parent.parent / "assets" / "ruleboard_icons"
BUTTON_ICON_COLOR = 0xFFFFFF
BUTTON_ICON = "ticket"
ICON_PREFIX = "rb_"

# Catálogo de iconos para las normas (máximo 25: es un desplegable). Clave -> etiqueta.
ICON_LABELS: dict[str, str] = {
    "rating-18-plus": "Edad",
    "heart-broken": "Ligar",
    "mail": "Mensajes privados",
    "heart-handshake": "Respeto",
    "shield": "Protección",
    "users": "Comunidad",
    "message-circle": "Temas de conversación",
    "ban": "Prohibido",
    "bell": "Spam y avisos",
    "folder": "Canales",
    "hourglass": "Actividad",
    "alert-triangle": "Aviso",
    "lock": "Privacidad",
    "eye-off": "Contenido sensible",
    "photo": "Imágenes",
    "microphone": "Voz",
    "link": "Enlaces",
    "speakerphone": "Publicidad",
    "gavel": "Sanciones",
    "info-circle": "Información",
    "crown": "Staff",
    "mood-smile": "Buen rollo",
    "star": "Destacado",
    "user-x": "Expulsión",
    "ticket": "Tickets",
}


def icon_name(key: str, color: int) -> str:
    """Nombre del emoji (2-32 caracteres, letras, números y _)."""
    return f"{ICON_PREFIX}{key.replace('-', '_')[:18]}_{color:06x}"


def logo_name(guild_id: int, icon_key: str) -> str:
    return f"g{guild_id}_{icon_key.removeprefix('a_')[:8]}"


def _png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def tinted_icon(key: str, color: int) -> bytes:
    """El icono blanco del catálogo pintado de `color`, conservando su transparencia."""
    with Image.open(ICONS_DIR / f"{key}.png") as source:
        alpha = source.convert("RGBA").getchannel("A")
    rgb = ((color >> 16) & 0xFF, (color >> 8) & 0xFF, color & 0xFF, 255)
    image = Image.new("RGBA", alpha.size, rgb)
    image.putalpha(alpha)
    return _png(image)


def round_logo(data: bytes, size: int = 128) -> bytes:
    """Recorta el logo en círculo (como el icono del pie de un embed)."""
    with Image.open(io.BytesIO(data)) as source:
        image = source.convert("RGBA").resize((size, size), Image.LANCZOS)
    scale = 4
    mask = Image.new("L", (size * scale, size * scale), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size * scale - 1, size * scale - 1), fill=255)
    mask = mask.resize((size, size), Image.LANCZOS)
    alpha = Image.composite(image.getchannel("A"), Image.new("L", (size, size), 0), mask)
    image.putalpha(alpha)
    return _png(image)


class RuleboardEmojis:
    def __init__(self) -> None:
        self._emojis: dict[str, discord.Emoji] = {}
        self._loaded = False
        self._lock = asyncio.Lock()

    # ---------- lectura (síncrona, para pintar) ----------

    def _get(self, name: str) -> str | None:
        emoji = self._emojis.get(name)
        return str(emoji) if emoji is not None else None

    def icon(self, key: str | None, color: int) -> str | None:
        if not key:
            return None
        return self._get(icon_name(key, color))

    def button_icon(self) -> discord.PartialEmoji | None:
        emoji = self._emojis.get(icon_name(BUTTON_ICON, BUTTON_ICON_COLOR))
        return discord.PartialEmoji(name=emoji.name, id=emoji.id) if emoji is not None else None

    def logo(self, guild: discord.Guild) -> str | None:
        if guild.icon is None:
            return None
        return self._get(logo_name(guild.id, guild.icon.key))

    # ---------- subida ----------

    async def _create(self, bot: discord.Client, name: str, image: bytes) -> None:
        emoji = await bot.create_application_emoji(name=name, image=image)
        self._emojis[emoji.name] = emoji

    async def _delete(self, name: str) -> None:
        emoji = self._emojis.pop(name, None)
        if emoji is not None:
            try:
                await emoji.delete()
            except discord.HTTPException:
                log.warning("No he podido borrar el emoji %s", name, exc_info=True)

    async def prepare(
        self,
        bot: discord.Client,
        guild: discord.Guild,
        color: int,
        keep_colors: set[int] | None = None,
    ) -> list[str]:
        """Sube lo que falte. Devuelve los problemas encontrados (vacío si todo bien)."""
        problems: list[str] = []
        async with self._lock:
            if not self._loaded:
                try:
                    for emoji in await bot.fetch_application_emojis():
                        self._emojis[emoji.name] = emoji
                    self._loaded = True
                except discord.HTTPException as error:
                    return [f"no puedo leer los emojis de la aplicación ({error.status}: {error.text or error})"]

            # Iconos de las normas del color del panel y el del botón en blanco.
            wanted = {icon_name(key, color): (key, color) for key in ICON_LABELS}
            wanted[icon_name(BUTTON_ICON, BUTTON_ICON_COLOR)] = (BUTTON_ICON, BUTTON_ICON_COLOR)
            for name, (key, tint) in wanted.items():
                if name in self._emojis:
                    continue
                try:
                    await self._create(bot, name, tinted_icon(key, tint))
                except (discord.HTTPException, OSError) as error:
                    problems.append(f"icono {key}: {getattr(error, 'text', None) or error}")
                    break

            # Borra los iconos de colores que ya no usa ningún panel.
            keep = {color, BUTTON_ICON_COLOR, *(keep_colors or set())}
            keep_suffixes = {f"_{tint:06x}" for tint in keep}
            for name in list(self._emojis):
                if name.startswith(ICON_PREFIX) and not any(name.endswith(s) for s in keep_suffixes):
                    await self._delete(name)

            # Logo del server en círculo.
            if guild.icon is not None:
                name = logo_name(guild.id, guild.icon.key)
                if name not in self._emojis:
                    try:
                        raw = await guild.icon.replace(size=128, format="png").read()
                        await self._create(bot, name, round_logo(raw))
                    except (discord.HTTPException, OSError, ValueError) as error:
                        problems.append(f"logo del server: {getattr(error, 'text', None) or error}")
                for old in [n for n in self._emojis if n.startswith(f"g{guild.id}_") and n != name]:
                    await self._delete(old)

        for problem in problems:
            log.warning("Ruleboard: %s", problem)
        return problems
