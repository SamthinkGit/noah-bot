"""Consejo: un canal fijo y oculto para que los admins y el consejo decidan en privado.

El canal «┆📌・𝐂𝐨𝐧𝐬𝐞𝐣𝐨» existe siempre, pero entre consejos está vacío y solo lo
ven los admins. `.noah council init` abre una consola privada para empezar un
consejo (tema, descripción e implicados excluidos). Dentro, `.noah council poll`
publica votaciones anónimas de 2 a 4 opciones (un voto por persona que se puede
corregir mientras el poll esté abierto; los admins ven quién votó qué),
`.noah council meet` deja entrar al rol del consejo y le avisa,
`.noah council exclude` oculta el canal a los implicados y `.noah council finish`
envía el acta (con la conversación en un .txt), borra todos los mensajes y vuelve a
ocultar el canal. El mensaje fijado trae botones para lo mismo.

Todos los mensajes usan Components V2 con el mismo estilo: una caja con franja
dorada, título, separadores finos y los botones dentro.
"""

import asyncio
import io
import logging
import re
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from typing import Any

import discord
from discord.ext import commands

from noah_bot.commands.billboard import (
    _is_admin,
    _local,
    _mark_transient,
    _truncate,
    _ts,
)
from noah_bot.modules.bot_context import get_bot_context
from noah_bot.modules.council import (
    OPTION_MAX_LENGTH,
    QUESTION_MAX_LENGTH,
    VOTE_ALREADY,
    VOTE_CHANGED,
    VOTE_CLOSED,
    VOTE_OK,
    VOTE_SAME,
    CouncilStore,
    parse_datetime,
    parse_options,
    poll_votes,
    tally,
    winners,
)

_log = logging.getLogger(__name__)

COUNCIL_COLOR = discord.Color.from_rgb(201, 162, 39)
CLOSED_COLOR = discord.Color.from_rgb(96, 104, 114)
SUCCESS_COLOR = discord.Color.from_rgb(67, 160, 71)
DANGER_COLOR = discord.Color.from_rgb(170, 32, 38)
CHANNEL_NAME = "┆📌・𝐂𝐨𝐧𝐬𝐞𝐣𝐨"
BAR_LENGTH = 16

CONTROL_TEMPLATE = r"noah:council:ctl:(?P<action>poll|meet|exclude|finish)"
POLL_TEMPLATE = (
    r"noah:council:(?P<action>vote|votes|close):(?P<poll_id>[0-9a-f]+)(?::(?P<index>\d))?"
)

PROMPT_TIMEOUT = 60
NOTICE_DELETE_AFTER = 8
CONSOLE_TIMEOUT = 840
VOTE_REPLY_TIMEOUT = 600
POLL_REFRESH_DELAY = 1.0
EXCLUDE_LIMIT = 25
MENTIONS_SHOWN = 20
TEXT_LIMIT = 1500
REQUIRED_PERMISSIONS = (
    ("manage_channels", "Gestionar canales"),
    ("manage_roles", "Gestionar permisos"),
    ("manage_messages", "Gestionar mensajes"),
)

_guild_locks: dict[int, asyncio.Lock] = {}
_poll_locks: dict[str, asyncio.Lock] = {}
_poll_refresh_tasks: dict[str, asyncio.Task] = {}
_poll_dirty: set[str] = set()


# ---------------------------------------------------------------------------
# Utilidades


def _store(bot: discord.Client) -> CouncilStore:
    return get_bot_context(bot).council  # type: ignore[arg-type]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _guild_lock(guild_id: int) -> asyncio.Lock:
    return _guild_locks.setdefault(guild_id, asyncio.Lock())


def _poll_lock(poll_id: str) -> asyncio.Lock:
    return _poll_locks.setdefault(poll_id, asyncio.Lock())


def _council_role(guild: discord.Guild, store: CouncilStore) -> discord.Role | None:
    role_id = store.get_role_id(guild.id)
    return guild.get_role(role_id) if role_id is not None else None


def _category(guild: discord.Guild, store: CouncilStore) -> discord.CategoryChannel | None:
    category_id = store.get_category_id(guild.id)
    category = guild.get_channel(category_id) if category_id is not None else None
    return category if isinstance(category, discord.CategoryChannel) else None


def _log_channel(guild: discord.Guild, store: CouncilStore) -> discord.TextChannel | None:
    channel_id = store.get_log_channel_id(guild.id)
    channel = guild.get_channel(channel_id) if channel_id is not None else None
    return channel if isinstance(channel, discord.TextChannel) else None


def _fixed_channel(guild: discord.Guild, store: CouncilStore) -> discord.TextChannel | None:
    channel_id = store.get_channel_id(guild.id)
    channel = guild.get_channel(channel_id) if channel_id is not None else None
    return channel if isinstance(channel, discord.TextChannel) else None


def _council_channel(
    guild: discord.Guild, active: dict[str, Any] | None
) -> discord.TextChannel | None:
    if active is None:
        return None
    try:
        channel = guild.get_channel(int(active.get("channel_id")))
    except (TypeError, ValueError):
        return None
    return channel if isinstance(channel, discord.TextChannel) else None


def _can_manage(member: discord.abc.User, store: CouncilStore) -> bool:
    """Admins que no estén excluidos del consejo activo."""
    if not _is_admin(member):
        return False
    return member.id not in store.excluded_ids(member.guild.id)  # type: ignore[union-attr]


def _can_vote(member: discord.abc.User, store: CouncilStore, active: dict[str, Any]) -> bool:
    if not isinstance(member, discord.Member) or member.bot:
        return False
    if member.id in store.excluded_ids(member.guild.id):
        return False
    if member.guild_permissions.administrator:
        return True
    role = _council_role(member.guild, store)
    return bool(active.get("met_at")) and role is not None and role in member.roles


def _voters(guild: discord.Guild, store: CouncilStore, active: dict[str, Any]) -> list[discord.Member]:
    return [member for member in guild.members if _can_vote(member, store, active)]


def _missing_permissions(guild: discord.Guild) -> list[str]:
    permissions = guild.me.guild_permissions
    return [label for name, label in REQUIRED_PERMISSIONS if not getattr(permissions, name)]


def _mentions(user_ids: Any, empty: str = "nadie", limit: int = MENTIONS_SHOWN) -> str:
    ids = list(user_ids)
    if not ids:
        return empty
    shown = ", ".join(f"<@{user_id}>" for user_id in ids[:limit])
    if len(ids) > limit:
        shown += f" y {len(ids) - limit} más"
    return shown


def _block(text: str, limit: int = TEXT_LIMIT) -> str:
    text = str(text).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _name(guild: discord.Guild, user_id: int) -> str:
    member = guild.get_member(int(user_id))
    if member is None:
        return f"usuario {user_id}"
    return f"{member.display_name} (@{member.name})"


def _plural(count: int, singular: str, plural: str) -> str:
    return f"{count} {singular if count == 1 else plural}"


def _format_span(delta: timedelta) -> str:
    minutes = max(0, int(delta.total_seconds() // 60))
    days, minutes = divmod(minutes, 24 * 60)
    hours, minutes = divmod(minutes, 60)
    parts = []
    if days:
        parts.append(f"{days} d")
    if hours:
        parts.append(f"{hours} h")
    if minutes or not parts:
        parts.append(f"{minutes} min")
    return " ".join(parts)


def _admin_warnings(guild: discord.Guild, user_ids: Any) -> list[str]:
    """Discord no deja ocultar un canal a quien tiene permiso de Administrador."""
    warnings = []
    for user_id in user_ids:
        member = guild.get_member(int(user_id))
        if member is not None and member.guild_permissions.administrator:
            warnings.append(
                f"⚠️ {member.mention} es admin: Discord no deja ocultarle el canal, así que lo "
                "seguirá viendo. Eso sí, no podrá votar ni gestionar el consejo."
            )
    return warnings


# -- Piezas del estilo (caja con franja, título y separadores) ---------------


def _box(color: discord.Color = COUNCIL_COLOR) -> discord.ui.Container:
    return discord.ui.Container(accent_color=color)


def _text(content: str) -> discord.ui.TextDisplay:
    return discord.ui.TextDisplay(content)


def _sep(visible: bool = True) -> discord.ui.Separator:
    return discord.ui.Separator(visible=visible, spacing=discord.SeparatorSpacing.small)


def _row(*items: discord.ui.Item[Any]) -> discord.ui.ActionRow:
    row = discord.ui.ActionRow()
    for item in items:
        row.add_item(item)
    return row


def _button(
    label: str,
    callback: Callable[[discord.Interaction], Awaitable[None]],
    *,
    emoji: str | None = None,
    style: discord.ButtonStyle = discord.ButtonStyle.secondary,
    disabled: bool = False,
) -> discord.ui.Button:
    button = discord.ui.Button(label=label, emoji=emoji, style=style, disabled=disabled)
    button.callback = callback
    return button


def _note_view(text: str, color: discord.Color = COUNCIL_COLOR) -> discord.ui.LayoutView:
    """Mensaje simple (sin botones) con el estilo del consejo."""
    view = discord.ui.LayoutView()
    box = _box(color)
    box.add_item(_text(text))
    view.add_item(box)
    return view


async def _notice(ctx: commands.Context, text: str) -> None:
    with suppress(discord.HTTPException):
        notice = await ctx.send(
            text, delete_after=NOTICE_DELETE_AFTER, allowed_mentions=discord.AllowedMentions.none()
        )
        _mark_transient(notice.id)


async def _reply_private(interaction: discord.Interaction, text: str) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True)
    else:
        await interaction.response.send_message(text, ephemeral=True)


async def _edit_view(interaction: discord.Interaction, view: discord.ui.LayoutView) -> None:
    """Edita el mensaje del componente, tanto si ya se ha respondido como si no."""
    if interaction.response.is_done():
        await interaction.edit_original_response(view=view)
    else:
        await interaction.response.edit_message(view=view)


async def _check_manager(interaction: discord.Interaction, *, needs_active: bool = True) -> bool:
    guild = interaction.guild
    store = _store(interaction.client)
    if guild is None or not _can_manage(interaction.user, store):
        await _reply_private(
            interaction, "❌ Solo los admins (que no estén excluidos) pueden gestionar el consejo."
        )
        return False
    if needs_active and store.get_active(guild.id) is None:
        await _reply_private(interaction, "❌ No hay ningún consejo abierto.")
        return False
    return True


# ---------------------------------------------------------------------------
# Canal fijo


def _bot_overwrite(channel: discord.TextChannel | None = None) -> discord.PermissionOverwrite:
    overwrite = (
        channel.overwrites_for(channel.guild.me) if channel is not None else discord.PermissionOverwrite()
    )
    overwrite.update(
        view_channel=True,
        send_messages=True,
        read_message_history=True,
        embed_links=True,
        attach_files=True,
    )
    return overwrite


async def _hide_channel(
    guild: discord.Guild, store: CouncilStore, channel: discord.TextChannel, reason: str
) -> None:
    """Deja el canal como entre consejos: solo lo ven los admins (y el bot)."""
    everyone = channel.overwrites_for(guild.default_role)
    if everyone.view_channel is not False:
        everyone.view_channel = False
        await channel.set_permissions(guild.default_role, overwrite=everyone, reason=reason)
    await channel.set_permissions(guild.me, overwrite=_bot_overwrite(channel), reason=reason)
    role = _council_role(guild, store)
    if role is not None:
        await channel.set_permissions(
            role, overwrite=discord.PermissionOverwrite(view_channel=False), reason=reason
        )


async def _create_fixed_channel(
    guild: discord.Guild, store: CouncilStore, reason: str
) -> discord.TextChannel:
    overwrites: dict[Any, discord.PermissionOverwrite] = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: _bot_overwrite(),
    }
    role = _council_role(guild, store)
    if role is not None:
        overwrites[role] = discord.PermissionOverwrite(view_channel=False)
    channel = await guild.create_text_channel(
        CHANNEL_NAME,
        category=_category(guild, store),
        overwrites=overwrites,
        topic="Consejo · entre consejos está vacío y solo lo ven los admins.",
        reason=reason,
    )
    store.set_channel(guild.id, channel.id)
    return channel


async def _clear_messages(
    guild: discord.Guild, store: CouncilStore, channel: discord.TextChannel, reason: str
) -> discord.TextChannel:
    """Borra todos los mensajes. Si no se puede, clona el canal y borra el viejo."""
    try:
        await channel.purge(limit=None, reason=reason)
        return channel
    except discord.HTTPException:
        _log.warning("No se pudo vaciar el canal del consejo; se clona", exc_info=True)
    clone = await channel.clone(reason=reason)
    store.set_channel(guild.id, clone.id)
    with suppress(discord.HTTPException):
        await clone.edit(position=channel.position)
    await channel.delete(reason=reason)
    return clone


# ---------------------------------------------------------------------------
# Mensaje fijado del canal (estado + botones)


class ControlButton(discord.ui.DynamicItem[discord.ui.Button], template=CONTROL_TEMPLATE):
    """Botones del mensaje fijado. Persistentes: funcionan tras reiniciar el bot."""

    def __init__(
        self,
        action: str,
        *,
        label: str | None = None,
        emoji: str | None = None,
        style: discord.ButtonStyle = discord.ButtonStyle.secondary,
    ) -> None:
        super().__init__(
            discord.ui.Button(
                label=label, emoji=emoji, style=style, custom_id=f"noah:council:ctl:{action}"
            )
        )
        self.action = action

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
        /,
    ) -> "ControlButton":
        _ = interaction, item
        return cls(match["action"])

    async def callback(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        store = _store(interaction.client)
        active = store.get_active(guild.id) if guild is not None else None
        if guild is None or active is None or interaction.channel_id != active.get("channel_id"):
            await interaction.response.send_message(
                "❌ Este consejo ya no está abierto.", ephemeral=True
            )
            return
        if not await _check_manager(interaction):
            return
        if self.action == "poll":
            await interaction.response.send_modal(PollModal())
        elif self.action == "meet":
            await interaction.response.defer(ephemeral=True, thinking=True)
            result = await _meet(guild, store, interaction.user)
            await interaction.followup.send(result, ephemeral=True)
        elif self.action == "exclude":
            await _open_exclusions(interaction)
        elif self.action == "finish":
            await _open_finish(interaction)


def _header_view(
    guild: discord.Guild, store: CouncilStore, active: dict[str, Any]
) -> discord.ui.LayoutView:
    role = _council_role(guild, store)
    role_text = role.mention if role is not None else "el rol del consejo"
    opened = parse_datetime(active.get("opened_at"))
    met = parse_datetime(active.get("met_at"))

    box = _box()
    subtitle = f"Consejo · abierto por <@{active.get('opened_by')}>"
    if opened is not None:
        subtitle += f" {_ts(opened, 'R')}"
    box.add_item(
        _text(f"## 📌 {_truncate(active.get('topic') or 'Sin tema', 200)}\n-# {subtitle}")
    )
    description = str(active.get("description") or "").strip()
    if description:
        box.add_item(_text(_block(description)))
    box.add_item(_sep())

    if met is None:
        box.add_item(
            discord.ui.Section(
                _text(
                    "**🔒 En preparación**\n"
                    "Solo lo ven los admins. Cuando esté listo, reúne "
                    + (f"a {role.mention}." if role is not None else "al consejo.")
                ),
                accessory=ControlButton(
                    "meet", label="Reunir consejo", emoji="📣", style=discord.ButtonStyle.success
                ),
            )
        )
    else:
        box.add_item(_text(f"**🟢 Reunido** {_ts(met, 'R')}\nLo ven los admins y {role_text}."))

    polls = store.list_polls(guild.id)
    open_count = sum(1 for poll in polls if not poll.get("closed"))
    polls_text = (
        f"{len(polls)} · {_plural(open_count, 'abierto', 'abiertos')}" if polls else "ninguno"
    )
    box.add_item(
        _text(
            f"📊 **Polls** · {polls_text}\n"
            f"🚫 **Excluidos** · {_mentions(sorted(store.excluded_ids(guild.id)))}"
        )
    )
    box.add_item(_sep())
    box.add_item(
        _row(
            ControlButton("poll", label="Nuevo poll", emoji="📊", style=discord.ButtonStyle.primary),
            ControlButton("exclude", label="Exclusiones", emoji="🚫"),
            ControlButton("finish", label="Finalizar", emoji="🏁", style=discord.ButtonStyle.danger),
        )
    )
    box.add_item(
        _text("-# Votos anónimos · solo los admins gestionan el consejo · `.noah council help`")
    )

    view = discord.ui.LayoutView(timeout=None)
    view.add_item(box)
    return view


async def _send_header(
    guild: discord.Guild, store: CouncilStore, channel: discord.TextChannel
) -> None:
    active = store.get_active(guild.id)
    if active is None:
        return
    try:
        header = await channel.send(
            view=_header_view(guild, store, active),
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except discord.HTTPException:
        _log.exception("No se pudo publicar el mensaje principal del consejo")
        return
    store.set_header(guild.id, header.id)
    with suppress(discord.HTTPException):
        await header.pin(reason="Mensaje principal del consejo")


async def _refresh_header(guild: discord.Guild, store: CouncilStore) -> None:
    active = store.get_active(guild.id)
    channel = _council_channel(guild, active)
    if active is None or channel is None:
        return
    header_id = active.get("header_message_id")
    if header_id:
        try:
            await channel.get_partial_message(int(header_id)).edit(
                view=_header_view(guild, store, active)
            )
            return
        except discord.NotFound:
            pass
        except discord.HTTPException:
            # Mensaje de una versión anterior que no se puede pasar al estilo nuevo.
            with suppress(discord.HTTPException):
                await channel.get_partial_message(int(header_id)).delete()
    await _send_header(guild, store, channel)


# ---------------------------------------------------------------------------
# Botón temporal para abrir algo en privado desde un comando


class _PromptView(discord.ui.View):
    def __init__(
        self,
        author_id: int,
        label: str,
        emoji: str,
        opener: Callable[[discord.Interaction], Awaitable[bool]],
    ) -> None:
        super().__init__(timeout=PROMPT_TIMEOUT)
        self.author_id = author_id
        self.opener = opener
        self.message: discord.Message | None = None
        button = discord.ui.Button(label=label, emoji=emoji, style=discord.ButtonStyle.secondary)
        button.callback = self._open
        self.add_item(button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Este botón no es para ti.", ephemeral=True)
            return False
        return True

    async def _open(self, interaction: discord.Interaction) -> None:
        if not await self.opener(interaction):
            return
        self.stop()
        if interaction.message is not None:
            with suppress(discord.HTTPException):
                await interaction.message.delete()

    async def on_timeout(self) -> None:
        if self.message is not None:
            with suppress(discord.HTTPException):
                await self.message.delete()


async def _send_prompt(
    ctx: commands.Context,
    label: str,
    emoji: str,
    opener: Callable[[discord.Interaction], Awaitable[bool]],
) -> None:
    view = _PromptView(ctx.author.id, label, emoji, opener)
    view.message = await ctx.send(
        f"🛠️ {ctx.author.mention}, pulsa el botón para continuar (solo lo verás tú).",
        view=view,
        delete_after=PROMPT_TIMEOUT,
        allowed_mentions=discord.AllowedMentions.none(),
    )
    _mark_transient(view.message.id)


# ---------------------------------------------------------------------------
# Consola de `.noah council init`


def _create_problems(guild: discord.Guild, store: CouncilStore) -> list[str]:
    problems = []
    active = store.get_active(guild.id)
    if active is not None:
        channel = _council_channel(guild, active)
        where = f" en {channel.mention}" if channel is not None else ""
        problems.append(f"❌ Ya hay un consejo abierto{where}. Finalízalo antes de abrir otro.")
    if _council_role(guild, store) is None:
        problems.append("❌ Falta elegir el rol del consejo en `.noah council config`.")
    missing = _missing_permissions(guild)
    if missing:
        problems.append(f"❌ Me faltan permisos: {', '.join(missing)}.")
    return problems


class InitConsoleView(discord.ui.LayoutView):
    def __init__(
        self,
        guild: discord.Guild,
        store: CouncilStore,
        author_id: int,
        *,
        topic: str = "",
        description: str = "",
        excluded: list[int] | None = None,
    ) -> None:
        super().__init__(timeout=CONSOLE_TIMEOUT)
        self.guild = guild
        self.store = store
        self.author_id = author_id
        self.topic = topic
        self.description = description
        self.excluded = list(excluded or [])

        role = _council_role(guild, store)
        category = _category(guild, store)
        channel = _fixed_channel(guild, store)
        if channel is not None:
            where = f"Será en {channel.mention}"
        else:
            in_category = f" en **{category.name}**" if category is not None else ""
            where = f"Se creará **{CHANNEL_NAME}**{in_category}"
        role_text = role.mention if role is not None else "el rol del consejo"

        box = _box()
        box.add_item(
            _text(
                "## 📌 Nuevo consejo\n"
                f"-# {where}. Al principio solo lo ven los admins; con 📣 Reunir consejo "
                f"entra {role_text}."
            )
        )
        box.add_item(_sep())

        topic_text = (
            f"**Tema**\n{_truncate(topic, 100)}" if topic else "**Tema**\n*Sin tema todavía*"
        )
        if description:
            topic_text += f"\n\n**Descripción**\n{_block(description, 900)}"
        box.add_item(
            discord.ui.Section(
                _text(topic_text),
                accessory=_button(
                    "Editar" if topic else "Poner tema",
                    self._edit_topic,
                    emoji="✏️",
                    style=discord.ButtonStyle.secondary if topic else discord.ButtonStyle.primary,
                ),
            )
        )
        box.add_item(_sep())

        box.add_item(
            _text(
                f"**🚫 Excluidos** · {_mentions(self.excluded)}\n"
                "-# No verán el canal aunque tengan el rol del consejo y no podrán votar."
            )
        )
        members = [
            member for user_id in self.excluded if (member := guild.get_member(user_id)) is not None
        ]
        self.exclude_select = discord.ui.UserSelect(
            placeholder="Elige a los implicados (opcional)",
            min_values=0,
            max_values=EXCLUDE_LIMIT,
            default_values=members,
        )
        self.exclude_select.callback = self._on_exclude
        box.add_item(_row(self.exclude_select))

        problems = _create_problems(guild, store)
        notes = problems + _admin_warnings(guild, self.excluded)
        if notes:
            box.add_item(_text("\n".join(notes)))
        box.add_item(_sep())
        box.add_item(
            _row(
                _button(
                    "Abrir consejo",
                    self._open,
                    emoji="📌",
                    style=discord.ButtonStyle.success,
                    disabled=not topic or bool(problems),
                ),
                _button("Cancelar", self._cancel),
            )
        )
        self.add_item(box)

    def _copy(self) -> "InitConsoleView":
        return InitConsoleView(
            self.guild,
            self.store,
            self.author_id,
            topic=self.topic,
            description=self.description,
            excluded=self.excluded,
        )

    async def refresh(self, interaction: discord.Interaction) -> None:
        view = self._copy()
        self.stop()
        await _edit_view(interaction, view)

    async def _on_exclude(self, interaction: discord.Interaction) -> None:
        self.excluded = [
            user.id
            for user in self.exclude_select.values
            if not user.bot and user.id != self.author_id
        ]
        await self.refresh(interaction)

    async def _edit_topic(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(TopicModal(self))

    async def _open(self, interaction: discord.Interaction) -> None:
        await _create_council(interaction, self)

    async def _cancel(self, interaction: discord.Interaction) -> None:
        self.stop()
        await interaction.response.edit_message(
            view=_note_view("-# Consejo cancelado. No se ha creado nada.", CLOSED_COLOR)
        )


class TopicModal(discord.ui.Modal):
    def __init__(self, console: InitConsoleView) -> None:
        super().__init__(title="Tema del consejo", timeout=900)
        self.console = console
        self.topic_input = discord.ui.TextInput(
            max_length=100,
            default=console.topic or None,
            placeholder="Sanción a un miembro",
        )
        self.description_input = discord.ui.TextInput(
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=1500,
            default=console.description or None,
            placeholder="Contexto y qué hay que decidir…",
        )
        self.add_item(
            discord.ui.Label(
                text="Tema",
                description="Sale en el canal y en el acta.",
                component=self.topic_input,
            )
        )
        self.add_item(
            discord.ui.Label(
                text="Descripción",
                description="Opcional. Se fija arriba del canal.",
                component=self.description_input,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        self.console.topic = " ".join(self.topic_input.value.split())
        self.console.description = self.description_input.value.strip()
        await self.console.refresh(interaction)


async def _open_console(interaction: discord.Interaction) -> bool:
    if not await _check_manager(interaction, needs_active=False):
        return False
    guild = interaction.guild
    assert guild is not None
    view = InitConsoleView(guild, _store(interaction.client), interaction.user.id)
    await interaction.response.send_message(view=view, ephemeral=True)
    return True


async def _create_council(interaction: discord.Interaction, console: InitConsoleView) -> None:
    guild = console.guild
    store = console.store
    if not await _check_manager(interaction, needs_active=False):
        return
    await interaction.response.defer()

    async with _guild_lock(guild.id):
        problems = _create_problems(guild, store)
        role = _council_role(guild, store)
        if problems or role is None or not console.topic:
            await console.refresh(interaction)
            return

        reason = f"Consejo abierto por {interaction.user}"
        excluded = []
        try:
            channel = _fixed_channel(guild, store)
            if channel is None:
                channel = await _create_fixed_channel(guild, store, reason)
            else:
                await _hide_channel(guild, store, channel, reason)
            for user_id in console.excluded:
                member = guild.get_member(user_id)
                if member is not None:
                    await channel.set_permissions(member, view_channel=False, reason=reason)
                    excluded.append(member.id)
        except discord.HTTPException:
            _log.exception("No se pudo preparar el canal del consejo")
            await interaction.followup.send(
                "❌ No he podido preparar el canal del consejo. Revisa que tenga permiso para "
                "gestionar canales y permisos (y la categoría configurada).",
                ephemeral=True,
            )
            return

        store.start(
            guild.id,
            channel_id=channel.id,
            topic=console.topic,
            description=console.description,
            opened_by=interaction.user.id,
            excluded=excluded,
        )
        await _send_header(guild, store, channel)

    console.stop()
    await _edit_view(
        interaction,
        _note_view(
            "### ✅ Consejo abierto\n"
            f"**{_truncate(console.topic, 100)}** ya está en {channel.mention}.\n"
            "-# De momento solo lo ven los admins. Crea polls y, cuando esté listo, pulsa "
            "📣 Reunir consejo.",
            SUCCESS_COLOR,
        ),
    )


# ---------------------------------------------------------------------------
# Reunir al consejo


async def _meet(guild: discord.Guild, store: CouncilStore, actor: discord.abc.User) -> str:
    async with _guild_lock(guild.id):
        active = store.get_active(guild.id)
        if active is None:
            return "❌ No hay ningún consejo abierto."
        if active.get("met_at"):
            return "ℹ️ El consejo ya está reunido."
        channel = _council_channel(guild, active)
        role = _council_role(guild, store)
        if channel is None:
            return "❌ No encuentro el canal del consejo."
        if role is None:
            return "❌ El rol del consejo no está configurado (`.noah council config`)."
        try:
            await channel.set_permissions(
                role,
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                add_reactions=True,
                reason=f"Consejo reunido por {actor}",
            )
        except discord.HTTPException:
            return "❌ No he podido dar acceso al rol del consejo. Revisa mis permisos."
        store.mark_met(guild.id)

    await _refresh_header(guild, store)
    for poll in store.list_polls(guild.id):
        if not poll.get("closed"):
            _request_poll_refresh(guild, store, poll["id"])

    with suppress(discord.HTTPException):
        await channel.send(
            f"📣 {role.mention} · **el consejo está reunido**\n"
            f"-# Tema: {_truncate(active.get('topic') or 'Sin tema', 150)} · "
            "los votos son anónimos y se pueden corregir mientras el poll esté abierto",
            allowed_mentions=discord.AllowedMentions(everyone=False, users=False, roles=[role]),
        )

    result = f"✅ Consejo reunido: {role.mention} ya puede ver {channel.mention}."
    if not role.mentionable and not channel.permissions_for(guild.me).mention_everyone:
        result += (
            "\n⚠️ El rol no es mencionable y no tengo permiso para mencionar a todos: "
            "puede que no les llegue la notificación."
        )
    return result


# ---------------------------------------------------------------------------
# Exclusiones durante el consejo


class ExclusionsView(discord.ui.LayoutView):
    def __init__(
        self, guild: discord.Guild, store: CouncilStore, author_id: int, last: str = ""
    ) -> None:
        super().__init__(timeout=CONSOLE_TIMEOUT)
        self.guild = guild
        self.store = store
        self.author_id = author_id
        excluded = sorted(store.excluded_ids(guild.id))

        box = _box()
        box.add_item(
            _text(
                "### 🚫 Exclusiones\n"
                "-# Los excluidos no ven el canal ni pueden votar. Si ya habían votado en un "
                "poll abierto, su voto se anula."
            )
        )
        box.add_item(_sep())
        box.add_item(_text(f"**Excluidos** · {_mentions(excluded)}"))
        members = [
            member for user_id in excluded if (member := guild.get_member(user_id)) is not None
        ]
        self.select = discord.ui.UserSelect(
            placeholder="Quién queda excluido",
            min_values=0,
            max_values=EXCLUDE_LIMIT,
            default_values=members[:EXCLUDE_LIMIT],
        )
        self.select.callback = self._on_select
        box.add_item(_row(self.select))
        warnings = _admin_warnings(guild, excluded)
        if warnings:
            box.add_item(_text("\n".join(warnings)))
        if last:
            box.add_item(_sep())
            box.add_item(_text(f"**Último cambio**\n{_block(last, 900)}"))
        box.add_item(_sep())
        box.add_item(_row(_button("Hecho", self._done)))
        self.add_item(box)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        if not await _check_manager(interaction):
            return
        await interaction.response.defer()
        selected = {
            user.id
            for user in self.select.values
            if not user.bot and user.id != interaction.user.id
        }
        last = await _apply_exclusions(self.guild, self.store, selected, interaction.user)
        self.stop()
        await _edit_view(interaction, ExclusionsView(self.guild, self.store, self.author_id, last))

    async def _done(self, interaction: discord.Interaction) -> None:
        self.stop()
        excluded = sorted(self.store.excluded_ids(self.guild.id))
        await interaction.response.edit_message(
            view=_note_view(f"### 🚫 Exclusiones guardadas\n**Excluidos** · {_mentions(excluded)}")
        )


async def _apply_exclusions(
    guild: discord.Guild, store: CouncilStore, selected: set[int], actor: discord.abc.User
) -> str:
    async with _guild_lock(guild.id):
        active = store.get_active(guild.id)
        channel = _council_channel(guild, active)
        if active is None or channel is None:
            return "❌ El consejo ya no está abierto."
        before = store.excluded_ids(guild.id)
        added, removed, failed = [], [], []
        reason = f"Exclusiones del consejo cambiadas por {actor}"
        for user_id in sorted(selected - before):
            member = guild.get_member(user_id)
            if member is None:
                continue
            try:
                await channel.set_permissions(member, view_channel=False, reason=reason)
                added.append(user_id)
            except discord.HTTPException:
                failed.append(user_id)
        for user_id in sorted(before - selected):
            member = guild.get_member(user_id)
            try:
                if member is not None:
                    await channel.set_permissions(member, overwrite=None, reason=reason)
                removed.append(user_id)
            except discord.HTTPException:
                failed.append(user_id)
        final = (before | set(added)) - set(removed)
        touched = store.set_excluded(guild.id, final)

    await _refresh_header(guild, store)
    for poll in store.list_polls(guild.id):
        if not poll.get("closed"):
            _request_poll_refresh(guild, store, poll["id"])

    parts = []
    if added:
        parts.append(f"🚫 Excluidos: {_mentions(added)}")
    if removed:
        parts.append(f"✅ Readmitidos: {_mentions(removed)}")
    if touched:
        parts.append(
            f"🗳️ Votos anulados en {_plural(len(touched), 'poll abierto', 'polls abiertos')}."
        )
    if failed:
        parts.append(f"❌ No he podido cambiar los permisos de {_mentions(failed)}.")
    return "\n".join(parts) or "Sin cambios."


async def _open_exclusions(interaction: discord.Interaction) -> bool:
    if not await _check_manager(interaction):
        return False
    guild = interaction.guild
    assert guild is not None
    await interaction.response.send_message(
        view=ExclusionsView(guild, _store(interaction.client), interaction.user.id),
        ephemeral=True,
    )
    return True


# ---------------------------------------------------------------------------
# Polls


def _bar(count: int, total: int) -> str:
    filled = round(BAR_LENGTH * count / total) if total else 0
    return "▰" * filled + "▱" * (BAR_LENGTH - filled)


def _option_name(poll: dict[str, Any], index: int) -> str:
    options = poll.get("options") or []
    name = options[index] if 0 <= index < len(options) else "?"
    return f"{index + 1} · {name}"


def _result_text(poll: dict[str, Any]) -> str:
    counts = tally(poll)
    total = sum(counts)
    best = winners(poll)
    options = poll.get("options") or []
    if not best:
        return "Nadie ha votado."
    if len(best) == 1:
        index = best[0]
        return f"🏆 **{options[index]}** gana con {counts[index]} de {total}"
    names = ", ".join(f"**{options[index]}**" for index in best)
    return f"🤝 Empate entre {names} ({_plural(counts[best[0]], 'voto', 'votos')} cada una)"


class PollButton(discord.ui.DynamicItem[discord.ui.Button], template=POLL_TEMPLATE):
    """Botones de los polls (votar, ver votos y cerrar). Persistentes tras reiniciar."""

    def __init__(
        self,
        action: str,
        poll_id: str,
        index: int | None = None,
        *,
        label: str | None = None,
        emoji: str | None = None,
        style: discord.ButtonStyle = discord.ButtonStyle.secondary,
        disabled: bool = False,
    ) -> None:
        custom_id = f"noah:council:{action}:{poll_id}"
        if index is not None:
            custom_id += f":{index}"
        super().__init__(
            discord.ui.Button(
                label=label, emoji=emoji, style=style, disabled=disabled, custom_id=custom_id
            )
        )
        self.action = action
        self.poll_id = poll_id
        self.index = index

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
        /,
    ) -> "PollButton":
        _ = interaction, item
        index = int(match["index"]) if match["index"] is not None else None
        return cls(match["action"], match["poll_id"], index)

    async def callback(self, interaction: discord.Interaction) -> None:
        await _poll_action(interaction, self.action, self.poll_id, self.index)


def _poll_view(
    guild: discord.Guild, store: CouncilStore, active: dict[str, Any], poll: dict[str, Any]
) -> discord.ui.LayoutView:
    closed = bool(poll.get("closed"))
    counts = tally(poll)
    total = sum(counts)
    show = closed or bool(poll.get("live"))
    best = set(winners(poll)) if closed else set()

    box = _box(CLOSED_COLOR if closed else COUNCIL_COLOR)
    box.add_item(_text(f"### 📊 {_truncate(poll.get('question') or 'Votación', 250)}"))

    lines = []
    for index in range(len(counts)):
        line = f"**{_option_name(poll, index)}**" + (" 🏆" if index in best else "")
        if show:
            percent = round(counts[index] * 100 / total) if total else 0
            line += f"\n{_bar(counts[index], total)}  **{percent}%** · {counts[index]}"
        lines.append(line)
    box.add_item(_text(("\n\n" if show else "\n").join(lines)))

    if closed:
        box.add_item(_text(_result_text(poll)))
        status = f"🔒 cerrada · {_plural(total, 'voto', 'votos')}"
    else:
        eligible = len(_voters(guild, store, active))
        participation = (
            f"🗳️ {total}/{eligible} han votado"
            if eligible >= total
            else f"🗳️ {_plural(total, 'voto', 'votos')}"
        )
        mode = "📈 en directo" if poll.get("live") else "🙈 resultados al cerrar"
        status = f"{participation} · {mode} · anónimo, se puede corregir"
    box.add_item(_text(f"-# {status}"))
    box.add_item(_sep())

    vote_row = discord.ui.ActionRow()
    for index, option in enumerate(poll.get("options") or []):
        vote_row.add_item(
            PollButton(
                "vote",
                poll["id"],
                index,
                label=_truncate(f"{index + 1} · {option}", 80),
                disabled=closed,
            )
        )
    box.add_item(vote_row)
    admin_row = discord.ui.ActionRow()
    admin_row.add_item(PollButton("votes", poll["id"], label="Votos", emoji="🔍"))
    if not closed:
        admin_row.add_item(PollButton("close", poll["id"], label="Cerrar", emoji="🔒"))
    box.add_item(admin_row)

    view = discord.ui.LayoutView(timeout=None)
    view.add_item(box)
    return view


async def _refresh_poll(guild: discord.Guild, store: CouncilStore, poll_id: str) -> None:
    active = store.get_active(guild.id)
    poll = store.get_poll(guild.id, poll_id)
    channel = _council_channel(guild, active)
    if active is None or poll is None or channel is None or not poll.get("message_id"):
        return
    with suppress(discord.HTTPException):
        await channel.get_partial_message(int(poll["message_id"])).edit(
            view=_poll_view(guild, store, active, poll)
        )


def _request_poll_refresh(guild: discord.Guild, store: CouncilStore, poll_id: str) -> None:
    """Junta varios votos seguidos en una sola edición del mensaje."""
    _poll_dirty.add(poll_id)
    pending = _poll_refresh_tasks.get(poll_id)
    if pending is not None and not pending.done():
        return

    async def _worker() -> None:
        try:
            while poll_id in _poll_dirty:
                _poll_dirty.discard(poll_id)
                await _refresh_poll(guild, store, poll_id)
                await asyncio.sleep(POLL_REFRESH_DELAY)
        finally:
            if _poll_refresh_tasks.get(poll_id) is asyncio.current_task():
                _poll_refresh_tasks.pop(poll_id, None)

    _poll_refresh_tasks[poll_id] = asyncio.create_task(_worker())


class PollModal(discord.ui.Modal):
    def __init__(self, values: dict[str, str] | None = None) -> None:
        super().__init__(title="Nuevo poll del consejo", timeout=900)
        values = values or {}
        self.question_input = discord.ui.TextInput(
            max_length=QUESTION_MAX_LENGTH,
            default=values.get("question") or None,
            placeholder="¿Qué hacemos con…?",
        )
        self.options_input = discord.ui.TextInput(
            style=discord.TextStyle.paragraph,
            max_length=4 * (OPTION_MAX_LENGTH + 4),
            default=values.get("options") or None,
            placeholder="Sí\nNo\nAplazarlo",
        )
        live = values.get("results", "live") == "live"
        self.results_select = discord.ui.Select(
            options=[
                discord.SelectOption(
                    label="En directo",
                    value="live",
                    emoji="📈",
                    description="Los porcentajes se ven mientras se vota.",
                    default=live,
                ),
                discord.SelectOption(
                    label="Ocultos hasta cerrar",
                    value="hidden",
                    emoji="🙈",
                    description="Solo se ve cuánta gente ha votado.",
                    default=not live,
                ),
            ],
            min_values=1,
            max_values=1,
        )
        self.add_item(discord.ui.Label(text="Pregunta", component=self.question_input))
        self.add_item(
            discord.ui.Label(
                text="Opciones",
                description="Entre 2 y 4, una por línea. Cada persona vota solo una.",
                component=self.options_input,
            )
        )
        self.add_item(
            discord.ui.Label(
                text="Resultados",
                description="Los admins siempre pueden ver quién votó qué.",
                component=self.results_select,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await _check_manager(interaction):
            return
        values = {
            "question": self.question_input.value,
            "options": self.options_input.value,
            "results": (self.results_select.values or ["live"])[0],
        }
        question = " ".join(values["question"].split())
        try:
            options = parse_options(values["options"])
        except ValueError as error:
            await interaction.response.send_message(
                f"❌ {error}", view=PollRetryView(values), ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        assert guild is not None
        result = await _publish_poll(
            guild,
            _store(interaction.client),
            question,
            options,
            live=values["results"] == "live",
            author=interaction.user,
        )
        await interaction.followup.send(result, ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        _log.exception("Error en el formulario de poll del consejo", exc_info=error)
        await _reply_private(interaction, "❌ No he podido crear el poll. Inténtalo de nuevo.")


class PollRetryView(discord.ui.View):
    def __init__(self, values: dict[str, str]) -> None:
        super().__init__(timeout=CONSOLE_TIMEOUT)
        self.values = values

    @discord.ui.button(label="Corregir", emoji="✏️", style=discord.ButtonStyle.primary)
    async def retry(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        _ = button
        await interaction.response.send_modal(PollModal(self.values))


async def _publish_poll(
    guild: discord.Guild,
    store: CouncilStore,
    question: str,
    options: list[str],
    *,
    live: bool,
    author: discord.abc.User,
) -> str:
    active = store.get_active(guild.id)
    channel = _council_channel(guild, active)
    if active is None or channel is None:
        return "❌ El consejo ya no está abierto."
    poll = store.add_poll(
        guild.id, question=question, options=options, live=live, created_by=author.id
    )
    if poll is None:
        return "❌ El consejo ya no está abierto."
    try:
        message = await channel.send(view=_poll_view(guild, store, active, poll))
    except discord.HTTPException:
        _log.exception("No se pudo publicar un poll del consejo")
        store.remove_poll(guild.id, poll["id"])
        return f"❌ No he podido publicar el poll en {channel.mention}."
    store.set_poll_message(guild.id, poll["id"], message.id)
    await _refresh_header(guild, store)
    return f"✅ Poll publicado: {message.jump_url}"


async def _poll_action(
    interaction: discord.Interaction, action: str, poll_id: str, index: int | None
) -> None:
    guild = interaction.guild
    store = _store(interaction.client)
    active = store.get_active(guild.id) if guild is not None else None
    poll = store.get_poll(guild.id, poll_id) if guild is not None else None
    if guild is None or active is None or poll is None:
        await interaction.response.send_message("❌ Este poll ya no existe.", ephemeral=True)
        return

    if action == "vote" and index is not None:
        await _vote(interaction, guild, store, active, poll_id, index)
        return
    if not _can_manage(interaction.user, store):
        what = "ver quién votó qué" if action == "votes" else "cerrar el poll"
        await interaction.response.send_message(
            f"🔒 Solo los admins pueden {what}. Tu voto es anónimo para el resto del consejo.",
            ephemeral=True,
        )
        return
    if action == "votes":
        await interaction.response.send_message(
            view=PollAdminView(guild, store, poll_id), ephemeral=True
        )
    elif action == "close":
        await interaction.response.defer(ephemeral=True, thinking=True)
        closed = await _close_poll(guild, store, poll_id)
        await interaction.followup.send(
            "🔒 Poll cerrado." if closed else "ℹ️ Este poll ya estaba cerrado.", ephemeral=True
        )


# -- Respuesta privada al votar (con «Corregir voto») ------------------------

REPLY_VOTED = "voted"
REPLY_PICK = "pick"
REPLY_ASK = "ask"


class VoteReplyView(discord.ui.LayoutView):
    """Respuesta privada al votar: confirma el voto y deja corregirlo.

    - `voted`: «Has votado X» + ✏️ Corregir voto.
    - `pick`: elige el nuevo voto entre las demás opciones.
    - `ask`: ha pulsado otra opción en el poll; ¿cambiar de X a Y?
    """

    def __init__(
        self,
        guild: discord.Guild,
        store: CouncilStore,
        poll_id: str,
        user_id: int,
        state: str = REPLY_VOTED,
        *,
        target: int | None = None,
        notice: str = "",
    ) -> None:
        super().__init__(timeout=VOTE_REPLY_TIMEOUT)
        self.guild = guild
        self.store = store
        self.poll_id = poll_id
        self.user_id = user_id

        poll = store.get_poll(guild.id, poll_id) or {}
        current = poll_votes(poll).get(user_id)
        question = _truncate(poll.get("question") or "Votación", 120)

        if not poll or poll.get("closed") or current is None:
            box = _box(CLOSED_COLOR)
            box.add_item(_text(f"🔒 Esta votación ya está cerrada.\n-# {question}"))
            self.add_item(box)
            return

        if state == REPLY_PICK:
            box = _box()
            box.add_item(
                _text(
                    f"**¿Cuál es tu voto?**\n-# {question} · ahora: {_option_name(poll, current)}"
                )
            )
            row = discord.ui.ActionRow()
            for index in range(len(poll.get("options") or [])):
                row.add_item(
                    _button(
                        _truncate(_option_name(poll, index), 80),
                        self._choose_callback(index),
                        disabled=index == current,
                    )
                )
            box.add_item(row)
            box.add_item(_row(_button("Cancelar", self._back)))
        elif state == REPLY_ASK and target is not None and target != current:
            box = _box()
            box.add_item(
                _text(
                    f"Ya votaste **{_option_name(poll, current)}**.\n"
                    f"¿Quieres cambiarlo a **{_option_name(poll, target)}**?\n-# {question}"
                )
            )
            box.add_item(
                _row(
                    _button(
                        _truncate(f"Cambiar a {_option_name(poll, target)}", 80),
                        self._choose_callback(target),
                        style=discord.ButtonStyle.primary,
                    ),
                    _button("Dejarlo así", self._back),
                )
            )
        else:
            box = _box(SUCCESS_COLOR)
            lead = notice or "✅ Has votado"
            box.add_item(
                _text(
                    f"{lead} **{_option_name(poll, current)}**.\n"
                    f"-# {question} · nadie del consejo verá tu voto (solo los admins)"
                )
            )
            box.add_item(_row(_button("Corregir voto", self._pick, emoji="✏️")))
        self.add_item(box)

    def _redraw(self, state: str, **kwargs: Any) -> "VoteReplyView":
        self.stop()
        return VoteReplyView(self.guild, self.store, self.poll_id, self.user_id, state, **kwargs)

    async def _pick(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(view=self._redraw(REPLY_PICK))

    async def _back(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(view=self._redraw(REPLY_VOTED))

    def _choose_callback(self, index: int) -> Callable[[discord.Interaction], Awaitable[None]]:
        async def _choose(interaction: discord.Interaction) -> None:
            await self._change(interaction, index)

        return _choose

    async def _change(self, interaction: discord.Interaction, index: int) -> None:
        active = self.store.get_active(self.guild.id)
        if active is None or not _can_vote(interaction.user, self.store, active):
            await interaction.response.edit_message(
                view=_note_view("❌ Ya no puedes votar en este consejo.", CLOSED_COLOR)
            )
            return
        async with _poll_lock(self.poll_id):
            status, _ = self.store.vote(
                self.guild.id, self.poll_id, self.user_id, index, change=True
            )
        notice = "✏️ Voto corregido:" if status == VOTE_CHANGED else ""
        await interaction.response.edit_message(view=self._redraw(REPLY_VOTED, notice=notice))
        if status == VOTE_CHANGED:
            _request_poll_refresh(self.guild, self.store, self.poll_id)


async def _vote(
    interaction: discord.Interaction,
    guild: discord.Guild,
    store: CouncilStore,
    active: dict[str, Any],
    poll_id: str,
    index: int,
) -> None:
    if not _can_vote(interaction.user, store, active):
        await interaction.response.send_message(
            "❌ No puedes votar en este consejo.", ephemeral=True
        )
        return
    async with _poll_lock(poll_id):
        status, option = store.vote(guild.id, poll_id, interaction.user.id, index)

    user_id = interaction.user.id
    view: discord.ui.LayoutView
    if status == VOTE_OK:
        view = VoteReplyView(guild, store, poll_id, user_id)
    elif status == VOTE_ALREADY and option is not None and option != index:
        view = VoteReplyView(guild, store, poll_id, user_id, REPLY_ASK, target=index)
    elif status in (VOTE_ALREADY, VOTE_SAME):
        view = VoteReplyView(guild, store, poll_id, user_id, notice="ℹ️ Ya votaste")
    elif status == VOTE_CLOSED:
        view = _note_view("🔒 Esta votación ya está cerrada.", CLOSED_COLOR)
    else:
        view = _note_view("❌ Esta opción ya no existe.", CLOSED_COLOR)
    await interaction.response.send_message(view=view, ephemeral=True)
    if status == VOTE_OK:
        _request_poll_refresh(guild, store, poll_id)


# -- Panel privado de admins: quién votó qué ---------------------------------


class PollAdminView(discord.ui.LayoutView):
    """Panel privado de un poll para admins: quién votó qué y modo de resultados."""

    def __init__(self, guild: discord.Guild, store: CouncilStore, poll_id: str) -> None:
        super().__init__(timeout=CONSOLE_TIMEOUT)
        self.guild = guild
        self.store = store
        self.poll_id = poll_id

        active = store.get_active(guild.id)
        poll = store.get_poll(guild.id, poll_id)
        if active is None or poll is None:
            box = _box(CLOSED_COLOR)
            box.add_item(_text("❌ Este poll ya no existe."))
            self.add_item(box)
            return

        closed = bool(poll.get("closed"))
        votes = poll_votes(poll)
        box = _box(CLOSED_COLOR if closed else COUNCIL_COLOR)
        box.add_item(
            _text(
                f"### 🔍 Votos · {_truncate(poll.get('question') or 'Votación', 200)}\n"
                "-# Solo lo ves tú. El resto del consejo nunca ve quién votó qué."
            )
        )
        box.add_item(_sep())
        lines = []
        for index in range(len(poll.get("options") or [])):
            voters = [user_id for user_id, choice in votes.items() if choice == index]
            lines.append(f"**{_option_name(poll, index)}** · {len(voters)}\n{_mentions(voters)}")
        if not closed:
            pending = [
                member.id for member in _voters(guild, store, active) if member.id not in votes
            ]
            lines.append(f"**⏳ Sin votar** · {len(pending)}\n{_mentions(pending)}")
        box.add_item(_text("\n\n".join(lines)))
        box.add_item(_sep())

        live = bool(poll.get("live"))
        buttons = [_button("Actualizar", self._update, emoji="🔄")]
        if not closed:
            buttons.append(
                _button(
                    "Ocultar resultados" if live else "Mostrar resultados",
                    self._toggle,
                    emoji="🙈" if live else "📈",
                )
            )
            buttons.append(
                _button("Cerrar poll", self._close, emoji="🔒", style=discord.ButtonStyle.danger)
            )
        box.add_item(_row(*buttons))
        if closed:
            box.add_item(_text("-# Votación cerrada"))
        else:
            box.add_item(
                _text(f"-# Resultados {'📈 en directo' if live else '🙈 ocultos hasta cerrar'}")
            )
        self.add_item(box)

    async def _redraw(self, interaction: discord.Interaction) -> None:
        self.stop()
        await _edit_view(interaction, PollAdminView(self.guild, self.store, self.poll_id))

    async def _update(self, interaction: discord.Interaction) -> None:
        await self._redraw(interaction)

    async def _toggle(self, interaction: discord.Interaction) -> None:
        if not await _check_manager(interaction):
            return
        poll = self.store.get_poll(self.guild.id, self.poll_id)
        if poll is not None and not poll.get("closed"):
            self.store.set_poll_live(self.guild.id, self.poll_id, not poll.get("live"))
            await interaction.response.defer()
            await _refresh_poll(self.guild, self.store, self.poll_id)
        await self._redraw(interaction)

    async def _close(self, interaction: discord.Interaction) -> None:
        if not await _check_manager(interaction):
            return
        await interaction.response.defer()
        await _close_poll(self.guild, self.store, self.poll_id)
        await self._redraw(interaction)


async def _close_poll(guild: discord.Guild, store: CouncilStore, poll_id: str) -> bool:
    async with _poll_lock(poll_id):
        closed = store.close_poll(guild.id, poll_id)
    if not closed:
        return False
    await _refresh_poll(guild, store, poll_id)
    await _refresh_header(guild, store)

    poll = store.get_poll(guild.id, poll_id)
    channel = _council_channel(guild, store.get_active(guild.id))
    if poll is not None and channel is not None:
        reference = (
            channel.get_partial_message(int(poll["message_id"])) if poll.get("message_id") else None
        )
        with suppress(discord.HTTPException):
            await channel.send(
                f"🔒 **Votación cerrada** · {_truncate(poll.get('question') or '', 150)}\n"
                f"{_result_text(poll)}",
                reference=reference,
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
    return True


# ---------------------------------------------------------------------------
# Finalizar: acta + vaciar y ocultar el canal


def _component_texts(components: Any) -> list[str]:
    """Textos de los Components V2 de un mensaje (cajas, secciones…)."""
    texts: list[str] = []
    for component in components or []:
        content = getattr(component, "content", None)
        if isinstance(content, str) and content:
            texts.append(" ".join(content.split()))
        texts += _component_texts(getattr(component, "children", None))
        texts += _component_texts(getattr(component, "components", None))
    return texts


async def _collect_transcript(
    channel: discord.TextChannel,
) -> tuple[list[str] | None, set[int]]:
    lines: list[str] = []
    participants: set[int] = set()
    try:
        async for message in channel.history(limit=None, oldest_first=True):
            if message.is_system():
                continue
            author = message.author
            if not author.bot:
                participants.add(author.id)
            parts = [message.clean_content] if message.clean_content else []
            for embed in message.embeds:
                if embed.title:
                    parts.append(f"[{embed.title}]")
            parts += _component_texts(message.components)
            for attachment in message.attachments:
                parts.append(f"[adjunto: {attachment.filename}]")
            when = _local(message.created_at).strftime("%d/%m/%Y %H:%M")
            text = " ".join(parts) or "(sin texto)"
            lines.append(f"[{when}] {author.display_name} (@{author.name}): {text}")
    except discord.HTTPException:
        return None, participants
    return lines, participants


def _when_text(value: Any) -> str:
    parsed = parse_datetime(value)
    return _local(parsed).strftime("%d/%m/%Y %H:%M") if parsed else "—"


def _acta_text(
    guild: discord.Guild,
    store: CouncilStore,
    active: dict[str, Any],
    *,
    transcript: list[str] | None,
    participants: set[int],
    actor: discord.abc.User | None,
    ended_at: datetime,
    lost: bool,
) -> str:
    excluded = store.excluded_ids(guild.id)
    lines = [
        f"ACTA DEL CONSEJO · {guild.name}",
        "=" * 60,
        f"Tema: {active.get('topic') or 'Sin tema'}",
        f"Descripción: {active.get('description') or '—'}",
        f"Abierto por: {_name(guild, active.get('opened_by') or 0)} · {_when_text(active.get('opened_at'))}",
        f"Reunido: {_when_text(active.get('met_at')) if active.get('met_at') else 'No se llegó a reunir'}",
        (
            f"Finalizado por: {_name(guild, actor.id)} · {_local(ended_at):%d/%m/%Y %H:%M}"
            if actor is not None
            else f"Finalizado: el canal se borró a mano · {_local(ended_at):%d/%m/%Y %H:%M}"
        ),
        f"Excluidos: {', '.join(_name(guild, user_id) for user_id in sorted(excluded)) or 'Nadie'}",
        f"Escribieron: {', '.join(_name(guild, user_id) for user_id in sorted(participants)) or 'Nadie'}",
        "",
        "VOTACIONES",
        "-" * 60,
    ]
    polls = store.list_polls(guild.id)
    if not polls:
        lines.append("Sin votaciones.")
    for number, poll in enumerate(polls, start=1):
        votes = poll_votes(poll)
        counts = tally(poll)
        mode = "en directo" if poll.get("live") else "ocultos hasta cerrar"
        lines.append(f"{number}. {poll.get('question')}  (resultados {mode})")
        for index, option in enumerate(poll.get("options") or []):
            names = [_name(guild, user_id) for user_id, choice in votes.items() if choice == index]
            lines.append(f"   {index + 1}) {option} — {_plural(counts[index], 'voto', 'votos')}")
            if names:
                lines.append(f"      {', '.join(names)}")
        lines.append(f"   Resultado: {_result_text(poll).replace('**', '')}")
        lines.append("")

    lines += ["", "CONVERSACIÓN", "-" * 60]
    if lost:
        lines.append("(El canal se borró a mano, así que no se pudo guardar la conversación.)")
    elif transcript is None:
        lines.append("(No se pudo leer la conversación del canal.)")
    else:
        lines += transcript or ["(Nadie escribió nada.)"]
    return "\n".join(lines) + "\n"


def _acta_view(
    guild: discord.Guild,
    store: CouncilStore,
    active: dict[str, Any],
    *,
    participants: set[int],
    actor: discord.abc.User | None,
    ended_at: datetime,
    filename: str,
) -> discord.ui.LayoutView:
    opened = parse_datetime(active.get("opened_at"))
    met = parse_datetime(active.get("met_at"))

    box = _box()
    box.add_item(
        _text(
            f"## 📜 Acta · {_truncate(active.get('topic') or 'Sin tema', 200)}\n"
            f"-# Consejo finalizado {_ts(ended_at, 'f')}"
        )
    )
    description = str(active.get("description") or "").strip()
    if description:
        box.add_item(_text(_block(description, 900)))
    box.add_item(_sep())

    timeline = [
        f"**Abierto** · <@{active.get('opened_by')}>" + (f" · {_ts(opened, 'f')}" if opened else ""),
        f"**Reunido** · {_ts(met, 'f') if met else 'no se llegó a reunir'}",
        "**Finalizado** · " + (actor.mention if actor is not None else "canal borrado a mano"),
    ]
    if opened is not None:
        timeline.append(f"**Duración** · {_format_span(ended_at - opened)}")
    box.add_item(_text("\n".join(timeline)))
    box.add_item(_sep())
    box.add_item(
        _text(
            f"**Escribieron** · {_mentions(sorted(participants))}\n"
            f"**🚫 Excluidos** · {_mentions(sorted(store.excluded_ids(guild.id)))}"
        )
    )
    box.add_item(_sep())
    poll_lines = [
        f"📊 **{_truncate(poll.get('question') or '', 90)}**\n{_result_text(poll)}"
        for poll in store.list_polls(guild.id)
    ]
    box.add_item(_text(_block("\n".join(poll_lines) or "📊 Sin votaciones", 1200)))
    box.add_item(_sep())
    box.add_item(discord.ui.File(f"attachment://{filename}"))
    box.add_item(_text("-# La conversación completa y quién votó qué van en el archivo."))

    view = discord.ui.LayoutView()
    view.add_item(box)
    return view


async def _deliver_acta(
    guild: discord.Guild,
    store: CouncilStore,
    active: dict[str, Any],
    *,
    participants: set[int],
    actor: discord.abc.User | None,
    text: str,
    ended_at: datetime,
) -> str | None:
    """Publica el acta en el canal de actas o, si no se puede, por MD a quien finaliza."""
    data = text.encode("utf-8")
    filename = f"acta-consejo-{_local(ended_at):%Y%m%d-%H%M}.txt"

    targets: list[tuple[discord.abc.Messageable, str]] = []
    log_channel = _log_channel(guild, store)
    if log_channel is not None:
        targets.append((log_channel, log_channel.mention))
    if actor is not None:
        targets.append((actor, "tus mensajes privados"))
    for target, label in targets:
        view = _acta_view(
            guild,
            store,
            active,
            participants=participants,
            actor=actor,
            ended_at=ended_at,
            filename=filename,
        )
        try:
            await target.send(
                view=view,
                file=discord.File(io.BytesIO(data), filename),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return label
        except discord.HTTPException:
            _log.warning("No se pudo enviar el acta del consejo a %s", label, exc_info=True)
    return None


async def _finish_council(
    guild: discord.Guild, store: CouncilStore, actor: discord.abc.User
) -> str:
    async with _guild_lock(guild.id):
        active = store.get_active(guild.id)
        if active is None:
            return "❌ No hay ningún consejo abierto."
        channel = _council_channel(guild, active)
        store.close_all_polls(guild.id)
        ended_at = _now()
        transcript, participants = (
            await _collect_transcript(channel) if channel is not None else (None, set())
        )
        text = _acta_text(
            guild,
            store,
            active,
            transcript=transcript,
            participants=participants,
            actor=actor,
            ended_at=ended_at,
            lost=channel is None,
        )
        delivered_to = await _deliver_acta(
            guild,
            store,
            active,
            participants=participants,
            actor=actor,
            text=text,
            ended_at=ended_at,
        )
        if delivered_to is None:
            for poll in store.list_polls(guild.id):
                await _refresh_poll(guild, store, poll["id"])
            await _refresh_header(guild, store)
            return (
                "❌ No he podido enviar el acta (ni al canal de actas ni por MD), así que no he "
                "vaciado el canal. Revisa `.noah council config` o abre tus MD."
            )

        excluded = store.excluded_ids(guild.id)
        store.end(guild.id)
        if channel is not None:
            problem = await _reset_channel(guild, store, channel, excluded, actor)
            if problem is not None:
                return f"⚠️ Acta enviada a {delivered_to}, pero {problem}."
    return (
        f"✅ Consejo finalizado. Acta enviada a {delivered_to}; el canal queda vacío y oculto "
        "hasta el próximo consejo."
    )


async def _reset_channel(
    guild: discord.Guild,
    store: CouncilStore,
    channel: discord.TextChannel,
    excluded: set[int],
    actor: discord.abc.User,
) -> str | None:
    """Entre consejos: sin mensajes, oculto al rol y sin las exclusiones del anterior."""
    reason = f"Consejo finalizado por {actor}"
    problems = []
    try:
        channel = await _clear_messages(guild, store, channel, reason)
    except discord.HTTPException:
        problems.append("no he podido borrar los mensajes del canal")
    try:
        await _hide_channel(guild, store, channel, reason)
    except discord.HTTPException:
        problems.append("no he podido volver a ocultar el canal al consejo")
    for user_id in excluded:
        member = guild.get_member(user_id)
        if member is not None:
            with suppress(discord.HTTPException):
                await channel.set_permissions(member, overwrite=None, reason=reason)
    return " y ".join(problems) or None


async def _handle_lost_channel(guild: discord.Guild, store: CouncilStore) -> None:
    """El canal se borró a mano (o desapareció con el bot apagado): acta sin conversación."""
    async with _guild_lock(guild.id):
        active = store.get_active(guild.id)
        if active is None or _council_channel(guild, active) is not None:
            return
        store.close_all_polls(guild.id)
        ended_at = _now()
        text = _acta_text(
            guild,
            store,
            active,
            transcript=None,
            participants=set(),
            actor=None,
            ended_at=ended_at,
            lost=True,
        )
        await _deliver_acta(
            guild, store, active, participants=set(), actor=None, text=text, ended_at=ended_at
        )
        store.end(guild.id)
        if store.get_channel_id(guild.id) == active.get("channel_id"):
            store.set_channel(guild.id, None)


class FinishConfirmView(discord.ui.LayoutView):
    def __init__(self, guild: discord.Guild, store: CouncilStore) -> None:
        super().__init__(timeout=PROMPT_TIMEOUT * 2)
        self.guild = guild
        self.store = store

        active = store.get_active(guild.id) or {}
        log_channel = _log_channel(guild, store)
        destination = (
            log_channel.mention if log_channel is not None else "tus MD (no hay canal de actas)"
        )
        open_polls = sum(1 for poll in store.list_polls(guild.id) if not poll.get("closed"))

        box = _box(DANGER_COLOR)
        box.add_item(
            _text(
                "### 🏁 Finalizar consejo\n"
                f"-# {_truncate(active.get('topic') or 'Sin tema', 150)}"
            )
        )
        box.add_item(_sep())
        box.add_item(
            _text(
                f"🔒 Se cerrarán {_plural(open_polls, 'poll abierto', 'polls abiertos')}.\n"
                f"📜 El acta (con la conversación y quién votó qué) irá a {destination}.\n"
                "🧹 Se borrarán **todos los mensajes** y el canal volverá a quedar oculto."
            )
        )
        box.add_item(_text("-# No se puede deshacer."))
        box.add_item(_sep())
        box.add_item(
            _row(
                _button("Finalizar", self._confirm, emoji="🏁", style=discord.ButtonStyle.danger),
                _button("Cancelar", self._cancel),
            )
        )
        self.add_item(box)

    async def _confirm(self, interaction: discord.Interaction) -> None:
        if not await _check_manager(interaction):
            return
        self.stop()
        await interaction.response.edit_message(
            view=_note_view("⏳ Cerrando los polls y generando el acta…")
        )
        result = await _finish_council(self.guild, self.store, interaction.user)
        color = SUCCESS_COLOR if result.startswith("✅") else DANGER_COLOR
        with suppress(discord.HTTPException):
            await interaction.edit_original_response(view=_note_view(result, color))

    async def _cancel(self, interaction: discord.Interaction) -> None:
        self.stop()
        await interaction.response.edit_message(
            view=_note_view("-# El consejo sigue abierto.", CLOSED_COLOR)
        )


async def _open_finish(interaction: discord.Interaction) -> bool:
    if not await _check_manager(interaction):
        return False
    guild = interaction.guild
    assert guild is not None
    await interaction.response.send_message(
        view=FinishConfirmView(guild, _store(interaction.client)), ephemeral=True
    )
    return True


# ---------------------------------------------------------------------------
# Configuración (solo admins)


def _config_problems(guild: discord.Guild, store: CouncilStore) -> list[str]:
    role = _council_role(guild, store)
    log_channel = _log_channel(guild, store)
    problems = []
    missing = _missing_permissions(guild)
    if missing:
        problems.append(f"⚠️ Me faltan permisos: {', '.join(missing)}.")
    if log_channel is not None:
        permissions = log_channel.permissions_for(guild.me)
        if not (permissions.send_messages and permissions.attach_files):
            problems.append(f"⚠️ No puedo enviar mensajes con archivos en {log_channel.mention}.")
        if log_channel.permissions_for(guild.default_role).view_channel or (
            role is not None and log_channel.permissions_for(role).view_channel
        ):
            problems.append(
                f"⚠️ {log_channel.mention} lo puede ver gente que no es admin y el acta dice quién "
                "votó qué. Mejor un canal solo para admins."
            )
    if role is not None and not role.mentionable and not guild.me.guild_permissions.mention_everyone:
        problems.append("⚠️ El rol no es mencionable: el aviso al reunir no les notificará.")
    return problems


class CouncilConfigView(discord.ui.LayoutView):
    def __init__(
        self,
        guild: discord.Guild,
        store: CouncilStore,
        author_id: int,
        *,
        interactive: bool = True,
    ) -> None:
        super().__init__(timeout=300)
        self.guild = guild
        self.store = store
        self.author_id = author_id
        self.message: discord.Message | None = None

        role = _council_role(guild, store)
        fixed = _fixed_channel(guild, store)
        category = _category(guild, store)
        log_channel = _log_channel(guild, store)
        current = _council_channel(guild, store.get_active(guild.id))

        box = _box()
        box.add_item(
            _text(
                "## ⚙️ Consejo · configuración\n"
                "-# El canal existe siempre, pero entre consejos está vacío y solo lo ven los "
                "admins. Al finalizar, el acta va al canal de actas."
            )
        )
        box.add_item(_sep())

        box.add_item(
            _text(f"**Rol del consejo** · {role.mention if role is not None else 'sin configurar'}")
        )
        if interactive:
            self.role_select = discord.ui.RoleSelect(
                placeholder="Rol del consejo",
                min_values=0,
                max_values=1,
                default_values=[role] if role is not None else [],
            )
            self.role_select.callback = self._on_role
            box.add_item(_row(self.role_select))

        box.add_item(
            _text(
                "**Canal del consejo** · "
                + (fixed.mention if fixed is not None else "se crea al abrir el primer consejo")
            )
        )
        if interactive:
            self.channel_select = discord.ui.ChannelSelect(
                placeholder="Elige un canal existente (o créalo abajo)",
                channel_types=[discord.ChannelType.text],
                min_values=0,
                max_values=1,
                default_values=[fixed] if fixed is not None else [],
            )
            self.channel_select.callback = self._on_channel
            box.add_item(_row(self.channel_select))

        box.add_item(
            _text(
                "**Categoría** · "
                + (category.name if category is not None else "sin categoría")
                + "\n-# Solo se usa si hay que crear el canal."
            )
        )
        if interactive:
            self.category_select = discord.ui.ChannelSelect(
                placeholder="Categoría (opcional)",
                channel_types=[discord.ChannelType.category],
                min_values=0,
                max_values=1,
                default_values=[category] if category is not None else [],
            )
            self.category_select.callback = self._on_category
            box.add_item(_row(self.category_select))

        box.add_item(
            _text(
                "**Canal de actas** · "
                + (log_channel.mention if log_channel is not None else "sin canal (va por MD)")
            )
        )
        if interactive:
            self.log_select = discord.ui.ChannelSelect(
                placeholder="Canal de actas (solo admins)",
                channel_types=[discord.ChannelType.text],
                min_values=0,
                max_values=1,
                default_values=[log_channel] if log_channel is not None else [],
            )
            self.log_select.callback = self._on_log
            box.add_item(_row(self.log_select))

        box.add_item(_sep())
        status = (
            f"📌 Consejo abierto en {current.mention}"
            if current is not None
            else "💤 Sin consejo abierto"
        )
        box.add_item(_text("\n".join([status] + _config_problems(guild, store))))
        if interactive:
            buttons = []
            if fixed is None:
                buttons.append(
                    _button(
                        "Crear canal",
                        self._create_channel,
                        emoji="📌",
                        style=discord.ButtonStyle.primary,
                    )
                )
            buttons.append(_button("Cerrar", self._close))
            box.add_item(_row(*buttons))
        self.add_item(box)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Solo quien ha abierto la configuración puede usarla.", ephemeral=True
            )
            return False
        return True

    async def refresh(self, interaction: discord.Interaction) -> None:
        view = CouncilConfigView(self.guild, self.store, self.author_id)
        view.message = self.message
        self.stop()
        await _edit_view(interaction, view)

    async def _on_role(self, interaction: discord.Interaction) -> None:
        values = self.role_select.values
        self.store.set_role(self.guild.id, values[0].id if values else None)
        await self.refresh(interaction)

    async def _on_channel(self, interaction: discord.Interaction) -> None:
        if self.store.get_active(self.guild.id) is not None:
            await interaction.response.send_message(
                "❌ No se puede cambiar el canal con un consejo abierto.", ephemeral=True
            )
            return
        await interaction.response.defer()
        values = self.channel_select.values
        channel = self.guild.get_channel(values[0].id) if values else None
        if isinstance(channel, discord.TextChannel):
            try:
                await _hide_channel(
                    self.guild,
                    self.store,
                    channel,
                    f"Canal del consejo elegido por {interaction.user}",
                )
            except discord.HTTPException:
                await interaction.followup.send(
                    f"❌ No puedo cambiar los permisos de {channel.mention}.", ephemeral=True
                )
                await self.refresh(interaction)
                return
            self.store.set_channel(self.guild.id, channel.id)
        else:
            self.store.set_channel(self.guild.id, None)
        await self.refresh(interaction)

    async def _on_category(self, interaction: discord.Interaction) -> None:
        values = self.category_select.values
        self.store.set_category(self.guild.id, values[0].id if values else None)
        await self.refresh(interaction)

    async def _on_log(self, interaction: discord.Interaction) -> None:
        values = self.log_select.values
        self.store.set_log_channel(self.guild.id, values[0].id if values else None)
        await self.refresh(interaction)

    async def _create_channel(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        if _fixed_channel(self.guild, self.store) is None:
            try:
                await _create_fixed_channel(
                    self.guild, self.store, f"Canal del consejo creado por {interaction.user}"
                )
            except discord.HTTPException:
                await interaction.followup.send(
                    "❌ No he podido crear el canal. Revisa mis permisos y la categoría.",
                    ephemeral=True,
                )
        await self.refresh(interaction)

    async def _close(self, interaction: discord.Interaction) -> None:
        self.stop()
        await interaction.response.edit_message(
            view=CouncilConfigView(self.guild, self.store, self.author_id, interactive=False)
        )

    async def on_timeout(self) -> None:
        if self.message is not None:
            with suppress(discord.HTTPException):
                await self.message.edit(
                    view=CouncilConfigView(
                        self.guild, self.store, self.author_id, interactive=False
                    )
                )


def _help_view() -> discord.ui.LayoutView:
    box = _box()
    box.add_item(
        _text(
            "## 📌 Consejo\n"
            f"-# Canal oculto ({CHANNEL_NAME}) para que el consejo decida en privado. Existe "
            "siempre, pero entre consejos está vacío y solo lo ven los admins."
        )
    )
    box.add_item(_sep())
    box.add_item(
        _text(
            "**`.noah council init`**\n"
            "Consola privada para empezar: tema, descripción y a quién excluir (implicados que "
            "no deben ver el canal aunque estén en el consejo).\n\n"
            "**`.noah council poll`**\n"
            "Votación de 2 a 4 opciones. Un voto por persona, anónimo y corregible mientras "
            "esté abierta. Resultados en directo u ocultos hasta cerrar; con 🔍 Votos los "
            "admins ven quién votó qué.\n\n"
            "**`.noah council meet`**\n"
            "Deja entrar al rol del consejo y le avisa con una mención.\n\n"
            "**`.noah council exclude`**\n"
            "Excluye o readmite gente en cualquier momento (sus votos abiertos se anulan).\n\n"
            "**`.noah council finish`**\n"
            "Cierra los polls, envía el acta (con la conversación en un .txt), borra todos los "
            "mensajes y vuelve a ocultar el canal.\n\n"
            "**`.noah council config`**\n"
            "Rol del consejo, canal del consejo y canal de actas."
        )
    )
    box.add_item(_sep())
    box.add_item(
        _text("-# Todo es solo para admins. El mensaje fijado del canal trae botones para lo mismo.")
    )
    view = discord.ui.LayoutView()
    view.add_item(box)
    return view


# ---------------------------------------------------------------------------
# Registro


def register_council_commands(bot: commands.Bot, noah_group: commands.Group) -> None:
    started = False

    @bot.listen("on_ready")
    async def _start_council() -> None:
        nonlocal started
        if started:
            return
        started = True
        bot.add_dynamic_items(ControlButton, PollButton)

        store = _store(bot)
        for guild_id in store.guild_ids():
            guild = bot.get_guild(guild_id)
            active = store.get_active(guild_id)
            if guild is None or active is None:
                continue
            if _council_channel(guild, active) is None:
                await _handle_lost_channel(guild, store)
            else:
                # Pasa el mensaje fijado al estilo actual si viene de una versión anterior.
                await _refresh_header(guild, store)

    @bot.listen("on_guild_channel_delete")
    async def _council_channel_deleted(channel: discord.abc.GuildChannel) -> None:
        store = _store(bot)
        active = store.get_active(channel.guild.id)
        if active is not None and active.get("channel_id") == channel.id:
            await _handle_lost_channel(channel.guild, store)
        elif store.get_channel_id(channel.guild.id) == channel.id:
            store.set_channel(channel.guild.id, None)

    async def _prepare(ctx: commands.Context, *, needs_active: bool = True) -> bool:
        """Borra el comando (para no dejar rastro) y comprueba permisos."""
        _mark_transient(ctx.message.id)
        with suppress(discord.HTTPException):
            await ctx.message.delete()
        if ctx.guild is None:
            await _notice(ctx, "❌ Este comando solo funciona dentro de un servidor.")
            return False
        store = _store(ctx.bot)
        if not _can_manage(ctx.author, store):
            await _notice(ctx, "❌ Solo los administradores pueden usar este comando.")
            return False
        if needs_active and store.get_active(ctx.guild.id) is None:
            await _notice(ctx, "❌ No hay ningún consejo abierto. Ábrelo con `.noah council init`.")
            return False
        return True

    @noah_group.group(name="council", invoke_without_command=True)
    async def council(ctx: commands.Context) -> None:
        await ctx.send(view=_help_view())

    @council.command(name="help")
    async def council_help(ctx: commands.Context) -> None:
        await ctx.send(view=_help_view())

    @council.command(name="config")
    async def council_config(ctx: commands.Context) -> None:
        if ctx.guild is None:
            await ctx.send("❌ Este comando solo funciona dentro de un servidor.")
            return
        if not _is_admin(ctx.author):
            await ctx.send("❌ Solo los administradores pueden usar este comando.")
            return
        view = CouncilConfigView(ctx.guild, _store(ctx.bot), ctx.author.id)
        view.message = await ctx.send(view=view, allowed_mentions=discord.AllowedMentions.none())

    @council.command(name="init")
    async def council_init(ctx: commands.Context) -> None:
        if not await _prepare(ctx, needs_active=False):
            return
        store = _store(ctx.bot)
        if store.get_active(ctx.guild.id) is not None:
            await _notice(
                ctx, "❌ Ya hay un consejo abierto. Finalízalo con `.noah council finish`."
            )
            return
        if _council_role(ctx.guild, store) is None:
            await _notice(ctx, "❌ Falta elegir el rol del consejo. Usa `.noah council config`.")
            return
        await _send_prompt(ctx, "Abrir consola", "📌", _open_console)

    @council.command(name="poll")
    async def council_poll(ctx: commands.Context) -> None:
        if not await _prepare(ctx):
            return

        async def _open_poll(interaction: discord.Interaction) -> bool:
            if not await _check_manager(interaction):
                return False
            await interaction.response.send_modal(PollModal())
            return True

        await _send_prompt(ctx, "Crear poll", "📊", _open_poll)

    @council.command(name="meet")
    async def council_meet(ctx: commands.Context) -> None:
        if not await _prepare(ctx):
            return
        await _notice(ctx, await _meet(ctx.guild, _store(ctx.bot), ctx.author))

    @council.command(name="exclude")
    async def council_exclude(ctx: commands.Context) -> None:
        if not await _prepare(ctx):
            return
        await _send_prompt(ctx, "Exclusiones", "🚫", _open_exclusions)

    @council.command(name="finish")
    async def council_finish(ctx: commands.Context) -> None:
        if not await _prepare(ctx):
            return
        await _send_prompt(ctx, "Finalizar", "🏁", _open_finish)
