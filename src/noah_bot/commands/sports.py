"""Agenda de eventos deportivos: panel fijo con botón para apuntarse y ficha con mapa.

Funciona igual que la cartelera (`billboard.py`): un panel que se queda abajo en su
canal con los próximos 7 días, una ficha privada por evento y un gestor para el rol
editor. Además, cada evento tiene duración, ubicación (con mapa de OpenStreetMap y
botón para abrirla en Google Maps), foto opcional y plazas limitadas opcionales.
"""

import asyncio
import io
import logging
import re
from collections.abc import Callable
from contextlib import suppress
from datetime import date, datetime, timedelta, timezone
from typing import Any

import discord
from discord.ext import commands

from noah_bot.commands.billboard import (
    WEEKDAYS,
    _date_options,
    _is_admin,
    _jump_url,
    _local,
    _mark_transient,
    _read_poster,
    _respond_edit,
    _short_when,
    _transient_ids,
    _truncate,
    _ts,
    _week_range,
)
from noah_bot.modules.billboard import (
    combine_local,
    item_attendees,
    item_start,
    parse_local_time,
)
from noah_bot.modules.bot_context import get_bot_context, is_guard_active
from noah_bot.modules.sports import (
    SportsStore,
    event_capacity,
    event_duration,
    event_end,
    format_duration,
    is_full,
    parse_capacity,
    parse_duration,
    prepare_photo,
)
from noah_bot.modules.sports_banner import render_banner
from noah_bot.modules.sports_map import build_map, is_url, maps_url

_log = logging.getLogger(__name__)

SPORTS_COLOR = discord.Color.from_rgb(52, 152, 219)

PANEL_PREV_ID = "noah:sports:prev"
PANEL_TODAY_ID = "noah:sports:today"
PANEL_NEXT_ID = "noah:sports:next"
PANEL_EDIT_ID = "noah:sports:edit"
PANEL_PICK_ID = "noah:sports:pick"
PANEL_JOIN_PREFIX = "noah:sports:join:"
PANEL_IDS = {PANEL_PREV_ID, PANEL_TODAY_ID, PANEL_NEXT_ID, PANEL_EDIT_ID, PANEL_PICK_ID}
PANEL_BANNER_NAME = "agenda-deportiva.png"

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
# Tiempo que un evento sigue en el panel después de terminar.
EXPIRE_AFTER_END = timedelta(hours=1)
MAX_WEEKS_AHEAD = 52
SELECT_LIMIT = 25
# Discord admite 40 componentes por mensaje: con 7 eventos (3 componentes cada uno:
# sección, texto y botón) el panel no pasa de 35.
PANEL_EVENTS_SHOWN = 7
ATTENDEES_SHOWN = 30
ATTENDEES_EDITABLE = 25
MESSAGE_LIMIT = 1900
LINK_URL_LIMIT = 512

# Estado compartido entre el panel persistente, el gestor y los listeners.
_panel_locks: dict[int, asyncio.Lock] = {}
_week_offsets: dict[int, int] = {}
_reset_tasks: dict[int, asyncio.Task] = {}
_refresh_tasks: dict[int, asyncio.Task] = {}
_refresh_dirty: set[int] = set()
_last_rendered: dict[int, Any] = {}


# ---------------------------------------------------------------------------
# Utilidades


def _store(bot: discord.Client) -> SportsStore:
    return get_bot_context(bot).sports  # type: ignore[arg-type]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _can_edit(member: discord.abc.User, store: SportsStore) -> bool:
    if not isinstance(member, discord.Member):
        return False
    if member.guild_permissions.administrator:
        return True
    role_id = store.get_editor_role_id(member.guild.id)
    return role_id is not None and any(role.id == role_id for role in member.roles)


def _panel_channel(guild: discord.Guild, store: SportsStore) -> discord.TextChannel | None:
    channel_id, _ = store.get_panel(guild.id)
    channel = guild.get_channel(channel_id) if channel_id is not None else None
    return channel if isinstance(channel, discord.TextChannel) else None


def _channel_problem(channel: discord.TextChannel) -> str | None:
    permissions = channel.permissions_for(channel.guild.me)
    if not (permissions.send_messages and permissions.embed_links and permissions.attach_files):
        return f"No puedo enviar mensajes con embeds e imágenes en {channel.mention}."
    if not permissions.read_message_history:
        return f"No puedo leer el historial de {channel.mention} (hace falta para dejar el panel abajo)."
    return None


def _title(event: dict[str, Any], limit: int = 80) -> str:
    return _truncate(event.get("title") or "Sin título", limit)


def _place(event: dict[str, Any], limit: int = 100) -> str:
    """Ubicación legible: lo que escribió el editor o, si pegó un enlace de Maps,
    el nombre del sitio que se sacó al generar el mapa."""
    location = str(event.get("location") or "").strip()
    if not location:
        return "Sin ubicación"
    if is_url(location):
        return _truncate(event.get("place") or "Ubicación en Google Maps", limit)
    return _truncate(location, limit)


def _location_link(event: dict[str, Any]) -> str:
    location = str(event.get("location") or "").strip()
    if not location:
        return "Sin ubicación"
    url = maps_url(location)
    label = _place(event, 150).replace("[", "(").replace("]", ")")
    return f"[{label}](<{url}>)"


def _maps_button(event: dict[str, Any]) -> discord.ui.Button | None:
    location = str(event.get("location") or "").strip()
    url = maps_url(location) if location else ""
    if not url or len(url) > LINK_URL_LIMIT:
        return None
    return discord.ui.Button(label="Abrir en Maps", emoji="📍", url=url)


def _spots(event: dict[str, Any]) -> str:
    count = len(item_attendees(event))
    capacity = event_capacity(event)
    if capacity is None:
        return f"👥 {count}"
    return f"👥 {count}/{capacity}" + (" · completo" if count >= capacity else "")


def _day_name(day: date, today: date) -> str:
    if day == today:
        return "Hoy"
    if day == today + timedelta(days=1):
        return "Mañana"
    return f"{WEEKDAYS[day.weekday()].capitalize()} {day.day}"


def _when_short(event: dict[str, Any], today: date) -> str:
    start, end = item_start(event), event_end(event)
    if start is None or end is None:
        return "Sin fecha"
    return f"{_day_name(_local(start).date(), today)} · {_ts(start, 't')} – {_ts(end, 't')}"


def _when_long(event: dict[str, Any]) -> str:
    start, end = item_start(event), event_end(event)
    if start is None or end is None:
        return "Sin fecha"
    duration = format_duration(int(event_duration(event).total_seconds() // 60))
    return f"{_ts(start, 'F')} → {_ts(end, 't')} ({duration}) · {_ts(start, 'R')}"


def _attendees_text(event: dict[str, Any]) -> str:
    attendees = item_attendees(event)
    capacity = event_capacity(event)
    header = (
        f"**Van {len(attendees)} de {capacity}**"
        if capacity is not None
        else f"**Van {len(attendees)}** · sin límite de plazas"
    )
    if not attendees:
        return f"{header}\nNadie todavía. ¡Sé el primero!"
    shown = " ".join(f"<@{user_id}>" for user_id in attendees[:ATTENDEES_SHOWN])
    hidden = len(attendees) - ATTENDEES_SHOWN
    return f"{header}\n{shown}" + (f" y {hidden} más" if hidden > 0 else "")


def _media_files(
    store: SportsStore, event: dict[str, Any], keys: tuple[str, ...] = ("map", "photo")
) -> dict[str, discord.File]:
    """Adjuntos del evento por clave, con nombres estables para `attachment://`."""
    files: dict[str, discord.File] = {}
    for key in keys:
        path = store.media_path(event, key)
        if path is not None:
            files[key] = discord.File(path, filename=f"{key}{path.suffix}")
    return files


# ---------------------------------------------------------------------------
# Panel (agenda de los próximos 7 días, con un botón para apuntarse a cada evento)


def _week_start(offset: int, now: datetime) -> date:
    return _local(now).date() + timedelta(weeks=offset)


def _week_subtitle(offset: int, start: date) -> str:
    return f"Próximos 7 días · {_week_range(start)}" if offset == 0 else _week_range(start)


def _week_events(store: SportsStore, guild_id: int, start_day: date) -> list[dict[str, Any]]:
    end_day = start_day + timedelta(days=7)
    return [
        event
        for event in store.list_events(guild_id)
        if (start := item_start(event)) is not None
        and start_day <= _local(start).date() < end_day
    ]


def _panel_event_text(event: dict[str, Any], now: datetime, today: date) -> str:
    start = item_start(event)
    meta = [_place(event)]
    if start is not None and start <= now:
        meta.append("🔴 en curso")
    return (
        f"**{_title(event)}**\n"
        f"{_when_short(event, today)}\n"
        f"-# {' · '.join(meta)}"
    )


class JoinButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"noah:sports:join:(?P<event_id>[0-9a-f]+)",
):
    """Botón «Apuntarme» de cada evento del panel. Persistente tras reiniciar."""

    def __init__(self, event_id: str, *, label: str = "Apuntarme", disabled: bool = False) -> None:
        super().__init__(
            discord.ui.Button(
                label=label,
                style=discord.ButtonStyle.secondary if disabled else discord.ButtonStyle.success,
                disabled=disabled,
                custom_id=f"{PANEL_JOIN_PREFIX}{event_id}",
            )
        )
        self.event_id = event_id

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
        /,
    ) -> "JoinButton":
        _ = interaction, item
        return cls(match["event_id"])

    async def callback(self, interaction: discord.Interaction) -> None:
        await _join_from_panel(interaction, self.event_id)


def _join_button(event: dict[str, Any], now: datetime) -> JoinButton:
    start = item_start(event)
    if start is not None and start <= now:
        return JoinButton(str(event["id"]), label="En curso", disabled=True)
    if is_full(event):
        return JoinButton(str(event["id"]), label="Completo", disabled=True)
    return JoinButton(str(event["id"]))


def _join_summary(event: dict[str, Any]) -> str:
    start = item_start(event)
    when = f" · {_ts(start, 'f')}" if start is not None else ""
    return f"**{_title(event)}**{when}"


class JoinResultView(discord.ui.View):
    """Respuesta privada al botón del panel: deshacer y abrir la ficha."""

    def __init__(self, guild_id: int, event: dict[str, Any], user_id: int) -> None:
        super().__init__(timeout=DETAIL_TIMEOUT)
        self.guild_id = guild_id
        self.event_id = str(event["id"])

        start = item_start(event)
        if start is None or start > _now():
            if user_id in item_attendees(event):
                toggle = discord.ui.Button(
                    label="Desapuntarme", emoji="❌", style=discord.ButtonStyle.secondary
                )
                toggle.callback = self._leave
                self.add_item(toggle)
            elif not is_full(event):
                toggle = discord.ui.Button(
                    label="Apuntarme", emoji="✅", style=discord.ButtonStyle.success
                )
                toggle.callback = self._join
                self.add_item(toggle)

        detail = discord.ui.Button(
            label="Ver ficha", emoji="🗺️", style=discord.ButtonStyle.secondary
        )
        detail.callback = self._detail
        self.add_item(detail)

    async def _leave(self, interaction: discord.Interaction) -> None:
        store = _store(interaction.client)
        store.leave(self.guild_id, self.event_id, interaction.user.id)
        await self._update(interaction, "Te has desapuntado de {event}.")

    async def _join(self, interaction: discord.Interaction) -> None:
        result = _store(interaction.client).join(
            self.guild_id, self.event_id, interaction.user.id
        )
        await self._update(
            interaction,
            "🚫 Ya no quedan plazas en {event}."
            if result == "full"
            else "✅ Apuntado a {event}. Te mencionaré 1 h antes de que empiece.",
        )

    async def _update(self, interaction: discord.Interaction, template: str) -> None:
        event = _store(interaction.client).get_event(self.guild_id, self.event_id)
        if event is None:
            await interaction.response.edit_message(
                content="❌ Este evento ya no está en la agenda.", view=None
            )
            return
        await interaction.response.edit_message(
            content=template.format(event=_join_summary(event)),
            view=JoinResultView(self.guild_id, event, interaction.user.id),
        )
        if interaction.guild is not None:
            _request_panel_refresh(interaction.client, interaction.guild)

    async def _detail(self, interaction: discord.Interaction) -> None:
        await _open_detail(interaction, self.event_id)


async def _join_from_panel(interaction: discord.Interaction, event_id: str) -> None:
    guild = interaction.guild
    if guild is None:
        return

    store = _store(interaction.client)
    event = store.get_event(guild.id, event_id)
    if event is None:
        await interaction.response.send_message(
            "❌ Ese evento ya no está en la agenda.", ephemeral=True
        )
        return

    user_id = interaction.user.id
    start = item_start(event)
    if start is not None and start <= _now():
        content = f"🔴 {_join_summary(event)} ya ha empezado."
    elif user_id in item_attendees(event):
        content = f"Ya estás apuntado a {_join_summary(event)}."
    elif store.join(guild.id, event_id, user_id) == "full":
        content = f"🚫 Ya no quedan plazas en {_join_summary(event)}."
    else:
        content = (
            f"✅ Apuntado a {_join_summary(event)}. Te mencionaré 1 h antes de que empiece."
        )
        _request_panel_refresh(interaction.client, guild)

    event = store.get_event(guild.id, event_id) or event
    await interaction.response.send_message(
        content, view=JoinResultView(guild.id, event, user_id), ephemeral=True
    )


class PickSelect(
    discord.ui.DynamicItem[discord.ui.Select],
    template=r"noah:sports:pick",
):
    """Menú del panel para abrir la ficha de un evento. Persistente tras reiniciar."""

    def __init__(self, options: list[discord.SelectOption] | None = None) -> None:
        super().__init__(
            discord.ui.Select(
                custom_id=PANEL_PICK_ID,
                placeholder="Ver ficha: mapa, foto y quién va…",
                options=options or [discord.SelectOption(label="—", value="none")],
            )
        )

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Select,
        match: re.Match[str],
        /,
    ) -> "PickSelect":
        _ = interaction, item, match
        return cls()

    async def callback(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            return
        values = (interaction.data or {}).get("values") or []  # type: ignore[union-attr]

        # Volver a pintar el panel deja el menú sin nada elegido. Si no, Discord lo
        # deja marcado y no se puede volver a elegir el mismo evento.
        view, files, signature = _render_panel(_store(interaction.client), guild.id)
        kwargs: dict[str, Any] = {"view": view}
        if _last_rendered.get(guild.id) != signature:
            kwargs["attachments"] = files
        await interaction.response.edit_message(**kwargs)
        _last_rendered[guild.id] = signature

        if values and values[0] != "none":
            await _open_detail(interaction, str(values[0]))


def _pick_options(events: list[dict[str, Any]], today: date) -> list[discord.SelectOption]:
    options: list[discord.SelectOption] = []
    for event in events[:SELECT_LIMIT]:
        start = item_start(event)
        when = (
            f"{_day_name(_local(start).date(), today)} · {_local(start):%H:%M}"
            if start is not None
            else "Sin fecha"
        )
        options.append(
            discord.SelectOption(
                label=_title(event, 100),
                description=_truncate(f"{when} · {_place(event)}", 100),
                value=str(event["id"]),
            )
        )
    return options


def _build_panel_container(
    store: SportsStore, guild_id: int, offset: int, now: datetime
) -> discord.ui.Container:
    today = _local(now).date()
    start_day = _week_start(offset, now)
    container = discord.ui.Container(accent_color=SPORTS_COLOR)
    container.add_item(
        discord.ui.MediaGallery(
            discord.MediaGalleryItem(
                f"attachment://{PANEL_BANNER_NAME}",
                description=f"Agenda deportiva · {_week_subtitle(offset, start_day)}",
            )
        )
    )

    events = _week_events(store, guild_id, start_day)
    for event in events[:PANEL_EVENTS_SHOWN]:
        container.add_item(
            discord.ui.Section(
                discord.ui.TextDisplay(_panel_event_text(event, now, today)),
                accessory=_join_button(event, now),
            )
        )

    hidden = len(events) - PANEL_EVENTS_SHOWN
    if hidden > 0:
        container.add_item(
            discord.ui.TextDisplay(
                f"-# …y {hidden} evento{'' if hidden == 1 else 's'} más (están en el menú)"
            )
        )
    if not events:
        container.add_item(discord.ui.TextDisplay("🏟️ No hay eventos estos días."))
    container.add_item(discord.ui.Separator())

    if events:
        row = discord.ui.ActionRow()
        row.add_item(PickSelect(_pick_options(events, today)))
        container.add_item(row)
    else:
        upcoming = [
            event
            for event in store.list_events(guild_id)
            if (start := item_start(event)) is not None and start > now
        ]
        if upcoming:
            start = item_start(upcoming[0])
            assert start is not None
            container.add_item(
                discord.ui.TextDisplay(
                    f"⏭️ Próximo: **{_title(upcoming[0], 60)}** · {_ts(start, 'R')}"
                )
            )
        else:
            container.add_item(
                discord.ui.TextDisplay("-# Un editor puede añadir eventos con ✏️ Editar.")
            )
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


class SportsPanelView(discord.ui.LayoutView):
    """Panel fijo de eventos. Sin datos solo lleva los botones: así se registra como
    vista persistente para que sigan funcionando tras reiniciar el bot."""

    def __init__(self, container: discord.ui.Container | None = None, offset: int = 0) -> None:
        super().__init__(timeout=None)
        if container is not None:
            self.add_item(container)
        self.add_item(PanelNavRow(offset))


def _component_key(item: discord.ui.Item[Any]) -> tuple[Any, ...]:
    inner = getattr(item, "item", item)
    media = getattr(inner, "media", None)
    return (
        type(inner).__name__,
        getattr(inner, "content", None),
        getattr(inner, "custom_id", None),
        getattr(inner, "label", None),
        getattr(inner, "disabled", None),
        getattr(media, "url", None),
        tuple(option.value for option in getattr(inner, "options", []) or []),
    )


def _render_panel(
    store: SportsStore, guild_id: int
) -> tuple[SportsPanelView, list[discord.File], Any]:
    now = _now()
    offset = _week_offsets.get(guild_id, 0)
    subtitle = _week_subtitle(offset, _week_start(offset, now))
    view = SportsPanelView(_build_panel_container(store, guild_id, offset, now), offset)
    banner = discord.File(io.BytesIO(render_banner(subtitle)), filename=PANEL_BANNER_NAME)
    signature = (subtitle, tuple(_component_key(item) for item in view.walk_children()))
    return view, [banner], signature


def _cancel_reset(guild_id: int) -> None:
    task = _reset_tasks.pop(guild_id, None)
    if task is not None and not task.done() and task is not asyncio.current_task():
        task.cancel()


def _schedule_reset(bot: discord.Client, guild: discord.Guild) -> None:
    """Vuelve a los próximos 7 días tras un rato sin que nadie navegue."""
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


async def _navigate(interaction: discord.Interaction, target: Callable[[int], int]) -> None:
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

    view, files, signature = _render_panel(store, guild.id)
    _last_rendered[guild.id] = signature
    await interaction.response.edit_message(view=view, attachments=files)


async def _publish_panel_unlocked(
    guild: discord.Guild, store: SportsStore, channel: discord.TextChannel
) -> str | None:
    problem = _channel_problem(channel)
    if problem is not None:
        return problem

    view, files, signature = _render_panel(store, guild.id)
    try:
        message = await channel.send(view=view, files=files)
    except discord.HTTPException:
        _log.exception("No se ha podido publicar el panel de eventos")
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
    guild: discord.Guild, store: SportsStore, channel: discord.TextChannel
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
            return "El canal de eventos no está configurado."

        _, message_id = store.get_panel(guild.id)
        if message_id is None:
            return await _publish_panel_unlocked(guild, store, channel)

        view, files, signature = _render_panel(store, guild.id)
        if not force and _last_rendered.get(guild.id) == signature:
            return None

        try:
            await channel.get_partial_message(message_id).edit(view=view, attachments=files)
        except discord.HTTPException:
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


async def _remove_panel(guild: discord.Guild, store: SportsStore) -> None:
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

    def _ids(components: Any) -> set[str]:
        found: set[str] = set()
        for component in components or []:
            custom_id = getattr(component, "custom_id", None)
            if custom_id:
                found.add(custom_id)
            found |= _ids(getattr(component, "children", None))
            found |= _ids(getattr(component, "components", None))
        return found

    return bool(_ids(message.components) & PANEL_IDS)


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
# Ficha pública de un evento (privada para quien la abre)


class DetailView(discord.ui.LayoutView):
    """Ficha del evento: mapa y foto, datos, apuntados y botones."""

    def __init__(
        self,
        guild_id: int,
        event: dict[str, Any],
        user_id: int,
        files: dict[str, discord.File],
        notice: str | None = None,
    ) -> None:
        super().__init__(timeout=DETAIL_TIMEOUT)
        self.guild_id = guild_id
        self.event_id = str(event["id"])

        container = discord.ui.Container(accent_color=SPORTS_COLOR)
        if notice:
            container.add_item(discord.ui.TextDisplay(notice))
        gallery = [
            discord.MediaGalleryItem(f"attachment://{files[key].filename}", description=label)
            for key, label in (("map", "Mapa"), ("photo", "Foto del sitio"))
            if key in files
        ]
        if gallery:
            container.add_item(discord.ui.MediaGallery(*gallery))

        container.add_item(
            discord.ui.TextDisplay(
                f"## 🏅 {_title(event, 200)}\n{_when_long(event)}\n📍 {_location_link(event)}"
            )
        )
        description = str(event.get("description") or "").strip()
        if description:
            container.add_item(discord.ui.TextDisplay(_truncate_block(description, 1500)))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(_attendees_text(event)))
        container.add_item(
            discord.ui.TextDisplay("-# Si te apuntas, te mencionaré 1 h antes de que empiece.")
        )
        self.add_item(container)

        row = discord.ui.ActionRow()
        joined = user_id in item_attendees(event)
        start = item_start(event)
        if start is not None and start <= _now():
            button = discord.ui.Button(
                label="Ya ha empezado", emoji="🔴", style=discord.ButtonStyle.secondary,
                disabled=True,
            )
        elif joined:
            button = discord.ui.Button(
                label="Desapuntarme", emoji="❌", style=discord.ButtonStyle.secondary
            )
        elif is_full(event):
            button = discord.ui.Button(
                label="Completo", emoji="🚫", style=discord.ButtonStyle.secondary, disabled=True
            )
        else:
            button = discord.ui.Button(
                label="Apuntarme", emoji="✅", style=discord.ButtonStyle.success
            )
        button.callback = self._toggle
        row.add_item(button)
        maps_button = _maps_button(event)
        if maps_button is not None:
            row.add_item(maps_button)
        self.add_item(row)

    async def _toggle(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        store = _store(interaction.client)
        event = store.get_event(self.guild_id, self.event_id)
        if guild is None or event is None:
            await interaction.response.edit_message(
                view=_gone_view("❌ Este evento ya no está en la agenda."), attachments=[]
            )
            return

        user_id = interaction.user.id
        start = item_start(event)
        if start is not None and start <= _now():
            notice = "🔴 Este evento ya ha empezado."
        elif user_id in item_attendees(event):
            store.leave(guild.id, self.event_id, user_id)
            notice = "Te has desapuntado."
        else:
            result = store.join(guild.id, self.event_id, user_id)
            notice = (
                "🚫 Ya no quedan plazas."
                if result == "full"
                else "✅ ¡Apuntado! Te mencionaré 1 h antes de que empiece."
            )

        event = store.get_event(guild.id, self.event_id) or event
        files = _media_files(store, event)
        await interaction.response.edit_message(
            view=DetailView(guild.id, event, user_id, files, notice),
            attachments=list(files.values()),
        )
        _request_panel_refresh(interaction.client, guild)


def _truncate_block(text: str, limit: int) -> str:
    """Como `_truncate`, pero respetando los saltos de línea."""
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _gone_view(text: str) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView()
    view.add_item(discord.ui.Container(discord.ui.TextDisplay(text), accent_color=SPORTS_COLOR))
    return view


async def _open_detail(interaction: discord.Interaction, event_id: str) -> None:
    guild = interaction.guild
    if guild is None:
        return

    store = _store(interaction.client)
    if _week_offsets.get(guild.id, 0):
        _schedule_reset(interaction.client, guild)

    event = store.get_event(guild.id, event_id)
    if event is None:
        if interaction.response.is_done():
            await interaction.followup.send("❌ Ese evento ya no está en la agenda.", ephemeral=True)
        else:
            await interaction.response.send_message(
                "❌ Ese evento ya no está en la agenda.", ephemeral=True
            )
        return

    files = _media_files(store, event)
    # Desde el menú del panel ya se ha respondido (editando el panel): va como followup.
    send = (
        interaction.followup.send
        if interaction.response.is_done()
        else interaction.response.send_message
    )
    await send(
        view=DetailView(guild.id, event, interaction.user.id, files),
        files=list(files.values()),
        ephemeral=True,
    )


# ---------------------------------------------------------------------------
# Gestor (solo rol editor / admins)


def build_event_embed(
    store: SportsStore, event: dict[str, Any]
) -> tuple[discord.Embed, list[discord.File]]:
    """Ficha del gestor: embed con el mapa grande y la foto en miniatura."""
    description = str(event.get("description") or "").strip()
    embed = discord.Embed(
        title=_truncate(f"🏅 {event.get('title') or 'Sin título'}", 256),
        description=description[:4096] or None,
        color=SPORTS_COLOR,
    )
    start = item_start(event)
    if start is not None:
        embed.add_field(
            name="📅 Cuándo", value=f"{_ts(start, 'F')}\n{_ts(start, 'R')}", inline=True
        )
    embed.add_field(
        name="⏱️ Duración",
        value=format_duration(int(event_duration(event).total_seconds() // 60)),
        inline=True,
    )
    capacity = event_capacity(event)
    embed.add_field(
        name="🎟️ Plazas", value=str(capacity) if capacity else "Sin límite", inline=True
    )
    location_value = _location_link(event)
    if event.get("location") and store.media_path(event, "map") is None:
        location_value += "\n-# Sin mapa: no he encontrado esta dirección en OpenStreetMap."
    embed.add_field(name="📍 Dónde", value=location_value[:1024], inline=False)
    embed.add_field(
        name=f"👥 Apuntados ({len(item_attendees(event))})",
        value=_attendees_text(event).split("\n", 1)[-1][:1024],
        inline=False,
    )

    files = _media_files(store, event)
    if "map" in files:
        embed.set_image(url=f"attachment://{files['map'].filename}")
    if "photo" in files:
        embed.set_thumbnail(url=f"attachment://{files['photo'].filename}")
    return embed, list(files.values())


def _editor_line(event: dict[str, Any]) -> str:
    start = item_start(event)
    when = f"`{_short_when(start)}` " if start is not None else ""
    return f"🏅 {when}**{_title(event, 60)}** · {_spots(event)}"


def _editor_embed(store: SportsStore, guild: discord.Guild) -> discord.Embed:
    events = store.list_events(guild.id)
    lines = [_editor_line(event) for event in events[:SELECT_LIMIT]]
    if len(events) > SELECT_LIMIT:
        lines.append(f"…y {len(events) - SELECT_LIMIT} más")
    embed = discord.Embed(
        title="🛠️ Gestor de eventos deportivos",
        description="\n".join(lines) or "_No hay eventos._",
        color=SPORTS_COLOR,
    )
    embed.set_footer(text="Añade un evento, o elige uno en el menú para editarlo o quitarlo.")
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


async def _show_event(
    interaction: discord.Interaction,
    guild: discord.Guild,
    author_id: int,
    event_id: str,
    content: str | None = None,
    confirm_delete: bool = False,
) -> None:
    store = _store(interaction.client)
    event = store.get_event(guild.id, event_id)
    if event is None:
        await _show_editor(interaction, guild, author_id, "❌ Ese evento ya no está en la agenda.")
        return

    embed, files = build_event_embed(store, event)
    await _respond_edit(
        interaction,
        content=content,
        embed=embed,
        view=EventManageView(guild, author_id, event, confirm_delete),
        attachments=files,
    )


async def _delete_reminder(guild: discord.Guild, event: dict[str, Any] | None) -> None:
    reminder = event.get("reminder_message") if event else None
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


async def _update_map(store: SportsStore, guild_id: int, event_id: str, location: str) -> bool:
    """Busca la ubicación y guarda el mapa. Devuelve si se ha podido dibujar."""
    geo, image, place = await asyncio.to_thread(build_map, location)
    store.set_map(guild_id, event_id, location, geo, image, place)
    return image is not None


class _EditorBaseView(discord.ui.View):
    def __init__(self, guild: discord.Guild, author_id: int) -> None:
        super().__init__(timeout=EDITOR_TIMEOUT)
        self.guild = guild
        self.author_id = author_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not _can_edit(interaction.user, _store(interaction.client)):
            await interaction.response.send_message(
                "❌ Ya no tienes permiso para editar los eventos.", ephemeral=True
            )
            return False
        return True


class EditorView(_EditorBaseView):
    def __init__(self, store: SportsStore, guild: discord.Guild, author_id: int) -> None:
        super().__init__(guild, author_id)
        events = store.list_events(guild.id)[:SELECT_LIMIT]
        if events:
            options = []
            for event in events:
                start = item_start(event)
                when = _short_when(start) if start is not None else "Sin fecha"
                options.append(
                    discord.SelectOption(
                        label=_title(event, 100),
                        description=_truncate(f"{when} · {_place(event)}", 100),
                        value=str(event["id"]),
                        emoji="🏅",
                    )
                )
            select = discord.ui.Select(placeholder="✏️ Editar o quitar…", options=options, row=1)

            async def _on_select(interaction: discord.Interaction) -> None:
                await _show_event(interaction, self.guild, self.author_id, select.values[0])

            select.callback = _on_select
            self.add_item(select)

    @discord.ui.button(label="Nuevo evento", emoji="🏅", style=discord.ButtonStyle.secondary, row=0)
    async def new_event(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        _ = button
        await interaction.response.send_modal(EventModal(self.guild, self.author_id))

    @discord.ui.button(label="Cerrar", style=discord.ButtonStyle.secondary, row=0)
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        _ = button
        self.stop()
        await interaction.response.defer()
        with suppress(discord.HTTPException):
            await interaction.delete_original_response()


class EventManageView(_EditorBaseView):
    def __init__(
        self,
        guild: discord.Guild,
        author_id: int,
        event: dict[str, Any],
        confirm_delete: bool = False,
    ) -> None:
        super().__init__(guild, author_id)
        self.event_id = str(event["id"])

        if confirm_delete:
            self._add_button("Sí, eliminar", "🗑️", discord.ButtonStyle.danger, self._delete)
            self._add_button("Cancelar", None, discord.ButtonStyle.secondary, self._cancel)
            return

        self._add_button("Editar", "✏️", discord.ButtonStyle.secondary, self._edit)
        self._add_button(
            "Plazas, texto y foto", "📝", discord.ButtonStyle.secondary, self._details
        )
        if event.get("photo"):
            self._add_button("Quitar foto", None, discord.ButtonStyle.secondary, self._remove_photo)
        if event.get("location") and not event.get("map"):
            self._add_button("Reintentar mapa", "🗺️", discord.ButtonStyle.secondary, self._retry_map)
        self._add_button("Apuntados", "👥", discord.ButtonStyle.secondary, self._attendees)
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

    async def _missing(self, interaction: discord.Interaction) -> None:
        await _show_editor(
            interaction, self.guild, self.author_id, "❌ Ese evento ya no está en la agenda."
        )

    async def _edit(self, interaction: discord.Interaction) -> None:
        event = _store(interaction.client).get_event(self.guild.id, self.event_id)
        if event is None:
            await self._missing(interaction)
            return
        start = item_start(event)
        values = {
            "title": event.get("title") or "",
            "date": _local(start).date().isoformat() if start is not None else "",
            "time": f"{_local(start):%H:%M}" if start is not None else "",
            "duration": format_duration(int(event_duration(event).total_seconds() // 60))
            .replace(" min", "")
            .replace(" ", ""),
            "location": event.get("location") or "",
        }
        await interaction.response.send_modal(
            EventModal(self.guild, self.author_id, self.event_id, values)
        )

    async def _details(self, interaction: discord.Interaction) -> None:
        event = _store(interaction.client).get_event(self.guild.id, self.event_id)
        if event is None:
            await self._missing(interaction)
            return
        capacity = event_capacity(event)
        values = {
            "capacity": str(capacity) if capacity else "",
            "description": event.get("description") or "",
        }
        await interaction.response.send_modal(
            DetailsModal(self.guild, self.author_id, self.event_id, values, bool(event.get("photo")))
        )

    async def _remove_photo(self, interaction: discord.Interaction) -> None:
        _store(interaction.client).set_photo(self.guild.id, self.event_id, None)
        await _show_event(interaction, self.guild, self.author_id, self.event_id, "🖼️ Foto quitada.")
        _request_panel_refresh(interaction.client, self.guild)

    async def _retry_map(self, interaction: discord.Interaction) -> None:
        store = _store(interaction.client)
        event = store.get_event(self.guild.id, self.event_id)
        if event is None:
            await self._missing(interaction)
            return
        await interaction.response.defer()
        found = await _update_map(store, self.guild.id, self.event_id, str(event.get("location") or ""))
        await _show_event(
            interaction,
            self.guild,
            self.author_id,
            self.event_id,
            "🗺️ Mapa generado." if found else "❌ Sigo sin encontrar la dirección. Prueba a "
            "escribirla más completa (calle, número y ciudad) o pega un enlace de Google Maps.",
        )

    async def _ask_delete(self, interaction: discord.Interaction) -> None:
        event = _store(interaction.client).get_event(self.guild.id, self.event_id)
        attendees = len(item_attendees(event)) if event is not None else 0
        warning = "⚠️ ¿Seguro que quieres eliminar este evento?"
        if attendees:
            warning += f" Hay **{attendees}** apuntados."
        await _show_event(
            interaction, self.guild, self.author_id, self.event_id, warning, confirm_delete=True
        )

    async def _cancel(self, interaction: discord.Interaction) -> None:
        await _show_event(interaction, self.guild, self.author_id, self.event_id)

    async def _delete(self, interaction: discord.Interaction) -> None:
        removed = _store(interaction.client).remove_event(self.guild.id, self.event_id)
        await _show_editor(
            interaction,
            self.guild,
            self.author_id,
            f"🗑️ Eliminado **{_title(removed)}**." if removed is not None
            else "❌ Ese evento ya no estaba en la agenda.",
        )
        if removed is not None:
            await _delete_reminder(self.guild, removed)
            _request_panel_refresh(interaction.client, self.guild)

    async def _attendees(self, interaction: discord.Interaction) -> None:
        event = _store(interaction.client).get_event(self.guild.id, self.event_id)
        if event is None:
            await self._missing(interaction)
            return
        content = (
            "👥 Marca en el menú a todos los que van (los apuntados ya salen marcados). "
            "Quita la marca para desapuntar a alguien. Tú sí puedes pasarte del límite de plazas."
        )
        if len(item_attendees(event)) > ATTENDEES_EDITABLE:
            content += (
                f"\n-# Solo caben {ATTENDEES_EDITABLE} en el menú: el resto de apuntados "
                "se mantiene tal cual."
            )
        await _respond_edit(
            interaction, content=content, view=AttendeesView(self.guild, self.author_id, event)
        )

    async def _back(self, interaction: discord.Interaction) -> None:
        await _show_editor(interaction, self.guild, self.author_id)


class AttendeesView(_EditorBaseView):
    """Selector para que el editor apunte o desapunte a gente a mano."""

    def __init__(self, guild: discord.Guild, author_id: int, event: dict[str, Any]) -> None:
        super().__init__(guild, author_id)
        self.event_id = str(event["id"])
        self.shown = item_attendees(event)[:ATTENDEES_EDITABLE]

        self.user_select = discord.ui.UserSelect(
            placeholder="Elige quién va a ir",
            min_values=0,
            max_values=ATTENDEES_EDITABLE,
            default_values=[discord.Object(id=user_id) for user_id in self.shown],
            row=0,
        )
        self.user_select.callback = self._on_select
        self.add_item(self.user_select)

        back = discord.ui.Button(label="Volver", emoji="◀️", style=discord.ButtonStyle.secondary, row=1)
        back.callback = self._back
        self.add_item(back)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        store = _store(interaction.client)
        event = store.get_event(self.guild.id, self.event_id)
        if event is None:
            await _show_editor(
                interaction, self.guild, self.author_id, "❌ Ese evento ya no está en la agenda."
            )
            return

        selected = [user.id for user in self.user_select.values if not user.bot]
        kept = [
            user_id
            for user_id in item_attendees(event)
            if user_id not in self.shown and user_id not in selected
        ]
        store.set_attendees(self.guild.id, self.event_id, selected + kept)
        total = len(selected) + len(kept)
        capacity = event_capacity(event)
        content = f"✅ Apuntados actualizados: ahora hay **{total}**."
        if capacity is not None and total > capacity:
            content += f" (Te has pasado del límite de {capacity} plazas.)"
        await _show_event(interaction, self.guild, self.author_id, self.event_id, content)
        _request_panel_refresh(interaction.client, self.guild)

    async def _back(self, interaction: discord.Interaction) -> None:
        await _show_event(interaction, self.guild, self.author_id, self.event_id)


async def _modal_error(interaction: discord.Interaction, error: Exception) -> None:
    _log.exception("Error en un formulario de eventos", exc_info=error)
    message = "❌ No he podido guardar los cambios. Inténtalo de nuevo más tarde."
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


async def _start_modal_submit(interaction: discord.Interaction) -> bool:
    if not _can_edit(interaction.user, _store(interaction.client)):
        await interaction.response.send_message(
            "❌ No tienes permiso para editar los eventos.", ephemeral=True
        )
        return False
    if interaction.message is not None:
        await interaction.response.defer()
    else:
        await interaction.response.defer(ephemeral=True, thinking=True)
    return True


class EventModal(discord.ui.Modal):
    """Paso 1: nombre, día, hora, duración y ubicación (Discord admite 5 campos)."""

    def __init__(
        self,
        guild: discord.Guild,
        author_id: int,
        event_id: str | None = None,
        values: dict[str, str] | None = None,
    ) -> None:
        super().__init__(title="Editar evento" if event_id else "Nuevo evento", timeout=900)
        self.guild = guild
        self.author_id = author_id
        self.event_id = event_id
        values = values or {}

        self.title_input = discord.ui.TextInput(
            max_length=100, default=values.get("title") or None, placeholder="Pádel 2 vs 2"
        )
        self.date_select = discord.ui.Select(
            placeholder="Elige el día",
            options=_date_options(values.get("date") or ""),
            min_values=1,
            max_values=1,
        )
        self.time_input = discord.ui.TextInput(
            max_length=8, default=values.get("time") or None, placeholder="19:30"
        )
        self.duration_input = discord.ui.TextInput(
            max_length=20, default=values.get("duration") or None, placeholder="1h30"
        )
        self.location_input = discord.ui.TextInput(
            max_length=300,
            default=values.get("location") or None,
            placeholder="Club Pádel Norte, Calle Ejemplo 12, Madrid",
        )

        self.add_item(discord.ui.Label(text="Nombre del evento", component=self.title_input))
        self.add_item(discord.ui.Label(text="Día", component=self.date_select))
        self.add_item(
            discord.ui.Label(
                text="Hora", description="Hora de España. Ej: 19:30.", component=self.time_input
            )
        )
        self.add_item(
            discord.ui.Label(
                text="Duración",
                description="Ej: 1h30, 90 (minutos) o 2h.",
                component=self.duration_input,
            )
        )
        self.add_item(
            discord.ui.Label(
                text="Ubicación",
                description="Dirección o enlace de Google Maps. Sale en un mapa en la ficha.",
                component=self.location_input,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await _start_modal_submit(interaction):
            return
        store = _store(interaction.client)
        values = {
            "title": self.title_input.value.strip(),
            "date": self.date_select.values[0] if self.date_select.values else "",
            "time": self.time_input.value.strip(),
            "duration": self.duration_input.value.strip(),
            "location": " ".join(self.location_input.value.split()),
        }

        existing = store.get_event(self.guild.id, self.event_id) if self.event_id else None
        if self.event_id is not None and existing is None:
            await interaction.followup.send("❌ Ese evento ya no está en la agenda.", ephemeral=True)
            return

        errors: list[str] = []
        if not values["title"]:
            errors.append("El nombre no puede estar vacío.")
        if not values["location"]:
            errors.append("Falta la ubicación.")
        duration = 0
        try:
            duration = parse_duration(values["duration"])
        except ValueError as error:
            errors.append(str(error))

        starts_at: datetime | None = None
        if not values["date"]:
            errors.append("Elige el día.")
        elif not values["time"]:
            errors.append("Falta la hora.")
        else:
            try:
                hour, minute = parse_local_time(values["time"])
                starts_at = combine_local(date.fromisoformat(values["date"]), hour, minute)
            except ValueError as error:
                errors.append(str(error))
            else:
                previous = item_start(existing) if existing is not None else None
                if starts_at <= _now() and starts_at != previous:
                    errors.append("Esa hora ya ha pasado.")

        if errors or starts_at is None:
            await interaction.followup.send(
                "\n".join(f"❌ {error}" for error in errors)
                + "\nPulsa **Corregir** para volver al formulario con lo que escribiste.",
                view=RetryView(
                    self.guild,
                    self.author_id,
                    lambda: EventModal(self.guild, self.author_id, self.event_id, values),
                ),
                ephemeral=True,
            )
            return

        location_changed = existing is None or existing.get("location") != values["location"]
        if existing is None:
            event_id = store.add_event(
                self.guild.id,
                title=values["title"],
                starts_at=starts_at,
                duration=duration,
                location=values["location"],
                created_by=interaction.user.id,
            )
            content = (
                "✅ Evento creado. Pulsa 📝 **Plazas, texto y foto** para poner un límite de "
                "plazas, una descripción o una foto del sitio."
            )
        else:
            event_id = str(existing["id"])
            if starts_at != item_start(existing):
                await _delete_reminder(self.guild, existing)
            store.update_basics(
                self.guild.id,
                event_id,
                title=values["title"],
                starts_at=starts_at,
                duration=duration,
                location=values["location"],
            )
            content = "✅ Cambios guardados."

        if location_changed and not await _update_map(
            store, self.guild.id, event_id, values["location"]
        ):
            content += (
                "\n⚠️ No he encontrado la dirección en el mapa. El botón de Google Maps sí "
                "funciona; si quieres el mapa, escríbela más completa o pega un enlace de Maps."
            )

        _request_panel_refresh(interaction.client, self.guild)
        await _show_event(interaction, self.guild, self.author_id, event_id, content)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await _modal_error(interaction, error)


class DetailsModal(discord.ui.Modal):
    """Paso 2: plazas, descripción y foto del sitio."""

    def __init__(
        self,
        guild: discord.Guild,
        author_id: int,
        event_id: str,
        values: dict[str, str] | None = None,
        has_photo: bool = False,
    ) -> None:
        super().__init__(title="Plazas, texto y foto", timeout=900)
        self.guild = guild
        self.author_id = author_id
        self.event_id = event_id
        self.has_photo = has_photo
        values = values or {}

        self.capacity_input = discord.ui.TextInput(
            max_length=3,
            required=False,
            default=values.get("capacity") or None,
            placeholder="8",
        )
        self.description_input = discord.ui.TextInput(
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=1500,
            default=values.get("description") or None,
            placeholder="Nivel, qué hay que llevar, cuánto cuesta la pista…",
        )
        self.photo_input = discord.ui.FileUpload(required=False, min_values=0, max_values=1)

        self.add_item(
            discord.ui.Label(
                text="Plazas máximas",
                description="Vacío = sin límite. Cuando se llena, nadie más puede apuntarse.",
                component=self.capacity_input,
            )
        )
        self.add_item(discord.ui.Label(text="Descripción", component=self.description_input))
        self.add_item(
            discord.ui.Label(
                text="Foto del sitio",
                description=(
                    "Déjalo vacío para mantener la actual."
                    if has_photo
                    else "Opcional. Sale en el panel y en la ficha."
                ),
                component=self.photo_input,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await _start_modal_submit(interaction):
            return
        store = _store(interaction.client)
        values = {
            "capacity": self.capacity_input.value.strip(),
            "description": self.description_input.value.strip(),
        }

        if store.get_event(self.guild.id, self.event_id) is None:
            await interaction.followup.send("❌ Ese evento ya no está en la agenda.", ephemeral=True)
            return

        errors: list[str] = []
        capacity: int | None = None
        try:
            capacity = parse_capacity(values["capacity"])
        except ValueError as error:
            errors.append(str(error))

        photo: tuple[bytes, bytes] | None = None
        if self.photo_input.values:
            try:
                data, _ = await _read_poster(self.photo_input.values[0])
                photo = await asyncio.to_thread(prepare_photo, data)
            except ValueError as error:
                errors.append(str(error))

        if errors:
            await interaction.followup.send(
                "\n".join(f"❌ {error}" for error in errors)
                + "\nPulsa **Corregir** para volver al formulario con lo que escribiste.",
                view=RetryView(
                    self.guild,
                    self.author_id,
                    lambda: DetailsModal(
                        self.guild, self.author_id, self.event_id, values, self.has_photo
                    ),
                ),
                ephemeral=True,
            )
            return

        store.update_details(
            self.guild.id, self.event_id, description=values["description"], capacity=capacity
        )
        if photo is not None:
            store.set_photo(self.guild.id, self.event_id, photo)

        event = store.get_event(self.guild.id, self.event_id)
        content = "✅ Cambios guardados."
        if event is not None and capacity is not None and len(item_attendees(event)) > capacity:
            content += (
                f"\n⚠️ Ya hay {len(item_attendees(event))} apuntados: nadie se queda fuera, "
                "pero no se puede apuntar nadie más."
            )
        _request_panel_refresh(interaction.client, self.guild)
        await _show_event(interaction, self.guild, self.author_id, self.event_id, content)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await _modal_error(interaction, error)


class RetryView(_EditorBaseView):
    """Reabre el formulario con lo que ya se había escrito si algo estaba mal."""

    def __init__(
        self,
        guild: discord.Guild,
        author_id: int,
        make_modal: Callable[[], discord.ui.Modal],
    ) -> None:
        super().__init__(guild, author_id)
        self.make_modal = make_modal

    @discord.ui.button(label="Corregir", emoji="✏️", style=discord.ButtonStyle.secondary)
    async def retry(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        _ = button
        await interaction.response.send_modal(self.make_modal())


async def _open_editor(interaction: discord.Interaction) -> bool:
    """Abre el gestor en un mensaje privado si quien lo pide tiene permiso."""
    guild = interaction.guild
    store = _store(interaction.client)
    if guild is None or not _can_edit(interaction.user, store):
        await interaction.response.send_message(
            "❌ Solo el rol editor de eventos (o un admin) puede editarlos.", ephemeral=True
        )
        return False

    await interaction.response.send_message(
        embed=_editor_embed(store, guild),
        view=EditorView(store, guild, interaction.user.id),
        ephemeral=True,
    )
    return True


class EditorPromptView(discord.ui.View):
    """Botón temporal de `.noah sports edit` para abrir el gestor en privado."""

    def __init__(self, author_id: int) -> None:
        super().__init__(timeout=PROMPT_TIMEOUT)
        self.author_id = author_id
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Este botón no es para ti. Usa `.noah sports edit`.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Abrir gestor", emoji="🛠️", style=discord.ButtonStyle.secondary)
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


def _build_config_embed(guild: discord.Guild, store: SportsStore) -> discord.Embed:
    channel = _panel_channel(guild, store)
    _, message_id = store.get_panel(guild.id)
    role_id = store.get_editor_role_id(guild.id)
    role = guild.get_role(role_id) if role_id is not None else None

    embed = discord.Embed(
        title="Configuración de eventos deportivos",
        description=(
            "Elige el canal del panel (se publica ahí y se queda siempre abajo) y el rol "
            "que puede usar `.noah sports edit`. Los admins siempre pueden."
        ),
        color=SPORTS_COLOR,
    )
    embed.add_field(
        name="Canal", value=channel.mention if channel is not None else "Sin configurar", inline=True
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
        name="Rol editor", value=role.mention if role is not None else "Solo admins", inline=True
    )
    embed.add_field(
        name="En la agenda", value=f"🏅 `{len(store.list_events(guild.id))}` eventos", inline=False
    )
    if channel is not None:
        problem = _channel_problem(channel)
        if problem is not None:
            embed.add_field(name="⚠️ Problema", value=problem, inline=False)
    return embed


class SportsConfigView(discord.ui.View):
    def __init__(self, guild: discord.Guild, store: SportsStore, author_id: int) -> None:
        super().__init__(timeout=300)
        self.guild = guild
        self.store = store
        self.author_id = author_id
        self.message: discord.Message | None = None

        channel = _panel_channel(guild, store)
        self.channel_select = discord.ui.ChannelSelect(
            placeholder="Canal de eventos",
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
            placeholder="Rol que puede editar los eventos",
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
        view = SportsConfigView(self.guild, self.store, self.author_id)
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
    async def republish(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        _ = button
        channel = _panel_channel(self.guild, self.store)
        if channel is None:
            await interaction.response.send_message(
                "❌ Primero elige el canal de eventos.", ephemeral=True
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
        title="Eventos deportivos",
        description=(
            "Agenda de las quedadas para hacer deporte. El panel se queda siempre abajo en "
            "su canal con los próximos 7 días: apúntate con el botón de cada evento o abre su "
            "ficha para ver el mapa, la foto y quién va. A los apuntados se les menciona "
            "1 h antes de empezar."
        ),
        color=SPORTS_COLOR,
    )
    embed.add_field(
        name="Panel",
        value=(
            "**Apuntarme** (a la derecha de cada evento) te apunta al momento; si te arrepientes, "
            "la respuesta trae **Desapuntarme**. En el menú **Ver ficha…** se abre en privado "
            "la ficha con el mapa, la foto, quién va y **Abrir en Maps**. ◀️ / ▶️ pasan de 7 "
            "en 7 días y ✏️ **Editar** abre el gestor (rol editor)."
        ),
        inline=False,
    )
    embed.add_field(
        name=".noah sports edit",
        value=(
            "Abre el gestor para añadir, editar o quitar eventos: nombre, día, hora, duración, "
            "ubicación, plazas, descripción y foto (rol editor)."
        ),
        inline=False,
    )
    embed.add_field(
        name=".noah sports config",
        value="Elige el canal del panel y el rol editor (solo admins).",
        inline=False,
    )
    return embed


# ---------------------------------------------------------------------------
# Recordatorios y limpieza


async def _send_reminder(
    guild: discord.Guild, store: SportsStore, event: dict[str, Any]
) -> None:
    channel = _panel_channel(guild, store)
    attendees = item_attendees(event)
    start = item_start(event)
    if channel is None or not attendees or start is None:
        store.mark_reminded(guild.id, event["id"], None, None)
        return

    header = (
        f"🏅 **{_title(event)}** empieza {_ts(start, 'R')} ({_ts(start, 't')}) "
        f"· 📍 {_location_link(event)}"
    )
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
            sent = await channel.send(chunk, allowed_mentions=allowed, suppress_embeds=True)
        except discord.HTTPException:
            break
        first = first or sent

    store.mark_reminded(
        guild.id,
        event["id"],
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

        for event in store.list_events(guild_id):
            start, end = item_start(event), event_end(event)
            if start is None or end is None:
                continue

            if now >= end + EXPIRE_AFTER_END:
                await _delete_reminder(guild, store.remove_event(guild_id, event["id"]))
                continue

            if not event.get("reminded") and now >= start - REMINDER_BEFORE:
                if now < start:
                    await _send_reminder(guild, store, event)
                else:
                    # El bot estaba apagado a la hora del aviso: ya no tiene sentido.
                    store.mark_reminded(guild_id, event["id"], None, None)

        if _panel_channel(guild, store) is not None and not is_guard_active(bot, guild_id):
            await _refresh_panel(bot, guild)


async def _sports_loop(bot: commands.Bot) -> None:
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            await _tick(bot)
        except Exception:
            _log.exception("Error en el bucle de eventos deportivos")
        await asyncio.sleep(LOOP_INTERVAL)


# ---------------------------------------------------------------------------
# Registro


def register_sports_commands(bot: commands.Bot, noah_group: commands.Group) -> None:
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
        sticky_dirty.add(guild.id)
        pending = sticky_tasks.get(guild.id)
        if pending is not None and not pending.done():
            return
        sticky_tasks[guild.id] = asyncio.create_task(_sticky_worker(guild))

    @bot.listen("on_ready")
    async def _start_sports() -> None:
        nonlocal started
        if started:
            return
        started = True
        bot.add_view(SportsPanelView())
        bot.add_dynamic_items(PickSelect, JoinButton)
        asyncio.create_task(_sports_loop(bot))

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
    async def _keep_sports_last(message: discord.Message) -> None:
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
    async def _restore_sports_panel(payload: discord.RawMessageDeleteEvent) -> None:
        if payload.guild_id is None:
            return
        store = _store(bot)
        channel_id, message_id = store.get_panel(payload.guild_id)
        if message_id != payload.message_id:
            return
        store.set_panel(payload.guild_id, channel_id, None)
        guild = bot.get_guild(payload.guild_id)
        if guild is not None:
            _request_panel_refresh(bot, guild)

    @noah_group.group(name="sports", invoke_without_command=True)
    async def sports(ctx: commands.Context) -> None:
        await ctx.send(embed=_build_help_embed())

    @sports.command(name="help")
    async def sports_help(ctx: commands.Context) -> None:
        await ctx.send(embed=_build_help_embed())

    @sports.command(name="config")
    async def sports_config(ctx: commands.Context) -> None:
        if ctx.guild is None:
            await ctx.send("❌ Este comando solo funciona dentro de un servidor.")
            return
        if not _is_admin(ctx.author):
            await ctx.send("❌ Solo los administradores pueden usar este comando.")
            return
        store = _store(ctx.bot)
        view = SportsConfigView(ctx.guild, store, ctx.author.id)
        view.message = await ctx.send(embed=_build_config_embed(ctx.guild, store), view=view)

    @sports.command(name="edit")
    async def sports_edit(ctx: commands.Context) -> None:
        # Se borra el comando para no ensuciar el canal ni mover el panel.
        _mark_transient(ctx.message.id)
        with suppress(discord.HTTPException):
            await ctx.message.delete()

        if ctx.guild is None:
            await _notice(ctx, "❌ Este comando solo funciona dentro de un servidor.")
            return
        store = _store(ctx.bot)
        if not _can_edit(ctx.author, store):
            await _notice(ctx, "❌ No tienes permiso para editar los eventos.")
            return
        if _panel_channel(ctx.guild, store) is None:
            await _notice(
                ctx,
                "❌ Los eventos no están configurados. Un admin debe usar `.noah sports config`.",
            )
            return

        view = EditorPromptView(ctx.author.id)
        view.message = await ctx.send(
            f"🛠️ {ctx.author.mention}, pulsa el botón para abrir el gestor de eventos "
            "(solo lo verás tú).",
            view=view,
            delete_after=PROMPT_TIMEOUT,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        _mark_transient(view.message.id)
