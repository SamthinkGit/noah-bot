import asyncio
import io
import logging
import re
from collections.abc import Callable
from contextlib import suppress
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import discord
from discord.ext import commands

from noah_bot.modules.billboard import (
    KIND_ANNOUNCEMENT,
    KIND_MOVIE,
    LOCAL_TIMEZONE,
    BillboardStore,
    combine_local,
    item_attendees,
    item_start,
    local_today,
    parse_local_time,
)
from noah_bot.modules.billboard_banner import render_banner
from noah_bot.modules.bot_context import get_bot_context, is_guard_active

_log = logging.getLogger(__name__)

BILLBOARD_COLOR = discord.Color.from_rgb(214, 48, 49)
ANNOUNCEMENT_COLOR = discord.Color.from_rgb(241, 196, 15)

PANEL_PREV_ID = "noah:billboard:prev"
PANEL_TODAY_ID = "noah:billboard:today"
PANEL_NEXT_ID = "noah:billboard:next"
PANEL_EDIT_ID = "noah:billboard:edit"
PANEL_CARD_PREFIX = "noah:billboard:card:"
PANEL_IDS = {PANEL_PREV_ID, PANEL_TODAY_ID, PANEL_NEXT_ID, PANEL_EDIT_ID}
PANEL_BANNER_NAME = "cartelera.png"

# Si alguien navega por las semanas y se va, el panel vuelve solo a «esta semana».
PANEL_RESET_AFTER = 300
PANEL_REFRESH_DELAY = 1.5
STICKY_REPOST_DELAY = 2.0
STICKY_HISTORY_LOOKUP = 10
NOTICE_DELETE_AFTER = 8
PROMPT_TIMEOUT = 60
EDITOR_TIMEOUT = 840
DETAIL_TIMEOUT = 600
LOOP_INTERVAL = 30
REMINDER_BEFORE = timedelta(hours=1)
# Tiempo que una película sigue en la cartelera («en curso») después de empezar.
MOVIE_EXPIRE_AFTER = timedelta(hours=4)
ANNOUNCEMENT_EXPIRE_AFTER = timedelta(hours=6)
MAX_WEEKS_AHEAD = 52
SELECT_LIMIT = 25
# Discord admite 40 componentes por mensaje: con estos topes el panel no pasa de 39.
PANEL_ANNOUNCEMENTS_SHOWN = 2
PANEL_MOVIES_SHOWN = 7
ATTENDEES_SHOWN = 30
MESSAGE_LIMIT = 1900
POSTER_MAX_BYTES = 10 * 1024 * 1024
POSTER_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
POSTER_CONTENT_TYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}

WEEKDAYS = ("LUN", "MAR", "MIÉ", "JUE", "VIE", "SÁB", "DOM")
WEEKDAY_NAMES = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")
DATE_OPTIONS_DAYS = 25
MONTHS = (
    "ene", "feb", "mar", "abr", "may", "jun",
    "jul", "ago", "sep", "oct", "nov", "dic",
)
LANGUAGES = (
    ("VOSE", "Versión original con subtítulos en español"),
    ("Castellano", "Doblada al castellano"),
    ("Latino", "Doblaje latino"),
    ("VOSI", "Versión original con subtítulos en inglés"),
    ("VO", "Versión original sin subtítulos"),
)

# Estado compartido entre el panel persistente, el gestor y los listeners.
_panel_locks: dict[int, asyncio.Lock] = {}
_week_offsets: dict[int, int] = {}
_reset_tasks: dict[int, asyncio.Task] = {}
_refresh_tasks: dict[int, asyncio.Task] = {}
_refresh_dirty: set[int] = set()
_last_rendered: dict[int, Any] = {}
# Mensajes que no deben empujar el panel: comandos borrados y avisos temporales.
_transient_ids: set[int] = set()


# ---------------------------------------------------------------------------
# Utilidades


def _store(bot: discord.Client) -> BillboardStore:
    return get_bot_context(bot).billboard  # type: ignore[arg-type]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _is_admin(member: discord.abc.User) -> bool:
    return isinstance(member, discord.Member) and member.guild_permissions.administrator


def _can_edit(member: discord.abc.User, store: BillboardStore) -> bool:
    if not isinstance(member, discord.Member):
        return False
    if member.guild_permissions.administrator:
        return True
    role_id = store.get_editor_role_id(member.guild.id)
    return role_id is not None and any(role.id == role_id for role in member.roles)


def _ts(value: datetime, style: str) -> str:
    return f"<t:{int(value.timestamp())}:{style}>"


def _local(value: datetime) -> datetime:
    return value.astimezone(LOCAL_TIMEZONE)


def _truncate(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _short_when(start: datetime) -> str:
    local = _local(start)
    return f"{WEEKDAYS[local.weekday()]} {local.day:02d}/{local.month:02d} · {local:%H:%M}"


def _is_movie(item: dict[str, Any]) -> bool:
    return item.get("kind") == KIND_MOVIE


def _expire_delta(item: dict[str, Any]) -> timedelta:
    return MOVIE_EXPIRE_AFTER if _is_movie(item) else ANNOUNCEMENT_EXPIRE_AFTER


def _jump_url(guild_id: int, channel_id: int, message_id: int) -> str:
    return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"


def _mark_transient(message_id: int) -> None:
    _transient_ids.add(message_id)
    asyncio.get_running_loop().call_later(
        PROMPT_TIMEOUT * 2, _transient_ids.discard, message_id
    )


def _panel_channel(guild: discord.Guild, store: BillboardStore) -> discord.TextChannel | None:
    channel_id, _ = store.get_panel(guild.id)
    channel = guild.get_channel(channel_id) if channel_id is not None else None
    return channel if isinstance(channel, discord.TextChannel) else None


def _channel_problem(channel: discord.TextChannel) -> str | None:
    permissions = channel.permissions_for(channel.guild.me)
    if not (permissions.send_messages and permissions.embed_links):
        return f"No puedo enviar mensajes con embeds en {channel.mention}."
    if not permissions.read_message_history:
        return f"No puedo leer el historial de {channel.mention} (hace falta para dejar el panel abajo)."
    return None


async def _respond_edit(interaction: discord.Interaction, **kwargs: Any) -> None:
    """Edita el mensaje del componente, tanto si ya se ha respondido como si no."""
    if interaction.response.is_done():
        await interaction.edit_original_response(**kwargs)
    else:
        await interaction.response.edit_message(**kwargs)


# ---------------------------------------------------------------------------
# Panel (marquesina con la agenda de los próximos 7 días)


def _week_start(offset: int, now: datetime) -> date:
    """Primer día del bloque: hoy, y cada página son los 7 días siguientes."""
    return _local(now).date() + timedelta(weeks=offset)


def _week_range(start: date) -> str:
    end = start + timedelta(days=6)
    if start.month == end.month:
        return f"{start.day} – {end.day} {MONTHS[end.month - 1]}"
    return f"{start.day} {MONTHS[start.month - 1]} – {end.day} {MONTHS[end.month - 1]}"


def _week_subtitle(offset: int, start: date) -> str:
    return f"Próximos 7 días · {_week_range(start)}" if offset == 0 else _week_range(start)


def _week_movies(
    store: BillboardStore, guild_id: int, start_day: date
) -> list[dict[str, Any]]:
    end_day = start_day + timedelta(days=7)
    movies: list[dict[str, Any]] = []
    for item in store.list_items(guild_id, KIND_MOVIE):
        start = item_start(item)
        if start is not None and start_day <= _local(start).date() < end_day:
            movies.append(item)
    return movies


def _day_label(day: date, today: date) -> str:
    name = f"{WEEKDAY_NAMES[day.weekday()]} {day.day}".upper()
    if day == today:
        return f"HOY · {name}"
    if day == today + timedelta(days=1):
        return f"MAÑANA · {name}"
    return name


def _movie_text(item: dict[str, Any], now: datetime, today: date) -> str:
    start = item_start(item)
    lines: list[str] = []
    if start is not None:
        lines.append(f"-# **{_day_label(_local(start).date(), today)}**")
    lines.append(f"**{_truncate(item.get('title') or 'Sin título', 80)}**")

    meta: list[str] = []
    if start is not None:
        meta.append(_ts(start, "t"))
    if item.get("language"):
        meta.append(str(item["language"]))
    if start is not None and start <= now:
        meta.append("🔴 en curso")
    attendees = len(item_attendees(item))
    meta.append(f"👥 {attendees} apuntado{'' if attendees == 1 else 's'}")
    lines.append(" · ".join(meta))
    return "\n".join(lines)


def _announcement_text(item: dict[str, Any]) -> str:
    start = item_start(item)
    text = f"📢 **{_truncate(item.get('title') or 'Anuncio', 80)}**"
    if start is not None:
        text += f" · {_ts(start, 'f')}"
    description = (item.get("description") or "").strip()
    if description:
        text += f"\n-# {_truncate(description, 120)}"
    return text


class CardButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"noah:billboard:card:(?P<item_id>[0-9a-f]+)",
):
    """Botón «Ficha» de cada sesión. Persistente aunque el bot se reinicie."""

    def __init__(self, item_id: str, *, label: str = "Ficha", emoji: str = "🎟️") -> None:
        super().__init__(
            discord.ui.Button(
                label=label,
                emoji=emoji,
                style=discord.ButtonStyle.secondary,
                custom_id=f"{PANEL_CARD_PREFIX}{item_id}",
            )
        )
        self.item_id = item_id

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
        /,
    ) -> "CardButton":
        _ = interaction, item
        return cls(match["item_id"])

    async def callback(self, interaction: discord.Interaction) -> None:
        await _open_detail(interaction, self.item_id)


def _build_panel_container(
    store: BillboardStore, guild_id: int, offset: int, now: datetime
) -> discord.ui.Container:
    today = _local(now).date()
    start_day = _week_start(offset, now)
    container = discord.ui.Container(accent_color=BILLBOARD_COLOR)
    container.add_item(
        discord.ui.MediaGallery(
            discord.MediaGalleryItem(
                f"attachment://{PANEL_BANNER_NAME}",
                description=f"Cartelera · {_week_subtitle(offset, start_day)}",
            )
        )
    )

    announcements = store.list_items(guild_id, KIND_ANNOUNCEMENT)
    for item in announcements[:PANEL_ANNOUNCEMENTS_SHOWN]:
        container.add_item(
            discord.ui.Section(
                discord.ui.TextDisplay(_announcement_text(item)),
                accessory=CardButton(item["id"], label="Ver", emoji="📢"),
            )
        )
    hidden = len(announcements) - PANEL_ANNOUNCEMENTS_SHOWN
    if hidden > 0:
        container.add_item(
            discord.ui.TextDisplay(f"-# …y {hidden} anuncio{'' if hidden == 1 else 's'} más")
        )
    container.add_item(discord.ui.Separator())

    movies = _week_movies(store, guild_id, start_day)
    for item in movies[:PANEL_MOVIES_SHOWN]:
        container.add_item(
            discord.ui.Section(
                discord.ui.TextDisplay(_movie_text(item, now, today)),
                accessory=CardButton(item["id"]),
            )
        )
    hidden = len(movies) - PANEL_MOVIES_SHOWN
    if hidden > 0:
        container.add_item(
            discord.ui.TextDisplay(f"-# …y {hidden} {'sesión' if hidden == 1 else 'sesiones'} más")
        )
    if not movies:
        container.add_item(discord.ui.TextDisplay("🍿 No hay sesiones estos días."))
    container.add_item(discord.ui.Separator())

    footer: list[str] = []
    if movies:
        busy = {_local(start).date() for item in movies if (start := item_start(item))}
        empty = [
            f"{WEEKDAYS[day.weekday()].lower()} {day.day}"
            for day in (start_day + timedelta(days=index) for index in range(7))
            if day not in busy
        ]
        if empty:
            footer.append(f"-# Sin sesiones: {' · '.join(empty)}")
    upcoming = [
        item
        for item in store.list_items(guild_id, KIND_MOVIE)
        if (start := item_start(item)) is not None and start > now
    ]
    if upcoming:
        start = item_start(upcoming[0])
        assert start is not None
        footer.append(
            f"⏭️ Próxima: **{_truncate(upcoming[0].get('title') or 'Sin título', 60)}** "
            f"· {_ts(start, 'R')}"
        )
    else:
        footer.append("-# Un editor puede añadir pelis con ✏️ Editar.")
    container.add_item(discord.ui.TextDisplay("\n".join(footer)))
    return container


class PanelNavRow(discord.ui.ActionRow):
    def __init__(self, offset: int = 0) -> None:
        super().__init__()
        self.prev_days.disabled = offset <= 0
        self.today.disabled = offset == 0
        self.next_days.disabled = offset >= MAX_WEEKS_AHEAD

    @discord.ui.button(
        label="Anterior", emoji="◀️", style=discord.ButtonStyle.secondary, custom_id=PANEL_PREV_ID
    )
    async def prev_days(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        _ = button
        await _navigate(interaction, lambda offset: offset - 1)

    @discord.ui.button(
        label="Hoy", emoji="📅", style=discord.ButtonStyle.secondary, custom_id=PANEL_TODAY_ID
    )
    async def today(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        _ = button
        await _navigate(interaction, lambda offset: 0)

    @discord.ui.button(
        label="Siguiente", emoji="▶️", style=discord.ButtonStyle.secondary, custom_id=PANEL_NEXT_ID
    )
    async def next_days(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        _ = button
        await _navigate(interaction, lambda offset: offset + 1)

    @discord.ui.button(
        label="Editar", emoji="✏️", style=discord.ButtonStyle.secondary, custom_id=PANEL_EDIT_ID
    )
    async def edit(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        _ = button
        await _open_editor(interaction)


class BillboardPanelView(discord.ui.LayoutView):
    """Panel fijo de la cartelera. Sin datos solo lleva los botones: así se registra
    como vista persistente para que sigan funcionando tras reiniciar el bot."""

    def __init__(
        self,
        store: BillboardStore | None = None,
        guild_id: int | None = None,
        offset: int = 0,
        now: datetime | None = None,
    ) -> None:
        super().__init__(timeout=None)
        if store is not None and guild_id is not None:
            self.add_item(_build_panel_container(store, guild_id, offset, now or _now()))
        self.add_item(PanelNavRow(offset))


def _component_key(item: discord.ui.Item[Any]) -> tuple[Any, ...]:
    inner = getattr(item, "item", item)
    return (
        type(inner).__name__,
        getattr(inner, "content", None),
        getattr(inner, "custom_id", None),
        getattr(inner, "label", None),
        getattr(inner, "disabled", None),
    )


def _render_panel(
    store: BillboardStore, guild_id: int
) -> tuple[BillboardPanelView, discord.File, Any]:
    now = _now()
    offset = _week_offsets.get(guild_id, 0)
    subtitle = _week_subtitle(offset, _week_start(offset, now))
    view = BillboardPanelView(store, guild_id, offset, now)
    banner = discord.File(io.BytesIO(render_banner(subtitle)), filename=PANEL_BANNER_NAME)
    signature = (subtitle, tuple(_component_key(item) for item in view.walk_children()))
    return view, banner, signature


def _cancel_reset(guild_id: int) -> None:
    task = _reset_tasks.pop(guild_id, None)
    if task is not None and not task.done() and task is not asyncio.current_task():
        task.cancel()


def _schedule_reset(bot: discord.Client, guild: discord.Guild) -> None:
    """Reinicia la cuenta atrás para volver a los próximos 7 días tras un rato sin uso."""
    _cancel_reset(guild.id)

    async def _reset() -> None:
        try:
            await asyncio.sleep(PANEL_RESET_AFTER)
            if _week_offsets.get(guild.id, 0) == 0:
                return
            _week_offsets[guild.id] = 0
            await _refresh_panel(bot, guild)
        finally:
            if _reset_tasks.get(guild.id) is asyncio.current_task():
                _reset_tasks.pop(guild.id, None)

    _reset_tasks[guild.id] = asyncio.create_task(_reset())


async def _navigate(
    interaction: discord.Interaction, target: Callable[[int], int]
) -> None:
    guild = interaction.guild
    if guild is None:
        return

    store = _store(interaction.client)
    offset = max(0, min(MAX_WEEKS_AHEAD, target(_week_offsets.get(guild.id, 0))))
    _week_offsets[guild.id] = offset
    if offset:
        _schedule_reset(interaction.client, guild)
    else:
        _cancel_reset(guild.id)

    view, banner, signature = _render_panel(store, guild.id)
    _last_rendered[guild.id] = signature
    await interaction.response.edit_message(view=view, attachments=[banner])


async def _open_detail(interaction: discord.Interaction, item_id: str) -> None:
    guild = interaction.guild
    if guild is None:
        return

    store = _store(interaction.client)
    if _week_offsets.get(guild.id, 0):
        _schedule_reset(interaction.client, guild)

    item = store.get_item(guild.id, item_id)
    if item is None:
        await interaction.response.send_message(
            "❌ Eso ya no está en la cartelera.", ephemeral=True
        )
        return

    card, poster = build_item_card(store, item)
    kwargs: dict[str, Any] = {"embed": card, "ephemeral": True}
    if poster is not None:
        kwargs["file"] = poster
    if _is_movie(item):
        kwargs["view"] = DetailView(guild.id, item, interaction.user.id)
    await interaction.response.send_message(**kwargs)


async def _publish_panel_unlocked(
    guild: discord.Guild, store: BillboardStore, channel: discord.TextChannel
) -> str | None:
    problem = _channel_problem(channel)
    if problem is not None:
        return problem

    view, banner, signature = _render_panel(store, guild.id)
    try:
        message = await channel.send(view=view, file=banner)
    except discord.HTTPException:
        _log.exception("No se ha podido publicar el panel de la cartelera")
        return f"No he podido publicar el panel en {channel.mention}."

    old_channel_id, old_message_id = store.get_panel(guild.id)
    store.set_panel(guild.id, channel.id, message.id)
    _last_rendered[guild.id] = signature

    old_channel = guild.get_channel(old_channel_id) if old_channel_id is not None else None
    if isinstance(old_channel, discord.TextChannel) and old_message_id is not None:
        with suppress(discord.HTTPException):
            await old_channel.get_partial_message(old_message_id).delete()
    return None


async def _publish_panel(
    guild: discord.Guild, store: BillboardStore, channel: discord.TextChannel
) -> str | None:
    """Publica el panel en `channel` y borra el anterior. Devuelve el error, si hay."""
    async with _panel_locks.setdefault(guild.id, asyncio.Lock()):
        return await _publish_panel_unlocked(guild, store, channel)


async def _refresh_panel(
    bot: discord.Client, guild: discord.Guild, *, force: bool = False
) -> str | None:
    """Actualiza el panel en su sitio (o lo vuelve a publicar si ha desaparecido)."""
    store = _store(bot)
    async with _panel_locks.setdefault(guild.id, asyncio.Lock()):
        channel = _panel_channel(guild, store)
        if channel is None:
            return "El canal de la cartelera no está configurado."

        _, message_id = store.get_panel(guild.id)
        if message_id is None:
            return await _publish_panel_unlocked(guild, store, channel)

        view, banner, signature = _render_panel(store, guild.id)
        if not force and _last_rendered.get(guild.id) == signature:
            return None

        try:
            await channel.get_partial_message(message_id).edit(view=view, attachments=[banner])
        except discord.HTTPException:
            # Borrado, o un panel antiguo que no se puede convertir al formato nuevo:
            # se publica uno nuevo en su lugar.
            return await _publish_panel_unlocked(guild, store, channel)

        _last_rendered[guild.id] = signature
        return None


def _request_panel_refresh(bot: discord.Client, guild: discord.Guild) -> None:
    """Agrupa varios cambios seguidos (p. ej. gente apuntándose) en una sola edición."""
    _refresh_dirty.add(guild.id)
    pending = _refresh_tasks.get(guild.id)
    if pending is not None and not pending.done():
        return

    async def _worker() -> None:
        try:
            while guild.id in _refresh_dirty:
                await asyncio.sleep(PANEL_REFRESH_DELAY)
                _refresh_dirty.discard(guild.id)
                await _refresh_panel(bot, guild)
        finally:
            if _refresh_tasks.get(guild.id) is asyncio.current_task():
                _refresh_tasks.pop(guild.id, None)

    _refresh_tasks[guild.id] = asyncio.create_task(_worker())


async def _remove_panel(guild: discord.Guild, store: BillboardStore) -> None:
    async with _panel_locks.setdefault(guild.id, asyncio.Lock()):
        channel = _panel_channel(guild, store)
        _, message_id = store.get_panel(guild.id)
        store.set_panel(guild.id, None, None)
        _last_rendered.pop(guild.id, None)
        if channel is not None and message_id is not None:
            with suppress(discord.HTTPException):
                await channel.get_partial_message(message_id).delete()


def _is_panel_message(bot: commands.Bot, message: discord.Message) -> bool:
    if bot.user is None or message.author.id != bot.user.id:
        return False
    return any(
        getattr(child, "custom_id", None) in PANEL_IDS
        for row in message.components
        for child in getattr(row, "children", [])
    )


async def _panel_is_last(channel: discord.TextChannel, panel_message_id: int | None) -> bool:
    if panel_message_id is None:
        return False

    try:
        async for message in channel.history(limit=STICKY_HISTORY_LOOKUP):
            if message.id in _transient_ids:
                continue
            return message.id == panel_message_id
    except discord.HTTPException:
        return False
    return False


async def _repost_sticky_panel(bot: discord.Client, guild: discord.Guild) -> None:
    """Baja el panel al final de su canal si hay algún mensaje por debajo."""
    store = _store(bot)
    async with _panel_locks.setdefault(guild.id, asyncio.Lock()):
        channel = _panel_channel(guild, store)
        if channel is None:
            return
        _, message_id = store.get_panel(guild.id)
        if await _panel_is_last(channel, message_id):
            return
        await _publish_panel_unlocked(guild, store, channel)


# ---------------------------------------------------------------------------
# Ficha de una película o anuncio


def build_item_card(
    store: BillboardStore, item: dict[str, Any], *, with_poster: bool = True
) -> tuple[discord.Embed, discord.File | None]:
    is_movie = _is_movie(item)
    title = item.get("title") or ("Sin título" if is_movie else "Anuncio")
    description = (item.get("description") or "").strip()
    embed = discord.Embed(
        title=_truncate(f"{'🎬' if is_movie else '📢'} {title}", 256),
        description=description[:4096] or None,
        color=BILLBOARD_COLOR if is_movie else ANNOUNCEMENT_COLOR,
    )

    start = item_start(item)
    if start is not None:
        embed.add_field(
            name="📅 Cuándo", value=f"{_ts(start, 'F')}\n{_ts(start, 'R')}", inline=True
        )

    if is_movie:
        language = item.get("language")
        if language:
            explanation = dict(LANGUAGES).get(str(language))
            embed.add_field(
                name="🗣️ Idioma",
                value=f"**{language}**" + (f"\n{explanation}" if explanation else ""),
                inline=True,
            )

        attendees = item_attendees(item)
        if attendees:
            shown = " ".join(f"<@{user_id}>" for user_id in attendees[:ATTENDEES_SHOWN])
            hidden = len(attendees) - ATTENDEES_SHOWN
            value = shown + (f" y {hidden} más" if hidden > 0 else "")
        else:
            value = "Nadie todavía. ¡Sé el primero!"
        embed.add_field(name=f"👥 Apuntados ({len(attendees)})", value=value, inline=False)
        embed.set_footer(text="Si te apuntas, te mencionaré 1 h antes de que empiece.")

    poster: discord.File | None = None
    path = store.poster_path(item)
    if path is not None:
        filename = f"poster{path.suffix}"
        embed.set_image(url=f"attachment://{filename}")
        if with_poster:
            poster = discord.File(path, filename=filename)
    return embed, poster


class DetailView(discord.ui.View):
    """Botón de apuntarse/desapuntarse en la ficha privada de una película."""

    def __init__(self, guild_id: int, item: dict[str, Any], user_id: int) -> None:
        super().__init__(timeout=DETAIL_TIMEOUT)
        self.guild_id = guild_id
        self.item_id = str(item["id"])

        joined = user_id in item_attendees(item)
        start = item_start(item)
        started = start is not None and start <= _now()
        if started:
            button = discord.ui.Button(
                label="Ya ha empezado", emoji="🔴", style=discord.ButtonStyle.secondary,
                disabled=True,
            )
        elif joined:
            button = discord.ui.Button(
                label="Desapuntarme", emoji="❌", style=discord.ButtonStyle.secondary
            )
        else:
            button = discord.ui.Button(
                label="Apuntarme", emoji="🍿", style=discord.ButtonStyle.success
            )
        button.callback = self._toggle
        self.add_item(button)

    async def _toggle(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        store = _store(interaction.client)
        item = store.get_item(self.guild_id, self.item_id)
        if guild is None or item is None:
            await interaction.response.edit_message(
                content="❌ Esta sesión ya no está en la cartelera.",
                embed=None,
                view=None,
                attachments=[],
            )
            return

        start = item_start(item)
        if start is not None and start <= _now():
            card, _ = build_item_card(store, item, with_poster=False)
            await interaction.response.edit_message(
                content="🔴 Esta sesión ya ha empezado.",
                embed=card,
                view=DetailView(guild.id, item, interaction.user.id),
            )
            return

        joined = store.toggle_attendee(guild.id, self.item_id, interaction.user.id)
        item = store.get_item(guild.id, self.item_id) or item
        card, _ = build_item_card(store, item, with_poster=False)
        content = (
            "✅ ¡Apuntado! Te mencionaré 1 h antes de que empiece."
            if joined
            else "Te has desapuntado."
        )
        await interaction.response.edit_message(
            content=content,
            embed=card,
            view=DetailView(guild.id, item, interaction.user.id),
        )
        _request_panel_refresh(interaction.client, guild)


# ---------------------------------------------------------------------------
# Gestor (solo rol editor / admins)


def _editor_line(item: dict[str, Any]) -> str:
    start = item_start(item)
    title = _truncate(item.get("title") or "Sin título", 60)
    when = f"`{_short_when(start)}` " if start is not None else ""
    if _is_movie(item):
        return f"🎬 {when}**{title}** · 👥 {len(item_attendees(item))}"
    return f"📢 {when}**{title}**"


def _editor_embed(store: BillboardStore, guild: discord.Guild) -> discord.Embed:
    items = store.list_items(guild.id)
    lines = [_editor_line(item) for item in items[:SELECT_LIMIT]]
    if len(items) > SELECT_LIMIT:
        lines.append(f"…y {len(items) - SELECT_LIMIT} más")
    embed = discord.Embed(
        title="🛠️ Gestor de la cartelera",
        description="\n".join(lines) or "_La cartelera está vacía._",
        color=BILLBOARD_COLOR,
    )
    embed.set_footer(
        text="Añade una película o un anuncio, o elige uno en el menú para editarlo o quitarlo."
    )
    return embed


async def _show_editor(
    interaction: discord.Interaction,
    guild: discord.Guild,
    author_id: int,
    content: str | None = None,
) -> None:
    store = _store(interaction.client)
    await _respond_edit(
        interaction,
        content=content,
        embed=_editor_embed(store, guild),
        view=EditorView(store, guild, author_id),
        attachments=[],
    )


async def _show_item(
    interaction: discord.Interaction,
    guild: discord.Guild,
    author_id: int,
    item_id: str,
    content: str | None = None,
    confirm_delete: bool = False,
) -> None:
    store = _store(interaction.client)
    item = store.get_item(guild.id, item_id)
    if item is None:
        await _show_editor(interaction, guild, author_id, "❌ Eso ya no está en la cartelera.")
        return

    card, poster = build_item_card(store, item)
    await _respond_edit(
        interaction,
        content=content,
        embed=card,
        view=ItemManageView(guild, author_id, item, confirm_delete),
        attachments=[poster] if poster is not None else [],
    )


async def _delete_reminder(guild: discord.Guild, item: dict[str, Any] | None) -> None:
    reminder = item.get("reminder_message") if item else None
    if not isinstance(reminder, list) or len(reminder) != 2:
        return
    try:
        channel_id, message_id = int(reminder[0]), int(reminder[1])
    except (TypeError, ValueError):
        return
    channel = guild.get_channel(channel_id)
    if isinstance(channel, discord.TextChannel):
        with suppress(discord.HTTPException):
            await channel.get_partial_message(message_id).delete()


class _EditorBaseView(discord.ui.View):
    def __init__(self, guild: discord.Guild, author_id: int) -> None:
        super().__init__(timeout=EDITOR_TIMEOUT)
        self.guild = guild
        self.author_id = author_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not _can_edit(interaction.user, _store(interaction.client)):
            await interaction.response.send_message(
                "❌ Ya no tienes permiso para editar la cartelera.", ephemeral=True
            )
            return False
        return True


class EditorView(_EditorBaseView):
    def __init__(self, store: BillboardStore, guild: discord.Guild, author_id: int) -> None:
        super().__init__(guild, author_id)
        items = store.list_items(guild.id)[:SELECT_LIMIT]
        if items:
            options = []
            for item in items:
                start = item_start(item)
                description = (
                    _short_when(start) if start is not None else "Sin fecha"
                ) + (" · Película" if _is_movie(item) else " · Anuncio")
                options.append(
                    discord.SelectOption(
                        label=_truncate(item.get("title") or "Sin título", 100),
                        description=_truncate(description, 100),
                        value=item["id"],
                        emoji="🎬" if _is_movie(item) else "📢",
                    )
                )
            select = discord.ui.Select(
                placeholder="✏️ Editar o quitar…", options=options, row=1
            )

            async def _on_select(interaction: discord.Interaction) -> None:
                await _show_item(interaction, self.guild, self.author_id, select.values[0])

            select.callback = _on_select
            self.add_item(select)

    @discord.ui.button(
        label="Nueva película", emoji="🎬", style=discord.ButtonStyle.secondary, row=0
    )
    async def new_movie(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        _ = button
        await interaction.response.send_modal(
            ItemModal(self.guild, self.author_id, KIND_MOVIE)
        )

    @discord.ui.button(
        label="Nuevo anuncio", emoji="📢", style=discord.ButtonStyle.secondary, row=0
    )
    async def new_announcement(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        _ = button
        await interaction.response.send_modal(
            ItemModal(self.guild, self.author_id, KIND_ANNOUNCEMENT)
        )

    @discord.ui.button(label="Cerrar", style=discord.ButtonStyle.secondary, row=0)
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        _ = button
        self.stop()
        await interaction.response.defer()
        with suppress(discord.HTTPException):
            await interaction.delete_original_response()


class ItemManageView(_EditorBaseView):
    def __init__(
        self,
        guild: discord.Guild,
        author_id: int,
        item: dict[str, Any],
        confirm_delete: bool = False,
    ) -> None:
        super().__init__(guild, author_id)
        self.item_id = str(item["id"])
        self.kind = str(item.get("kind") or KIND_MOVIE)

        if confirm_delete:
            self._add_button("Sí, eliminar", "🗑️", discord.ButtonStyle.danger, self._delete)
            self._add_button("Cancelar", None, discord.ButtonStyle.secondary, self._cancel)
            return

        noun = "póster" if _is_movie(item) else "imagen"
        self._add_button("Editar", "✏️", discord.ButtonStyle.secondary, self._edit)
        self._add_button(
            f"Cambiar {noun}" if item.get("poster") else f"Subir {noun}",
            "🖼️",
            discord.ButtonStyle.secondary,
            self._upload_poster,
        )
        if item.get("poster"):
            self._add_button(
                f"Quitar {noun}", None, discord.ButtonStyle.secondary, self._remove_poster
            )
        self._add_button("Eliminar", "🗑️", discord.ButtonStyle.danger, self._ask_delete)
        self._add_button("Volver", "◀️", discord.ButtonStyle.secondary, self._back)

    def _add_button(
        self,
        label: str,
        emoji: str | None,
        style: discord.ButtonStyle,
        callback: Callable[[discord.Interaction], Any],
    ) -> None:
        button = discord.ui.Button(label=label, emoji=emoji, style=style)
        button.callback = callback
        self.add_item(button)

    async def _edit(self, interaction: discord.Interaction) -> None:
        store = _store(interaction.client)
        item = store.get_item(self.guild.id, self.item_id)
        if item is None:
            await _show_editor(
                interaction, self.guild, self.author_id, "❌ Eso ya no está en la cartelera."
            )
            return

        start = item_start(item)
        values = {
            "title": item.get("title") or "",
            "date": _local(start).date().isoformat() if start is not None else "",
            "time": f"{_local(start):%H:%M}" if start is not None else "",
            "language": item.get("language") or "",
            "description": item.get("description") or "",
        }
        await interaction.response.send_modal(
            ItemModal(self.guild, self.author_id, self.kind, self.item_id, values)
        )

    async def _upload_poster(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(
            PosterModal(self.guild, self.author_id, self.kind, self.item_id)
        )

    async def _remove_poster(self, interaction: discord.Interaction) -> None:
        _store(interaction.client).set_poster(self.guild.id, self.item_id, None)
        await _show_item(
            interaction, self.guild, self.author_id, self.item_id, "🖼️ Imagen quitada."
        )

    async def _ask_delete(self, interaction: discord.Interaction) -> None:
        item = _store(interaction.client).get_item(self.guild.id, self.item_id)
        attendees = len(item_attendees(item)) if item is not None else 0
        warning = "⚠️ ¿Seguro que quieres eliminarlo de la cartelera?"
        if attendees:
            warning += f" Hay **{attendees}** apuntados."
        await _show_item(
            interaction, self.guild, self.author_id, self.item_id, warning, confirm_delete=True
        )

    async def _cancel(self, interaction: discord.Interaction) -> None:
        await _show_item(interaction, self.guild, self.author_id, self.item_id)

    async def _delete(self, interaction: discord.Interaction) -> None:
        store = _store(interaction.client)
        removed = store.remove_item(self.guild.id, self.item_id)
        await _show_editor(
            interaction,
            self.guild,
            self.author_id,
            f"🗑️ Eliminado **{_truncate(removed.get('title') or 'Sin título', 80)}**."
            if removed is not None
            else "❌ Eso ya no estaba en la cartelera.",
        )
        if removed is not None:
            await _delete_reminder(self.guild, removed)
            _request_panel_refresh(interaction.client, self.guild)

    async def _back(self, interaction: discord.Interaction) -> None:
        await _show_editor(interaction, self.guild, self.author_id)


def _language_options(current: str) -> list[discord.SelectOption]:
    options = [
        discord.SelectOption(
            label=label, description=description, value=label, default=label == current
        )
        for label, description in LANGUAGES
    ]
    if current and current not in dict(LANGUAGES):
        options.append(discord.SelectOption(label=current[:100], value=current[:100], default=True))
    return options


async def _read_poster(attachment: discord.Attachment) -> tuple[bytes, str]:
    content_type = (attachment.content_type or "").split(";")[0].strip().lower()
    extension = Path(attachment.filename).suffix.lower()
    if not content_type.startswith("image/") and extension not in POSTER_EXTENSIONS:
        raise ValueError("La imagen tiene que ser un archivo de imagen (png, jpg, gif o webp).")
    if attachment.size > POSTER_MAX_BYTES:
        raise ValueError("La imagen pesa demasiado (máximo 10 MB).")
    try:
        data = await attachment.read()
    except discord.HTTPException as error:
        raise ValueError("No he podido descargar la imagen. Prueba a subirla otra vez.") from error

    if extension not in POSTER_EXTENSIONS:
        extension = POSTER_CONTENT_TYPES.get(content_type, ".png")
    return data, extension


def _date_label(day: date, today: date) -> str:
    text = f"{WEEKDAY_NAMES[day.weekday()]} {day.day} {MONTHS[day.month - 1]}"
    if day == today:
        return f"Hoy · {text}"
    if day == today + timedelta(days=1):
        return f"Mañana · {text}"
    return text[0].upper() + text[1:]


def _date_options(current: str) -> list[discord.SelectOption]:
    """Los próximos días para elegir en el formulario (Discord no tiene calendario)."""
    today = local_today()
    days = [today + timedelta(days=offset) for offset in range(DATE_OPTIONS_DAYS)]
    try:
        current_day = date.fromisoformat(current) if current else None
    except ValueError:
        current_day = None
    if current_day is not None and current_day not in days:
        # Al editar una sesión lejana (o en curso) su día tiene que seguir saliendo.
        days = sorted([*days[: DATE_OPTIONS_DAYS - 1], current_day])

    return [
        discord.SelectOption(
            label=_date_label(day, today),
            value=day.isoformat(),
            default=day == current_day,
        )
        for day in days
    ]


class ItemModal(discord.ui.Modal):
    """Formulario para crear o editar una película o un anuncio.

    Discord admite 5 campos por formulario: el póster de las películas se sube
    aparte desde su ficha; los anuncios sí llevan la imagen aquí.
    """

    def __init__(
        self,
        guild: discord.Guild,
        author_id: int,
        kind: str,
        item_id: str | None = None,
        values: dict[str, str] | None = None,
        pending_poster: tuple[bytes, str] | None = None,
    ) -> None:
        is_movie = kind == KIND_MOVIE
        editing = item_id is not None
        if is_movie:
            title = "Editar película" if editing else "Nueva película"
        else:
            title = "Editar anuncio" if editing else "Nuevo anuncio"
        super().__init__(title=title, timeout=900)

        self.guild = guild
        self.author_id = author_id
        self.kind = kind
        self.item_id = item_id
        self.pending_poster = pending_poster
        values = values or {}

        self.title_input = discord.ui.TextInput(
            max_length=100,
            default=values.get("title") or None,
            placeholder="Dune: Parte 2" if is_movie else "Maratón de Halloween",
        )
        self.date_select = discord.ui.Select(
            placeholder="Elige el día",
            options=_date_options(values.get("date") or ""),
            min_values=1 if is_movie else 0,
            max_values=1,
            required=is_movie,
        )
        self.time_input = discord.ui.TextInput(
            max_length=8,
            required=is_movie,
            default=values.get("time") or None,
            placeholder="22:00",
        )
        self.description_input = discord.ui.TextInput(
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=1000 if is_movie else 1500,
            default=values.get("description") or None,
            placeholder="De qué va, quién la propone…" if is_movie else "Lo que quieras contar.",
        )
        self.language_select: discord.ui.Select | None = None
        self.poster_input: discord.ui.FileUpload | None = None

        self.add_item(
            discord.ui.Label(
                text="Nombre de la película" if is_movie else "Título",
                component=self.title_input,
            )
        )
        if is_movie:
            self.add_item(discord.ui.Label(text="Día", component=self.date_select))
            self.add_item(
                discord.ui.Label(
                    text="Hora",
                    description="Hora de España. Ej: 22:00 o 21:30.",
                    component=self.time_input,
                )
            )
            self.language_select = discord.ui.Select(
                placeholder="Elige idioma / subtítulos",
                options=_language_options(values.get("language") or ""),
                min_values=0,
                max_values=1,
                required=False,
            )
            self.add_item(
                discord.ui.Label(text="Idioma y subtítulos", component=self.language_select)
            )
            self.add_item(discord.ui.Label(text="Descripción", component=self.description_input))
            return

        self.add_item(discord.ui.Label(text="Texto del anuncio", component=self.description_input))
        self.add_item(
            discord.ui.Label(
                text="Día (opcional)",
                description="Si lo pones, sale en el anuncio y se quita solo unas horas después.",
                component=self.date_select,
            )
        )
        self.add_item(
            discord.ui.Label(
                text="Hora (opcional)",
                description="Solo si has elegido día. Ej: 22:00.",
                component=self.time_input,
            )
        )
        self.poster_input = discord.ui.FileUpload(required=False, min_values=0, max_values=1)
        if pending_poster is not None:
            poster_help = "Ya tengo la imagen que subiste: súbela solo si quieres cambiarla."
        elif editing:
            poster_help = "Déjalo vacío para mantener la actual."
        else:
            poster_help = "Opcional. Solo se ve al abrir la ficha."
        self.add_item(
            discord.ui.Label(text="Imagen", description=poster_help, component=self.poster_input)
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        store = _store(interaction.client)
        if not _can_edit(interaction.user, store):
            await interaction.response.send_message(
                "❌ No tienes permiso para editar la cartelera.", ephemeral=True
            )
            return

        if interaction.message is not None:
            await interaction.response.defer()
        else:
            await interaction.response.defer(ephemeral=True, thinking=True)

        is_movie = self.kind == KIND_MOVIE
        values = {
            "title": self.title_input.value.strip(),
            "date": self.date_select.values[0] if self.date_select.values else "",
            "time": self.time_input.value.strip(),
            "language": (
                self.language_select.values[0]
                if self.language_select is not None and self.language_select.values
                else ""
            ),
            "description": self.description_input.value.strip(),
        }

        errors: list[str] = []
        poster = self.pending_poster
        if self.poster_input is not None and self.poster_input.values:
            try:
                poster = await _read_poster(self.poster_input.values[0])
            except ValueError as error:
                errors.append(str(error))

        if not values["title"]:
            errors.append("El nombre no puede estar vacío.")

        existing = (
            store.get_item(self.guild.id, self.item_id) if self.item_id is not None else None
        )
        if self.item_id is not None and existing is None:
            await interaction.followup.send(
                "❌ Eso ya no está en la cartelera.", ephemeral=True
            )
            return

        starts_at: datetime | None = None
        if values["date"] or values["time"] or is_movie:
            if not values["date"]:
                errors.append("Elige el día.")
            if not values["time"]:
                errors.append("Falta la hora.")
            if values["date"] and values["time"]:
                try:
                    hour, minute = parse_local_time(values["time"])
                    starts_at = combine_local(date.fromisoformat(values["date"]), hour, minute)
                except ValueError as error:
                    errors.append(str(error))
                else:
                    previous = item_start(existing) if existing is not None else None
                    if starts_at <= _now() and starts_at != previous:
                        errors.append("Esa hora ya ha pasado.")

        if errors:
            await interaction.followup.send(
                "\n".join(f"❌ {error}" for error in errors)
                + "\nPulsa **Corregir** para volver al formulario con lo que escribiste.",
                view=RetryView(
                    self.guild, self.author_id, self.kind, self.item_id, values, poster
                ),
                ephemeral=True,
            )
            return

        if existing is None:
            item_id = store.add_item(
                self.guild.id,
                kind=self.kind,
                title=values["title"],
                starts_at=starts_at,
                description=values["description"],
                language=values["language"],
                created_by=interaction.user.id,
            )
            content = (
                "✅ Película añadida a la cartelera. Pulsa 🖼️ **Subir póster** si quieres ponerle uno."
                if is_movie
                else "✅ Anuncio publicado."
            )
        else:
            item_id = str(existing["id"])
            if starts_at != item_start(existing):
                # El recordatorio de la hora antigua ya no vale.
                await _delete_reminder(self.guild, existing)
            store.update_item(
                self.guild.id,
                item_id,
                title=values["title"],
                starts_at=starts_at,
                description=values["description"],
                language=values["language"],
            )
            content = "✅ Cambios guardados."

        # Sin imagen nueva se mantiene la que hubiera.
        if poster is not None:
            store.set_poster(self.guild.id, item_id, poster[0], poster[1])

        _request_panel_refresh(interaction.client, self.guild)
        await _show_item(interaction, self.guild, self.author_id, item_id, content)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await _modal_error(interaction, error)


async def _modal_error(interaction: discord.Interaction, error: Exception) -> None:
    _log.exception("Error en un formulario de la cartelera", exc_info=error)
    message = "❌ No he podido guardar los cambios. Inténtalo de nuevo más tarde."
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


class PosterModal(discord.ui.Modal):
    """Subir o cambiar el póster (o la imagen de un anuncio) desde su ficha."""

    def __init__(self, guild: discord.Guild, author_id: int, kind: str, item_id: str) -> None:
        is_movie = kind == KIND_MOVIE
        super().__init__(title="Póster de la película" if is_movie else "Imagen del anuncio")
        self.guild = guild
        self.author_id = author_id
        self.item_id = item_id
        self.poster_input = discord.ui.FileUpload(required=True, min_values=1, max_values=1)
        self.add_item(
            discord.ui.Label(
                text="Póster" if is_movie else "Imagen",
                description="png, jpg, gif o webp (máximo 10 MB). Solo se ve al abrir la ficha.",
                component=self.poster_input,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        store = _store(interaction.client)
        if not _can_edit(interaction.user, store):
            await interaction.response.send_message(
                "❌ No tienes permiso para editar la cartelera.", ephemeral=True
            )
            return

        await interaction.response.defer()
        if not self.poster_input.values:
            await interaction.followup.send("❌ No has subido ninguna imagen.", ephemeral=True)
            return
        try:
            data, extension = await _read_poster(self.poster_input.values[0])
        except ValueError as error:
            await interaction.followup.send(f"❌ {error}", ephemeral=True)
            return

        if not store.set_poster(self.guild.id, self.item_id, data, extension):
            await _show_editor(
                interaction, self.guild, self.author_id, "❌ Eso ya no está en la cartelera."
            )
            return
        await _show_item(interaction, self.guild, self.author_id, self.item_id, "🖼️ Imagen guardada.")

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await _modal_error(interaction, error)


class RetryView(_EditorBaseView):
    """Reabre el formulario con lo que ya se había escrito si algo estaba mal."""

    def __init__(
        self,
        guild: discord.Guild,
        author_id: int,
        kind: str,
        item_id: str | None,
        values: dict[str, str],
        pending_poster: tuple[bytes, str] | None,
    ) -> None:
        super().__init__(guild, author_id)
        self.kind = kind
        self.item_id = item_id
        self.values = values
        self.pending_poster = pending_poster

    @discord.ui.button(label="Corregir", emoji="✏️", style=discord.ButtonStyle.secondary)
    async def retry(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        _ = button
        await interaction.response.send_modal(
            ItemModal(
                self.guild,
                self.author_id,
                self.kind,
                self.item_id,
                self.values,
                self.pending_poster,
            )
        )


async def _open_editor(interaction: discord.Interaction) -> bool:
    """Abre el gestor en un mensaje privado si quien lo pide tiene permiso."""
    guild = interaction.guild
    store = _store(interaction.client)
    if guild is None or not _can_edit(interaction.user, store):
        await interaction.response.send_message(
            "❌ Solo el rol editor de la cartelera (o un admin) puede editarla.",
            ephemeral=True,
        )
        return False

    await interaction.response.send_message(
        embed=_editor_embed(store, guild),
        view=EditorView(store, guild, interaction.user.id),
        ephemeral=True,
    )
    return True


class EditorPromptView(discord.ui.View):
    """Botón temporal de `.noah billboard edit` para abrir el gestor en privado."""

    def __init__(self, guild: discord.Guild, author_id: int) -> None:
        super().__init__(timeout=PROMPT_TIMEOUT)
        self.guild = guild
        self.author_id = author_id
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Este botón no es para ti. Usa `.noah billboard edit`.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(
        label="Abrir gestor", emoji="🛠️", style=discord.ButtonStyle.secondary
    )
    async def open(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        _ = button
        if not await _open_editor(interaction):
            return
        self.stop()
        with suppress(discord.HTTPException):
            await interaction.message.delete()

    async def on_timeout(self) -> None:
        if self.message is not None:
            with suppress(discord.HTTPException):
                await self.message.delete()


# ---------------------------------------------------------------------------
# Configuración (solo admins)


def _build_config_embed(guild: discord.Guild, store: BillboardStore) -> discord.Embed:
    channel = _panel_channel(guild, store)
    _, message_id = store.get_panel(guild.id)
    role_id = store.get_editor_role_id(guild.id)
    role = guild.get_role(role_id) if role_id is not None else None

    embed = discord.Embed(
        title="Configuración de la cartelera",
        description=(
            "Elige el canal del panel (se publica ahí y se queda siempre abajo) y el rol "
            "que puede usar `.noah billboard edit`. Los admins siempre pueden."
        ),
        color=BILLBOARD_COLOR,
    )
    embed.add_field(
        name="Canal",
        value=channel.mention if channel is not None else "Sin configurar",
        inline=True,
    )
    embed.add_field(
        name="Panel",
        value=(
            f"[Ver panel]({_jump_url(guild.id, channel.id, message_id)})"
            if channel is not None and message_id is not None
            else "Sin publicar"
        ),
        inline=True,
    )
    embed.add_field(
        name="Rol editor",
        value=role.mention if role is not None else "Solo admins",
        inline=True,
    )
    embed.add_field(
        name="En cartelera",
        value=(
            f"🎬 `{len(store.list_items(guild.id, KIND_MOVIE))}` películas · "
            f"📢 `{len(store.list_items(guild.id, KIND_ANNOUNCEMENT))}` anuncios"
        ),
        inline=False,
    )
    if channel is not None:
        problem = _channel_problem(channel)
        if problem is not None:
            embed.add_field(name="⚠️ Problema", value=problem, inline=False)
    return embed


class BillboardConfigView(discord.ui.View):
    def __init__(self, guild: discord.Guild, store: BillboardStore, author_id: int) -> None:
        super().__init__(timeout=300)
        self.guild = guild
        self.store = store
        self.author_id = author_id
        self.message: discord.Message | None = None

        channel = _panel_channel(guild, store)
        self.channel_select = discord.ui.ChannelSelect(
            placeholder="Canal de la cartelera",
            channel_types=[discord.ChannelType.text],
            min_values=1,
            max_values=1,
            default_values=[channel] if channel is not None else [],
            row=0,
        )
        self.channel_select.callback = self._on_channel
        self.add_item(self.channel_select)

        role_id = store.get_editor_role_id(guild.id)
        role = guild.get_role(role_id) if role_id is not None else None
        self.role_select = discord.ui.RoleSelect(
            placeholder="Rol que puede editar la cartelera",
            min_values=0,
            max_values=1,
            default_values=[role] if role is not None else [],
            row=1,
        )
        self.role_select.callback = self._on_role
        self.add_item(self.role_select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Solo quien ha abierto la configuración puede usarla.", ephemeral=True
            )
            return False
        return True

    async def refresh(self, interaction: discord.Interaction) -> None:
        view = BillboardConfigView(self.guild, self.store, self.author_id)
        view.message = self.message
        self.stop()
        await _respond_edit(
            interaction, embed=_build_config_embed(self.guild, self.store), view=view
        )

    async def _on_channel(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        channel = self.guild.get_channel(self.channel_select.values[0].id)
        if not isinstance(channel, discord.TextChannel):
            await interaction.followup.send("❌ No encuentro ese canal.", ephemeral=True)
        else:
            problem = await _publish_panel(self.guild, self.store, channel)
            if problem is not None:
                await interaction.followup.send(f"❌ {problem}", ephemeral=True)
        await self.refresh(interaction)

    async def _on_role(self, interaction: discord.Interaction) -> None:
        values = self.role_select.values
        self.store.set_editor_role(self.guild.id, values[0].id if values else None)
        await self.refresh(interaction)

    @discord.ui.button(label="Republicar panel", style=discord.ButtonStyle.secondary, row=2)
    async def republish(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        _ = button
        channel = _panel_channel(self.guild, self.store)
        if channel is None:
            await interaction.response.send_message(
                "❌ Primero elige el canal de la cartelera.", ephemeral=True
            )
            return

        await interaction.response.defer()
        problem = await _publish_panel(self.guild, self.store, channel)
        if problem is not None:
            await interaction.followup.send(f"❌ {problem}", ephemeral=True)
        await self.refresh(interaction)

    @discord.ui.button(label="Quitar panel", style=discord.ButtonStyle.secondary, row=2)
    async def remove(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        _ = button
        await interaction.response.defer()
        await _remove_panel(self.guild, self.store)
        await self.refresh(interaction)

    @discord.ui.button(label="Cerrar", style=discord.ButtonStyle.secondary, row=2)
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        _ = button
        self.stop()
        await interaction.response.edit_message(view=None)

    async def on_timeout(self) -> None:
        if self.message is not None:
            with suppress(discord.HTTPException):
                await self.message.edit(view=None)


def _build_help_embed() -> discord.Embed:
    embed = discord.Embed(
        title="Cartelera",
        description=(
            "Agenda de las pelis que vemos en el server. El panel se queda siempre abajo "
            "en su canal: navega por los próximos días, abre la ficha de cada sesión (con póster) y "
            "apúntate. A los apuntados se les menciona 1 h antes de empezar."
        ),
        color=BILLBOARD_COLOR,
    )
    embed.add_field(
        name="Panel",
        value=(
            "Muestra los próximos 7 días; ◀️ / ▶️ pasan de 7 en 7 y ✏️ **Editar** abre el "
            "gestor (rol editor). "
            "🎟️ **Ficha** abre en privado el póster y el botón **Apuntarme / Desapuntarme**."
        ),
        inline=False,
    )
    embed.add_field(
        name=".noah billboard edit",
        value="Abre el gestor para añadir, editar o quitar películas y anuncios (rol editor).",
        inline=False,
    )
    embed.add_field(
        name=".noah billboard config",
        value="Elige el canal del panel y el rol editor (solo admins).",
        inline=False,
    )
    return embed


# ---------------------------------------------------------------------------
# Recordatorios y limpieza


async def _send_reminder(
    guild: discord.Guild, store: BillboardStore, item: dict[str, Any]
) -> None:
    channel = _panel_channel(guild, store)
    attendees = item_attendees(item)
    start = item_start(item)
    if channel is None or not attendees or start is None:
        store.mark_reminded(guild.id, item["id"], None, None)
        return

    header = (
        f"🍿 **{_truncate(item.get('title') or 'La peli', 80)}** empieza "
        f"{_ts(start, 'R')} ({_ts(start, 't')})"
    )
    if item.get("language"):
        header += f" · {item['language']}"

    chunks: list[str] = []
    current = header + "\n"
    for user_id in attendees:
        mention = f"<@{user_id}> "
        if len(current) + len(mention) > MESSAGE_LIMIT:
            chunks.append(current.strip())
            current = ""
        current += mention
    chunks.append(current.strip())

    first: discord.Message | None = None
    allowed = discord.AllowedMentions(users=True, roles=False, everyone=False)
    for chunk in chunks:
        try:
            sent = await channel.send(chunk, allowed_mentions=allowed)
        except discord.HTTPException:
            break
        first = first or sent

    store.mark_reminded(
        guild.id,
        item["id"],
        channel.id if first is not None else None,
        first.id if first is not None else None,
    )


async def _tick(bot: commands.Bot) -> None:
    store = _store(bot)
    now = _now()
    for guild_id in store.guild_ids():
        guild = bot.get_guild(guild_id)
        if guild is None:
            continue

        for item in store.list_items(guild_id):
            start = item_start(item)
            if start is None:
                continue

            if now >= start + _expire_delta(item):
                await _delete_reminder(guild, store.remove_item(guild_id, item["id"]))
                continue

            if _is_movie(item) and not item.get("reminded") and now >= start - REMINDER_BEFORE:
                if now < start:
                    await _send_reminder(guild, store, item)
                else:
                    # El bot estaba apagado a la hora del aviso: ya no tiene sentido.
                    store.mark_reminded(guild_id, item["id"], None, None)

        # Solo edita si algo ha cambiado (día nuevo, sesión en curso, caducadas…).
        if _panel_channel(guild, store) is not None and not is_guard_active(bot, guild_id):
            await _refresh_panel(bot, guild)


async def _billboard_loop(bot: commands.Bot) -> None:
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            await _tick(bot)
        except Exception:
            _log.exception("Error en el bucle de la cartelera")
        await asyncio.sleep(LOOP_INTERVAL)


# ---------------------------------------------------------------------------
# Registro


def register_billboard_commands(bot: commands.Bot, noah_group: commands.Group) -> None:
    started = False
    sticky_tasks: dict[int, asyncio.Task] = {}
    sticky_dirty: set[int] = set()

    async def _notice(ctx: commands.Context, text: str) -> None:
        with suppress(discord.HTTPException):
            notice = await ctx.send(text, delete_after=NOTICE_DELETE_AFTER)
            _mark_transient(notice.id)

    async def _sticky_worker(guild: discord.Guild) -> None:
        try:
            while True:
                await asyncio.sleep(STICKY_REPOST_DELAY)
                sticky_dirty.discard(guild.id)
                await _repost_sticky_panel(bot, guild)
                if guild.id not in sticky_dirty:
                    return
        finally:
            if sticky_tasks.get(guild.id) is asyncio.current_task():
                sticky_tasks.pop(guild.id, None)

    def _schedule_sticky(guild: discord.Guild) -> None:
        """Agrupa ráfagas de mensajes en un único reenvío del panel."""
        sticky_dirty.add(guild.id)
        pending = sticky_tasks.get(guild.id)
        if pending is not None and not pending.done():
            return
        sticky_tasks[guild.id] = asyncio.create_task(_sticky_worker(guild))

    @bot.listen("on_ready")
    async def _start_billboard() -> None:
        nonlocal started
        if started:
            return
        started = True
        bot.add_view(BillboardPanelView())
        bot.add_dynamic_items(CardButton)
        asyncio.create_task(_billboard_loop(bot))

        # Si alguien escribió mientras el bot estaba apagado, vuelve a dejarlo el último.
        store = _store(bot)
        for guild_id in store.guild_ids():
            guild = bot.get_guild(guild_id)
            if guild is None:
                continue
            channel = _panel_channel(guild, store)
            _, message_id = store.get_panel(guild_id)
            if channel is not None and channel.last_message_id != message_id:
                await _repost_sticky_panel(bot, guild)

    @bot.listen("on_message")
    async def _keep_billboard_last(message: discord.Message) -> None:
        if message.guild is None or _is_panel_message(bot, message):
            return
        if message.id in _transient_ids:
            return

        channel_id, _ = _store(bot).get_panel(message.guild.id)
        if channel_id is None or message.channel.id != channel_id:
            return
        if is_guard_active(bot, message.guild.id):
            return

        _schedule_sticky(message.guild)

    @bot.listen("on_raw_message_delete")
    async def _restore_billboard_panel(payload: discord.RawMessageDeleteEvent) -> None:
        if payload.guild_id is None:
            return

        store = _store(bot)
        channel_id, message_id = store.get_panel(payload.guild_id)
        if message_id != payload.message_id:
            return

        # Si alguien borra el panel a mano, se vuelve a publicar abajo.
        store.set_panel(payload.guild_id, channel_id, None)
        guild = bot.get_guild(payload.guild_id)
        if guild is not None:
            _request_panel_refresh(bot, guild)

    @noah_group.group(invoke_without_command=True)
    async def billboard(ctx: commands.Context) -> None:
        await ctx.send(embed=_build_help_embed())

    @billboard.command(name="help")
    async def billboard_help(ctx: commands.Context) -> None:
        await ctx.send(embed=_build_help_embed())

    @billboard.command(name="config")
    async def billboard_config(ctx: commands.Context) -> None:
        if ctx.guild is None:
            await ctx.send("❌ Este comando solo funciona dentro de un servidor.")
            return

        if not _is_admin(ctx.author):
            await ctx.send("❌ Solo los administradores pueden usar este comando.")
            return

        store = _store(ctx.bot)
        view = BillboardConfigView(ctx.guild, store, ctx.author.id)
        view.message = await ctx.send(embed=_build_config_embed(ctx.guild, store), view=view)

    @billboard.command(name="edit")
    async def billboard_edit(ctx: commands.Context) -> None:
        # Se borra el comando para no ensuciar el canal ni mover el panel.
        _mark_transient(ctx.message.id)
        with suppress(discord.HTTPException):
            await ctx.message.delete()

        if ctx.guild is None:
            await _notice(ctx, "❌ Este comando solo funciona dentro de un servidor.")
            return

        store = _store(ctx.bot)
        if not _can_edit(ctx.author, store):
            await _notice(ctx, "❌ No tienes permiso para editar la cartelera.")
            return

        if _panel_channel(ctx.guild, store) is None:
            await _notice(
                ctx,
                "❌ La cartelera no está configurada. Un admin debe usar "
                "`.noah billboard config`.",
            )
            return

        view = EditorPromptView(ctx.guild, ctx.author.id)
        view.message = await ctx.send(
            f"🛠️ {ctx.author.mention}, pulsa el botón para abrir el gestor de la cartelera "
            "(solo lo verás tú).",
            view=view,
            delete_after=PROMPT_TIMEOUT,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        _mark_transient(view.message.id)
