"""`.noah stickers`: mensajes fijos que Noah mantiene siempre al final de un canal.

Cada sticker está definido aquí en código (contenido y diseño). Desde Discord solo se
elige en qué canal va cada uno. Cuando alguien escribe en ese canal, Noah espera unos
segundos de calma, manda el sticker otra vez abajo y borra el anterior.
"""

import asyncio
import io
import logging
import unicodedata
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass

import discord
from discord.ext import commands

from noah_bot.modules.bot_context import get_bot_context, is_guard_active
from noah_bot.modules.help_ui import HelpEntry, HelpModule, HelpSection, register_help, send_help
from noah_bot.modules.sticker_banner import render_music_banner
from noah_bot.modules.stickers import StickersStore

STICKY_DELAY = 5.0  # segundos de calma antes de volver a bajar el sticker
NO_MENTIONS = discord.AllowedMentions.none()
StickerChannel = discord.TextChannel | discord.VoiceChannel
log = logging.getLogger(__name__)


HELP = register_help(
    HelpModule(
        key="stickers",
        emoji="📌",
        name="Stickers",
        summary="Mensajes fijos que siempre quedan al final de un canal",
        description=(
            "Un sticker es un mensaje de Noah que se queda siempre el último de su canal: "
            "cuando alguien escribe, se vuelve a mandar abajo a los pocos segundos."
        ),
        staff=True,
        sections=(
            HelpSection(
                "Comandos",
                (
                    HelpEntry(".noah stickers list", "Stickers disponibles y en qué canales están."),
                    HelpEntry(
                        ".noah stickers set <sticker> [#canal]",
                        "Pone el sticker en el canal (por defecto, este).",
                    ),
                    HelpEntry(".noah stickers remove [#canal]", "Quita el sticker del canal y borra su mensaje."),
                ),
            ),
        ),
    )
)


# ---------------------------------------------------------------------------
# Stickers definidos en código


@dataclass(frozen=True, slots=True)
class Sticker:
    key: str
    name: str
    summary: str
    build: Callable[[discord.Guild], tuple[discord.ui.LayoutView, list[discord.File]]]


MUSIC_COLOR = discord.Color(0x22D3EE)
MUSIC_BANNER = "canal_musica.png"
MUSIC_BOTS = (
    (
        "### Jockie Music\n"
        "-# Prefijo `m!` · también con `/`\n"
        "- `m!play <canción o link>` pone una canción\n"
        "- `m!skip` · `m!queue` · `m!pause` · `m!resume`\n"
        "- `m!stop` para la música y sale"
    ),
    (
        "### Lofi Radio\n"
        "-# Lofi 24/7, sin elegir canciones\n"
        "- `/lofi` entra y pone lofi sin parar\n"
        "- `/mode` cambia de emisora · `+np` qué suena\n"
        "- `/stop` sale del canal"
    ),
    (
        "### Nekotina\n"
        "-# Comandos con `/`\n"
        "- `/play <canción>` pone una canción\n"
        "- `/skip` · `/queue` · `/stop`"
    ),
)


def build_music_sticker(guild: discord.Guild) -> tuple[discord.ui.LayoutView, list[discord.File]]:
    banner = discord.File(io.BytesIO(render_music_banner(guild.name)), filename=MUSIC_BANNER)
    box = discord.ui.Container(accent_color=MUSIC_COLOR)
    box.add_item(
        discord.ui.MediaGallery(
            discord.MediaGalleryItem(f"attachment://{MUSIC_BANNER}", description="Canal de música")
        )
    )
    box.add_item(
        discord.ui.TextDisplay(
            "-# Este es el canal de música. Entra en un canal de voz y pide aquí la música "
            "con uno de estos bots."
        )
    )
    for text in MUSIC_BOTS:
        box.add_item(discord.ui.Separator())
        box.add_item(discord.ui.TextDisplay(text))
    box.add_item(discord.ui.Separator())
    box.add_item(
        discord.ui.TextDisplay(
            "-# 📌 Este canal es solo para comandos de música. Para hablar, usa el chat general."
        )
    )
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(box)
    return view, [banner]


STICKERS: dict[str, Sticker] = {
    sticker.key: sticker
    for sticker in (
        Sticker(
            key="musica",
            name="Canal de música",
            summary="Cómo pedir música con Jockie Music, Lofi Radio y Nekotina.",
            build=build_music_sticker,
        ),
    )
}
ALIASES = {"music": "musica"}


def _sticker_key(raw: str) -> str:
    plain = unicodedata.normalize("NFKD", raw.strip().lower())
    plain = "".join(ch for ch in plain if not unicodedata.combining(ch))
    return ALIASES.get(plain, plain)


# ---------------------------------------------------------------------------
# Utilidades


def _store(bot: discord.Client) -> StickersStore:
    return get_bot_context(bot).stickers


def _can_manage(user: discord.abc.User) -> bool:
    if not isinstance(user, discord.Member):
        return False
    perms = user.guild_permissions
    return user.id == user.guild.owner_id or perms.administrator or perms.manage_guild


def _channel(guild: discord.Guild, channel_id: int) -> StickerChannel | None:
    channel = guild.get_channel(channel_id)
    return channel if isinstance(channel, StickerChannel) else None


async def _delete_message(channel: StickerChannel, message_id: int | None) -> None:
    if message_id is None:
        return
    with suppress(discord.NotFound, discord.Forbidden, discord.HTTPException):
        await channel.get_partial_message(message_id).delete()


# ---------------------------------------------------------------------------
# Publicación


_locks: dict[int, asyncio.Lock] = {}


async def _bump(bot: discord.Client, channel: StickerChannel, *, force: bool = False) -> str | None:
    """Manda el sticker al final del canal y borra el anterior. Devuelve un error, si lo hay.

    Sin `force`, no hace nada si el sticker ya es el último mensaje del canal.
    """
    guild = channel.guild
    store = _store(bot)
    async with _locks.setdefault(channel.id, asyncio.Lock()):
        entry = store.get(guild.id, channel.id)
        if entry is None:
            return None
        old_id = entry.get("message_id")
        if not force and old_id is not None and channel.last_message_id == old_id:
            return None
        sticker = STICKERS.get(entry["sticker"])
        if sticker is None:
            return f"El sticker `{entry['sticker']}` ya no existe."

        view, files = sticker.build(guild)
        try:
            message = await channel.send(view=view, files=files, allowed_mentions=NO_MENTIONS)
        except discord.Forbidden:
            return f"No puedo escribir en {channel.mention}."
        except discord.HTTPException:
            log.exception("No se pudo publicar el sticker %s en %s", entry["sticker"], channel.id)
            return "Discord ha rechazado el mensaje. Inténtalo de nuevo."

        store.set_message(guild.id, channel.id, message.id)
        await _delete_message(channel, old_id)
        return None


async def _refresh(bot: discord.Client, channel: StickerChannel) -> None:
    """Al arrancar: si el sticker sigue el último, se edita en sitio (por si cambió el
    código); si no, se manda abajo."""
    entry = _store(bot).get(channel.guild.id, channel.id)
    if entry is None:
        return
    message_id = entry.get("message_id")
    sticker = STICKERS.get(entry["sticker"])
    if sticker is None or message_id is None or channel.last_message_id != message_id:
        await _bump(bot, channel)
        return
    view, files = sticker.build(channel.guild)
    try:
        await channel.get_partial_message(message_id).edit(
            view=view, attachments=files, allowed_mentions=NO_MENTIONS
        )
    except discord.HTTPException:
        await _bump(bot, channel, force=True)


# ---------------------------------------------------------------------------
# Comandos y listeners


def register_stickers_commands(bot: commands.Bot, noah_group: commands.Group) -> None:
    started = False
    tasks: dict[int, asyncio.Task] = {}
    dirty: set[int] = set()

    async def _worker(channel: StickerChannel) -> None:
        try:
            while True:
                await asyncio.sleep(STICKY_DELAY)
                dirty.discard(channel.id)
                await _bump(bot, channel)
                if channel.id not in dirty:
                    return
        finally:
            if tasks.get(channel.id) is asyncio.current_task():
                tasks.pop(channel.id, None)

    def _schedule(channel: StickerChannel) -> None:
        """Agrupa ráfagas de mensajes en un único reenvío."""
        dirty.add(channel.id)
        pending = tasks.get(channel.id)
        if pending is not None and not pending.done():
            return
        tasks[channel.id] = asyncio.create_task(_worker(channel))

    @bot.listen("on_ready")
    async def _start_stickers() -> None:
        nonlocal started
        if started:
            return
        started = True
        store = _store(bot)
        for guild_id in store.guild_ids():
            guild = bot.get_guild(guild_id)
            if guild is None:
                continue
            for channel_id in store.channels(guild_id):
                channel = _channel(guild, channel_id)
                if channel is not None:
                    await _refresh(bot, channel)

    @bot.listen("on_message")
    async def _keep_sticker_last(message: discord.Message) -> None:
        if message.guild is None or not isinstance(message.channel, StickerChannel):
            return
        entry = _store(bot).get(message.guild.id, message.channel.id)
        if entry is None or message.id == entry.get("message_id"):
            return
        if is_guard_active(bot, message.guild.id):
            return
        _schedule(message.channel)

    @bot.listen("on_raw_message_delete")
    async def _restore_sticker(payload: discord.RawMessageDeleteEvent) -> None:
        if payload.guild_id is None:
            return
        store = _store(bot)
        entry = store.get(payload.guild_id, payload.channel_id)
        if entry is None or entry.get("message_id") != payload.message_id:
            return
        # Si alguien lo borra a mano, se vuelve a mandar (para quitarlo: `.noah stickers remove`).
        store.set_message(payload.guild_id, payload.channel_id, None)
        guild = bot.get_guild(payload.guild_id)
        channel = _channel(guild, payload.channel_id) if guild is not None else None
        if channel is not None:
            _schedule(channel)

    @noah_group.group(name="stickers", invoke_without_command=True)
    async def stickers(ctx: commands.Context) -> None:
        await send_help(ctx, "stickers")

    @stickers.command(name="help")
    async def stickers_help(ctx: commands.Context) -> None:
        await send_help(ctx, "stickers")

    @stickers.command(name="list")
    async def stickers_list(ctx: commands.Context) -> None:
        if ctx.guild is None:
            return
        active = _store(bot).channels(ctx.guild.id)
        lines = ["## 📌 Stickers"]
        for sticker in STICKERS.values():
            where = [
                f"<#{channel_id}>" for channel_id, entry in active.items() if entry["sticker"] == sticker.key
            ]
            place = ", ".join(where) if where else "sin canal"
            lines.append(f"**{sticker.name}** · `{sticker.key}`\n-# {sticker.summary} · {place}")
        lines.append("-# Ponlo con `.noah stickers set <sticker> [#canal]`.")
        await ctx.send("\n".join(lines), allowed_mentions=NO_MENTIONS)

    @stickers.command(name="set")
    async def stickers_set(ctx: commands.Context, sticker: str, channel: StickerChannel | None = None) -> None:
        if ctx.guild is None:
            return
        if not _can_manage(ctx.author):
            await ctx.send("❌ Solo los admins pueden gestionar los stickers.")
            return
        key = _sticker_key(sticker)
        if key not in STICKERS:
            names = ", ".join(f"`{name}`" for name in STICKERS)
            await ctx.send(f"❌ No existe el sticker `{sticker}`. Disponibles: {names}.")
            return
        target = channel or ctx.channel
        if not isinstance(target, StickerChannel):
            await ctx.send("❌ Elige un canal de texto.")
            return

        store = _store(bot)
        previous = store.set(ctx.guild.id, target.id, key)
        error = await _bump(bot, target, force=True)
        if error is not None:
            # Se deshace para no dejar un canal con sticker que no se puede publicar.
            if previous is None:
                store.remove(ctx.guild.id, target.id)
            else:
                store.set(ctx.guild.id, target.id, previous["sticker"])
            await ctx.send(f"❌ {error}")
            return
        replaced = ""
        if previous is not None and previous["sticker"] != key:
            replaced = f" (sustituye a `{previous['sticker']}`)"
        if target.id != ctx.channel.id:
            await ctx.send(f"📌 Sticker **{STICKERS[key].name}** puesto en {target.mention}{replaced}.")

    @stickers.command(name="remove")
    async def stickers_remove(ctx: commands.Context, channel: StickerChannel | None = None) -> None:
        if ctx.guild is None:
            return
        if not _can_manage(ctx.author):
            await ctx.send("❌ Solo los admins pueden gestionar los stickers.")
            return
        target = channel or ctx.channel
        entry = _store(bot).remove(ctx.guild.id, target.id)
        if entry is None:
            await ctx.send(f"❌ {target.mention} no tiene sticker.")
            return
        pending = tasks.pop(target.id, None)
        if pending is not None:
            pending.cancel()
        dirty.discard(target.id)
        if isinstance(target, StickerChannel):
            await _delete_message(target, entry.get("message_id"))
        await ctx.send(f"🗑️ Sticker quitado de {target.mention}.", delete_after=10)
