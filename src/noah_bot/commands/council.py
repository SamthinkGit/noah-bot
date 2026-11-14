"""Consejo: un canal fijo y oculto para que los admins y el consejo decidan en privado.

El canal «┆📌・𝐂𝐨𝐧𝐬𝐞𝐣𝐨» existe siempre, pero entre consejos está vacío y solo lo
ven los admins. `.noah council init` abre una consola privada para empezar un
consejo (tema, descripción e implicados excluidos). Dentro, `.noah council poll`
publica votaciones anónimas de 2 a 4 opciones (un voto por persona, sin cambios; los
admins ven quién votó qué), `.noah council meet` deja entrar al rol del consejo y le
avisa, `.noah council exclude` oculta el canal a los implicados y
`.noah council finish` envía el acta (con la conversación en un .txt), borra todos los
mensajes y vuelve a ocultar el canal. El mensaje fijado trae botones para lo mismo.
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
    _jump_url,
    _local,
    _mark_transient,
    _respond_edit,
    _truncate,
    _ts,
)
from noah_bot.modules.bot_context import get_bot_context
from noah_bot.modules.council import (
    OPTION_MAX_LENGTH,
    QUESTION_MAX_LENGTH,
    VOTE_ALREADY,
    VOTE_CLOSED,
    VOTE_OK,
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
DANGER_COLOR = discord.Color.from_rgb(170, 32, 38)
FOOTER = "Noah Council"
CHANNEL_NAME = "┆📌・𝐂𝐨𝐧𝐬𝐞𝐣𝐨"
OPTION_EMOJIS = ("1️⃣", "2️⃣", "3️⃣", "4️⃣")
BAR_LENGTH = 12

CONTROL_POLL_ID = "noah:council:poll"
CONTROL_MEET_ID = "noah:council:meet"
CONTROL_EXCLUDE_ID = "noah:council:exclude"
CONTROL_FINISH_ID = "noah:council:finish"

PROMPT_TIMEOUT = 60
NOTICE_DELETE_AFTER = 8
CONSOLE_TIMEOUT = 840
POLL_REFRESH_DELAY = 1.0
EXCLUDE_LIMIT = 25
MENTIONS_SHOWN = 40
FIELD_LIMIT = 1024
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


def _mentions(user_ids: Any, empty: str = "Nadie") -> str:
    ids = list(user_ids)
    if not ids:
        return empty
    shown = ", ".join(f"<@{user_id}>" for user_id in ids[:MENTIONS_SHOWN])
    if len(ids) > MENTIONS_SHOWN:
        shown += f" y {len(ids) - MENTIONS_SHOWN} más"
    return _field(shown)


def _field(text: str, limit: int = FIELD_LIMIT) -> str:
    text = str(text).strip() or "—"
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
# Mensaje fijado del canal (estado + botones)


def _header_embed(guild: discord.Guild, store: CouncilStore, active: dict[str, Any]) -> discord.Embed:
    role = _council_role(guild, store)
    role_text = role.mention if role is not None else "el rol del consejo"
    embed = discord.Embed(
        title=f"📌 Consejo · {_truncate(active.get('topic') or 'Sin tema', 200)}",
        description=_field(active.get("description") or "*Sin descripción.*", 3000),
        color=COUNCIL_COLOR,
    )
    if active.get("met_at"):
        status = f"🟢 **Reunido** · lo ven los admins y {role_text}."
    else:
        status = (
            "🔒 **En preparación** · de momento solo lo ven los admins.\n"
            f"Pulsa 📣 **Reunir consejo** cuando quieras que entre {role_text}."
        )
    embed.add_field(name="Estado", value=status, inline=False)
    embed.add_field(name="Abierto por", value=f"<@{active.get('opened_by')}>", inline=True)
    opened = parse_datetime(active.get("opened_at"))
    embed.add_field(name="Desde", value=_ts(opened, "R") if opened else "—", inline=True)
    polls = store.list_polls(guild.id)
    open_count = sum(1 for poll in polls if not poll.get("closed"))
    embed.add_field(
        name="Polls", value=f"📊 `{len(polls)}` · `{open_count}` abiertos", inline=True
    )
    embed.add_field(
        name="🚫 Excluidos",
        value=_mentions(store.excluded_ids(guild.id)),
        inline=False,
    )
    embed.set_footer(text=f"{FOOTER} · .noah council help")
    return embed


class CouncilControlView(discord.ui.View):
    """Botones del mensaje fijado. Persistentes: funcionan tras reiniciar el bot."""

    def __init__(self, met: bool = False) -> None:
        super().__init__(timeout=None)
        self.meet.disabled = met

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        guild = interaction.guild
        store = _store(interaction.client)
        active = store.get_active(guild.id) if guild is not None else None
        if active is None or interaction.channel_id != active.get("channel_id"):
            await interaction.response.send_message(
                "❌ Este consejo ya no está abierto.", ephemeral=True
            )
            return False
        return await _check_manager(interaction)

    @discord.ui.button(
        label="Nuevo poll", emoji="📊", style=discord.ButtonStyle.primary, custom_id=CONTROL_POLL_ID
    )
    async def poll(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        _ = button
        await interaction.response.send_modal(PollModal())

    @discord.ui.button(
        label="Reunir consejo",
        emoji="📣",
        style=discord.ButtonStyle.success,
        custom_id=CONTROL_MEET_ID,
    )
    async def meet(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        _ = button
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await _meet(interaction.guild, _store(interaction.client), interaction.user)  # type: ignore[arg-type]
        await interaction.followup.send(result, ephemeral=True)

    @discord.ui.button(
        label="Exclusiones",
        emoji="🚫",
        style=discord.ButtonStyle.secondary,
        custom_id=CONTROL_EXCLUDE_ID,
    )
    async def exclude(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        _ = button
        await _open_exclusions(interaction)

    @discord.ui.button(
        label="Finalizar", emoji="🏁", style=discord.ButtonStyle.danger, custom_id=CONTROL_FINISH_ID
    )
    async def finish(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        _ = button
        await _open_finish(interaction)


async def _refresh_header(guild: discord.Guild, store: CouncilStore) -> None:
    active = store.get_active(guild.id)
    channel = _council_channel(guild, active)
    if active is None or channel is None or not active.get("header_message_id"):
        return
    with suppress(discord.HTTPException):
        await channel.get_partial_message(int(active["header_message_id"])).edit(
            embed=_header_embed(guild, store, active),
            view=CouncilControlView(met=bool(active.get("met_at"))),
        )


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


class InitConsoleView(discord.ui.View):
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

        members = [
            member for user_id in self.excluded if (member := guild.get_member(user_id)) is not None
        ]
        self.exclude_select = discord.ui.UserSelect(
            placeholder="🚫 Excluir a los implicados (opcional)",
            min_values=0,
            max_values=EXCLUDE_LIMIT,
            default_values=members,
            row=0,
        )
        self.exclude_select.callback = self._on_exclude
        self.add_item(self.exclude_select)

        self.topic_button.style = (
            discord.ButtonStyle.secondary if self.topic else discord.ButtonStyle.primary
        )
        self.open_button.disabled = not self.topic or bool(_create_problems(guild, store))

    def embed(self) -> discord.Embed:
        role = _council_role(self.guild, self.store)
        category = _category(self.guild, self.store)
        channel = _fixed_channel(self.guild, self.store)
        if channel is not None:
            where = f"El consejo será en {channel.mention}"
        else:
            in_category = f" en **{category.name}**" if category is not None else ""
            where = f"Se creará el canal **{CHANNEL_NAME}**{in_category}"
        role_text = role.mention if role is not None else "el rol del consejo"
        embed = discord.Embed(
            title="📌 Nuevo consejo",
            description=(
                f"{where}. Al principio solo lo verán los admins; con `.noah council meet` "
                f"(o 📣 en el canal) entrará {role_text}."
            ),
            color=COUNCIL_COLOR,
        )
        embed.add_field(
            name="Tema",
            value=_field(self.topic) if self.topic else "*Sin tema · pulsa ✏️ Tema y descripción*",
            inline=False,
        )
        embed.add_field(
            name="Descripción",
            value=_field(self.description) if self.description else "*Sin descripción*",
            inline=False,
        )
        embed.add_field(
            name="🚫 Excluidos",
            value=(
                _mentions(self.excluded)
                + "\nNo verán el canal aunque tengan el rol del consejo y no podrán votar."
            ),
            inline=False,
        )
        notes = _create_problems(self.guild, self.store) + _admin_warnings(
            self.guild, self.excluded
        )
        if notes:
            embed.add_field(name="Avisos", value=_field("\n".join(notes)), inline=False)
        embed.set_footer(text=FOOTER)
        return embed

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
        await _respond_edit(interaction, embed=view.embed(), view=view)

    async def _on_exclude(self, interaction: discord.Interaction) -> None:
        self.excluded = [
            user.id
            for user in self.exclude_select.values
            if not user.bot and user.id != self.author_id
        ]
        await self.refresh(interaction)

    @discord.ui.button(label="Tema y descripción", emoji="✏️", row=1)
    async def topic_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        _ = button
        await interaction.response.send_modal(TopicModal(self))

    @discord.ui.button(
        label="Abrir consejo", emoji="📌", style=discord.ButtonStyle.success, row=1
    )
    async def open_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        _ = button
        await _create_council(interaction, self)

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.secondary, row=1)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        _ = button
        self.stop()
        await interaction.response.edit_message(
            content="Consejo cancelado. No se ha creado nada.", embed=None, view=None
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
    await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)
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

        active = store.start(
            guild.id,
            channel_id=channel.id,
            topic=console.topic,
            description=console.description,
            opened_by=interaction.user.id,
            excluded=excluded,
        )
        try:
            header = await channel.send(
                embed=_header_embed(guild, store, active), view=CouncilControlView()
            )
        except discord.HTTPException:
            header = None
        if header is not None:
            store.set_header(guild.id, header.id)
            with suppress(discord.HTTPException):
                await header.pin(reason="Mensaje principal del consejo")

    console.stop()
    embed = discord.Embed(
        title="✅ Consejo abierto",
        description=(
            f"**{_truncate(console.topic, 100)}** ya está en {channel.mention}.\n"
            "De momento solo lo ven los admins. Desde ahí puedes crear polls y, cuando esté "
            "listo, reunir al consejo con 📣 o `.noah council meet`."
        ),
        color=COUNCIL_COLOR,
    )
    await _respond_edit(interaction, content=None, embed=embed, view=None)


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

    lines = [f"📣 {role.mention}, el consejo está reunido."]
    lines.append(f"**Tema:** {_truncate(active.get('topic') or 'Sin tema', 200)}")
    lines.append("Las votaciones son anónimas: solo puedes votar una vez y no se puede cambiar.")
    with suppress(discord.HTTPException):
        await channel.send(
            "\n".join(lines),
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


def _exclusions_embed(guild: discord.Guild, store: CouncilStore, last: str = "") -> discord.Embed:
    excluded = store.excluded_ids(guild.id)
    embed = discord.Embed(
        title="🚫 Exclusiones del consejo",
        description=(
            "Los excluidos no ven el canal aunque tengan el rol del consejo y no pueden votar. "
            "Si ya habían votado en un poll abierto, su voto se anula."
        ),
        color=COUNCIL_COLOR,
    )
    embed.add_field(name="Excluidos", value=_mentions(excluded), inline=False)
    warnings = _admin_warnings(guild, excluded)
    if warnings:
        embed.add_field(name="Avisos", value=_field("\n".join(warnings)), inline=False)
    if last:
        embed.add_field(name="Último cambio", value=_field(last), inline=False)
    embed.set_footer(text=FOOTER)
    return embed


class ExclusionsView(discord.ui.View):
    def __init__(self, guild: discord.Guild, store: CouncilStore, author_id: int) -> None:
        super().__init__(timeout=CONSOLE_TIMEOUT)
        self.guild = guild
        self.store = store
        self.author_id = author_id
        members = [
            member
            for user_id in sorted(store.excluded_ids(guild.id))
            if (member := guild.get_member(user_id)) is not None
        ]
        self.select = discord.ui.UserSelect(
            placeholder="🚫 Quién queda excluido",
            min_values=0,
            max_values=EXCLUDE_LIMIT,
            default_values=members[:EXCLUDE_LIMIT],
        )
        self.select.callback = self._on_select
        self.add_item(self.select)

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
        view = ExclusionsView(self.guild, self.store, self.author_id)
        self.stop()
        await _respond_edit(
            interaction, embed=_exclusions_embed(self.guild, self.store, last), view=view
        )

    @discord.ui.button(label="Hecho", style=discord.ButtonStyle.secondary, row=1)
    async def done(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        _ = button
        self.stop()
        await interaction.response.edit_message(view=None)


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
        parts.append(f"🗳️ Votos anulados en {_plural(len(touched), 'poll abierto', 'polls abiertos')}.")
    if failed:
        parts.append(f"❌ No he podido cambiar los permisos de {_mentions(failed)}.")
    return "\n".join(parts) or "Sin cambios."


async def _open_exclusions(interaction: discord.Interaction) -> bool:
    if not await _check_manager(interaction):
        return False
    guild = interaction.guild
    assert guild is not None
    store = _store(interaction.client)
    await interaction.response.send_message(
        embed=_exclusions_embed(guild, store),
        view=ExclusionsView(guild, store, interaction.user.id),
        ephemeral=True,
    )
    return True


# ---------------------------------------------------------------------------
# Polls


def _bar(count: int, total: int) -> str:
    filled = round(BAR_LENGTH * count / total) if total else 0
    return "█" * filled + "░" * (BAR_LENGTH - filled)


def _option_label(poll: dict[str, Any], index: int) -> str:
    options = poll.get("options") or []
    name = options[index] if 0 <= index < len(options) else "?"
    return f"{OPTION_EMOJIS[index]} **{name}**"


def _result_text(poll: dict[str, Any]) -> str:
    counts = tally(poll)
    total = sum(counts)
    best = winners(poll)
    options = poll.get("options") or []
    if not best:
        return "Nadie ha votado."
    if len(best) == 1:
        index = best[0]
        return f"🏆 **{options[index]}** ({counts[index]}/{total})"
    names = ", ".join(f"**{options[index]}**" for index in best)
    return f"🤝 Empate entre {names} ({_plural(counts[best[0]], 'voto', 'votos')} cada una)"


def _poll_embed(
    guild: discord.Guild, store: CouncilStore, active: dict[str, Any], poll: dict[str, Any]
) -> discord.Embed:
    closed = bool(poll.get("closed"))
    counts = tally(poll)
    total = sum(counts)
    show = closed or bool(poll.get("live"))
    best = set(winners(poll)) if closed else set()

    lines = []
    for index in range(len(counts)):
        line = _option_label(poll, index) + (" 🏆" if index in best else "")
        if show:
            percent = round(counts[index] * 100 / total) if total else 0
            line += (
                f"\n`{_bar(counts[index], total)}` **{percent}%** · "
                f"{_plural(counts[index], 'voto', 'votos')}"
            )
        lines.append(line)

    embed = discord.Embed(
        title=f"📊 {_truncate(poll.get('question') or 'Votación', 250)}",
        description="\n\n".join(lines),
        color=CLOSED_COLOR if closed else COUNCIL_COLOR,
    )
    eligible = len(_voters(guild, store, active))
    if closed or eligible < total:
        participation = f"🗳️ **{_plural(total, 'voto', 'votos')}**"
    else:
        participation = f"🗳️ **{total}/{eligible}** han votado"
    if closed:
        status = "🔒 Votación cerrada"
    elif poll.get("live"):
        status = "📈 Resultados en directo"
    else:
        status = "🙈 Resultados ocultos hasta cerrar"
    embed.add_field(name="Participación", value=participation, inline=True)
    embed.add_field(name="Estado", value=status, inline=True)
    if closed:
        embed.add_field(name="Resultado", value=_result_text(poll), inline=False)
    embed.set_footer(
        text=f"{FOOTER} · Voto anónimo · 1 voto por persona, no se puede cambiar"
    )
    return embed


class PollButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"noah:council:(?P<action>vote|votes|close):(?P<poll_id>[0-9a-f]+)(?::(?P<index>\d))?",
):
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
        row: int | None = None,
    ) -> None:
        custom_id = f"noah:council:{action}:{poll_id}"
        if index is not None:
            custom_id += f":{index}"
        super().__init__(
            discord.ui.Button(
                label=label, emoji=emoji, style=style, disabled=disabled, custom_id=custom_id
            ),
            row=row,
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


def _poll_view(poll: dict[str, Any]) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    closed = bool(poll.get("closed"))
    for index, option in enumerate(poll.get("options") or []):
        view.add_item(
            PollButton(
                "vote",
                poll["id"],
                index,
                label=_truncate(option, 80),
                emoji=OPTION_EMOJIS[index],
                style=discord.ButtonStyle.secondary,
                disabled=closed,
                row=0,
            )
        )
    view.add_item(PollButton("votes", poll["id"], label="Votos", emoji="🔍", row=1))
    if not closed:
        view.add_item(PollButton("close", poll["id"], label="Cerrar", emoji="🔒", row=1))
    return view


async def _refresh_poll(guild: discord.Guild, store: CouncilStore, poll_id: str) -> None:
    active = store.get_active(guild.id)
    poll = store.get_poll(guild.id, poll_id)
    channel = _council_channel(guild, active)
    if active is None or poll is None or channel is None or not poll.get("message_id"):
        return
    with suppress(discord.HTTPException):
        await channel.get_partial_message(int(poll["message_id"])).edit(
            embed=_poll_embed(guild, store, active, poll), view=_poll_view(poll)
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
        message = await channel.send(
            embed=_poll_embed(guild, store, active, poll), view=_poll_view(poll)
        )
    except discord.HTTPException:
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
            embed=_votes_embed(guild, store, active, poll),
            view=PollAdminView(guild, store, poll_id),
            ephemeral=True,
        )
    elif action == "close":
        await interaction.response.defer(ephemeral=True, thinking=True)
        closed = await _close_poll(guild, store, poll_id)
        await interaction.followup.send(
            "🔒 Poll cerrado." if closed else "ℹ️ Este poll ya estaba cerrado.", ephemeral=True
        )


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
    poll = store.get_poll(guild.id, poll_id) or {}

    if status == VOTE_ALREADY and option is not None:
        text = (
            f"ℹ️ Ya votaste {_option_label(poll, option)}.\n"
            "Solo se puede votar una vez y el voto no se puede cambiar."
        )
    elif status == VOTE_CLOSED:
        text = "🔒 Esta votación ya está cerrada."
    elif status != VOTE_OK or option is None:
        text = "❌ Esta opción ya no existe."
    else:
        text = (
            f"✅ Has votado {_option_label(poll, option)}.\n"
            "Tu voto es anónimo: el resto del consejo no sabrá qué has votado "
            "(solo los admins pueden verlo)."
        )
    await interaction.response.send_message(text, ephemeral=True)
    if status == VOTE_OK:
        _request_poll_refresh(guild, store, poll_id)


def _votes_embed(
    guild: discord.Guild, store: CouncilStore, active: dict[str, Any], poll: dict[str, Any]
) -> discord.Embed:
    votes = poll_votes(poll)
    options = poll.get("options") or []
    embed = discord.Embed(
        title=f"🔍 Votos · {_truncate(poll.get('question') or 'Votación', 230)}",
        description="Solo lo ves tú. El resto del consejo nunca ve quién votó qué.",
        color=CLOSED_COLOR if poll.get("closed") else COUNCIL_COLOR,
    )
    for index, option in enumerate(options):
        voters = [user_id for user_id, choice in votes.items() if choice == index]
        embed.add_field(
            name=_truncate(f"{OPTION_EMOJIS[index]} {option} — {len(voters)}", 256),
            value=_mentions(voters),
            inline=False,
        )
    if not poll.get("closed"):
        pending = [member.id for member in _voters(guild, store, active) if member.id not in votes]
        embed.add_field(name="⏳ Sin votar", value=_mentions(pending), inline=False)
        mode = "📈 en directo" if poll.get("live") else "🙈 ocultos hasta cerrar"
        embed.set_footer(text=f"{FOOTER} · Resultados {mode}")
    else:
        embed.set_footer(text=f"{FOOTER} · Votación cerrada")
    return embed


class PollAdminView(discord.ui.View):
    """Panel privado de un poll para admins: quién votó qué y modo de resultados."""

    def __init__(self, guild: discord.Guild, store: CouncilStore, poll_id: str) -> None:
        super().__init__(timeout=CONSOLE_TIMEOUT)
        self.guild = guild
        self.store = store
        self.poll_id = poll_id
        poll = store.get_poll(guild.id, poll_id) or {}
        closed = bool(poll.get("closed"))
        self.toggle.label = "Ocultar resultados" if poll.get("live") else "Mostrar resultados"
        self.toggle.emoji = "🙈" if poll.get("live") else "📈"
        self.toggle.disabled = closed
        self.close.disabled = closed

    async def _redraw(self, interaction: discord.Interaction) -> None:
        active = self.store.get_active(self.guild.id)
        poll = self.store.get_poll(self.guild.id, self.poll_id)
        if active is None or poll is None:
            await _respond_edit(interaction, content="❌ Este poll ya no existe.", embed=None, view=None)
            return
        view = PollAdminView(self.guild, self.store, self.poll_id)
        self.stop()
        await _respond_edit(
            interaction, embed=_votes_embed(self.guild, self.store, active, poll), view=view
        )

    @discord.ui.button(label="Actualizar", emoji="🔄", style=discord.ButtonStyle.secondary)
    async def update(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        _ = button
        await self._redraw(interaction)

    @discord.ui.button(label="Mostrar resultados", style=discord.ButtonStyle.secondary)
    async def toggle(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        _ = button
        if not await _check_manager(interaction):
            return
        poll = self.store.get_poll(self.guild.id, self.poll_id)
        if poll is not None and not poll.get("closed"):
            self.store.set_poll_live(self.guild.id, self.poll_id, not poll.get("live"))
            await interaction.response.defer()
            await _refresh_poll(self.guild, self.store, self.poll_id)
        await self._redraw(interaction)

    @discord.ui.button(label="Cerrar poll", emoji="🔒", style=discord.ButtonStyle.danger)
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        _ = button
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
                f"🔒 Votación cerrada · **{_truncate(poll.get('question') or '', 150)}**\n"
                f"{_result_text(poll)}",
                reference=reference,
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
    return True


# ---------------------------------------------------------------------------
# Finalizar: acta + borrar el canal


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


def _acta_embed(
    guild: discord.Guild,
    store: CouncilStore,
    active: dict[str, Any],
    *,
    participants: set[int],
    actor: discord.abc.User | None,
    ended_at: datetime,
) -> discord.Embed:
    embed = discord.Embed(
        title=f"📜 Acta del consejo · {_truncate(active.get('topic') or 'Sin tema', 200)}",
        description=_field(active.get("description") or "*Sin descripción.*", 1500),
        color=COUNCIL_COLOR,
        timestamp=ended_at,
    )
    opened = parse_datetime(active.get("opened_at"))
    met = parse_datetime(active.get("met_at"))
    embed.add_field(
        name="Abierto por",
        value=f"<@{active.get('opened_by')}>" + (f"\n{_ts(opened, 'f')}" if opened else ""),
        inline=True,
    )
    embed.add_field(
        name="Reunido", value=_ts(met, "f") if met else "No se llegó a reunir", inline=True
    )
    embed.add_field(
        name="Finalizado por",
        value=actor.mention if actor is not None else "Canal borrado a mano",
        inline=True,
    )
    if opened is not None:
        embed.add_field(name="Duración", value=_format_span(ended_at - opened), inline=True)
    embed.add_field(name="Escribieron", value=_mentions(sorted(participants)), inline=False)
    embed.add_field(
        name="🚫 Excluidos", value=_mentions(sorted(store.excluded_ids(guild.id))), inline=False
    )
    polls = store.list_polls(guild.id)
    poll_lines = [
        f"📊 **{_truncate(poll.get('question') or '', 90)}** → {_result_text(poll)}"
        for poll in polls
    ]
    embed.add_field(
        name="Votaciones", value=_field("\n".join(poll_lines) or "Sin votaciones"), inline=False
    )
    embed.set_footer(
        text=f"{FOOTER} · La conversación y quién votó qué van en el archivo adjunto."
    )
    return embed


async def _deliver_acta(
    guild: discord.Guild,
    store: CouncilStore,
    actor: discord.abc.User | None,
    embed: discord.Embed,
    text: str,
    ended_at: datetime,
) -> str | None:
    """Publica el acta en el canal de actas o, si no se puede, por MD a quien finaliza."""
    data = text.encode("utf-8")
    filename = f"acta-consejo-{_local(ended_at):%Y%m%d-%H%M}.txt"

    log_channel = _log_channel(guild, store)
    if log_channel is not None:
        try:
            await log_channel.send(embed=embed, file=discord.File(io.BytesIO(data), filename))
            return log_channel.mention
        except discord.HTTPException:
            _log.warning("No se pudo publicar el acta del consejo en %s", log_channel.id)
    if actor is not None:
        try:
            await actor.send(embed=embed, file=discord.File(io.BytesIO(data), filename))
            return "tus mensajes privados"
        except discord.HTTPException:
            pass
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
        embed = _acta_embed(
            guild, store, active, participants=participants, actor=actor, ended_at=ended_at
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
        delivered_to = await _deliver_acta(guild, store, actor, embed, text, ended_at)
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
        embed = _acta_embed(
            guild, store, active, participants=set(), actor=None, ended_at=ended_at
        )
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
        await _deliver_acta(guild, store, None, embed, text, ended_at)
        store.end(guild.id)
        if store.get_channel_id(guild.id) == active.get("channel_id"):
            store.set_channel(guild.id, None)


class FinishConfirmView(discord.ui.View):
    def __init__(self, guild: discord.Guild, store: CouncilStore) -> None:
        super().__init__(timeout=PROMPT_TIMEOUT * 2)
        self.guild = guild
        self.store = store

    @discord.ui.button(label="Finalizar y borrar", emoji="🏁", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        _ = button
        if not await _check_manager(interaction):
            return
        self.stop()
        await interaction.response.edit_message(
            content="⏳ Cerrando los polls y generando el acta…", embed=None, view=None
        )
        result = await _finish_council(self.guild, self.store, interaction.user)
        with suppress(discord.HTTPException):
            await interaction.edit_original_response(content=result)

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        _ = button
        self.stop()
        await interaction.response.edit_message(
            content="El consejo sigue abierto.", embed=None, view=None
        )


async def _open_finish(interaction: discord.Interaction) -> bool:
    if not await _check_manager(interaction):
        return False
    guild = interaction.guild
    assert guild is not None
    store = _store(interaction.client)
    active = store.get_active(guild.id) or {}
    log_channel = _log_channel(guild, store)
    destination = log_channel.mention if log_channel is not None else "tus MD (no hay canal de actas)"
    open_polls = sum(1 for poll in store.list_polls(guild.id) if not poll.get("closed"))
    embed = discord.Embed(
        title="🏁 Finalizar consejo",
        description=(
            f"**{_truncate(active.get('topic') or 'Sin tema', 100)}**\n\n"
            f"• Se cerrarán {_plural(open_polls, 'poll abierto', 'polls abiertos')}.\n"
            f"• El acta (con la conversación y quién votó qué en un .txt) irá a {destination}.\n"
            "• Se borrarán **todos los mensajes** del canal y volverá a quedar oculto "
            "(solo admins) hasta el próximo consejo.\n\n"
            "**No se puede deshacer.**"
        ),
        color=DANGER_COLOR,
    )
    await interaction.response.send_message(
        embed=embed, view=FinishConfirmView(guild, store), ephemeral=True
    )
    return True


# ---------------------------------------------------------------------------
# Configuración (solo admins)


def _config_embed(guild: discord.Guild, store: CouncilStore) -> discord.Embed:
    role = _council_role(guild, store)
    category = _category(guild, store)
    log_channel = _log_channel(guild, store)
    fixed = _fixed_channel(guild, store)
    active = store.get_active(guild.id)
    channel = _council_channel(guild, active)

    embed = discord.Embed(
        title="Configuración del consejo",
        description=(
            "El canal del consejo existe siempre, pero entre consejos está vacío y solo lo ven "
            "los admins. Con `.noah council meet` entra el rol del consejo y con "
            "`.noah council finish` el acta va al canal de actas y se borran todos los mensajes."
        ),
        color=COUNCIL_COLOR,
    )
    embed.add_field(
        name="Rol del consejo", value=role.mention if role is not None else "Sin configurar", inline=True
    )
    embed.add_field(
        name="Canal del consejo",
        value=fixed.mention if fixed is not None else "Se crea al abrir el primer consejo",
        inline=True,
    )
    embed.add_field(
        name="Categoría (si hay que crearlo)",
        value=category.name if category is not None else "Sin categoría",
        inline=True,
    )
    embed.add_field(
        name="Canal de actas",
        value=log_channel.mention if log_channel is not None else "Sin canal (va por MD)",
        inline=True,
    )
    if active is not None and channel is not None and active.get("header_message_id"):
        current = f"[{_truncate(active.get('topic') or 'Sin tema', 80)}]({_jump_url(guild.id, channel.id, int(active['header_message_id']))})"
    elif active is not None and channel is not None:
        current = channel.mention
    else:
        current = "Ninguno"
    embed.add_field(name="Consejo abierto", value=current, inline=False)

    problems = []
    missing = _missing_permissions(guild)
    if missing:
        problems.append(f"Me faltan permisos: {', '.join(missing)}.")
    if log_channel is not None:
        permissions = log_channel.permissions_for(guild.me)
        if not (permissions.send_messages and permissions.attach_files and permissions.embed_links):
            problems.append(f"No puedo enviar embeds y archivos en {log_channel.mention}.")
        if log_channel.permissions_for(guild.default_role).view_channel or (
            role is not None and log_channel.permissions_for(role).view_channel
        ):
            problems.append(
                f"{log_channel.mention} lo puede ver gente que no es admin y el acta dice quién "
                "votó qué. Mejor un canal solo para admins."
            )
    if role is not None and not role.mentionable and not guild.me.guild_permissions.mention_everyone:
        problems.append(
            "El rol no es mencionable: el aviso de `.noah council meet` no les notificará."
        )
    if problems:
        embed.add_field(name="⚠️ Avisos", value=_field("\n".join(problems)), inline=False)
    embed.set_footer(text=FOOTER)
    return embed


class CouncilConfigView(discord.ui.View):
    def __init__(self, guild: discord.Guild, store: CouncilStore, author_id: int) -> None:
        super().__init__(timeout=300)
        self.guild = guild
        self.store = store
        self.author_id = author_id
        self.message: discord.Message | None = None

        role = _council_role(guild, store)
        self.role_select = discord.ui.RoleSelect(
            placeholder="Rol del consejo",
            min_values=0,
            max_values=1,
            default_values=[role] if role is not None else [],
            row=0,
        )
        self.role_select.callback = self._on_role
        self.add_item(self.role_select)

        fixed = _fixed_channel(guild, store)
        self.channel_select = discord.ui.ChannelSelect(
            placeholder="Canal del consejo (o créalo con el botón)",
            channel_types=[discord.ChannelType.text],
            min_values=0,
            max_values=1,
            default_values=[fixed] if fixed is not None else [],
            row=1,
        )
        self.channel_select.callback = self._on_channel
        self.add_item(self.channel_select)

        category = _category(guild, store)
        self.category_select = discord.ui.ChannelSelect(
            placeholder="Categoría donde crear el canal (opcional)",
            channel_types=[discord.ChannelType.category],
            min_values=0,
            max_values=1,
            default_values=[category] if category is not None else [],
            row=2,
        )
        self.category_select.callback = self._on_category
        self.add_item(self.category_select)

        log_channel = _log_channel(guild, store)
        self.log_select = discord.ui.ChannelSelect(
            placeholder="Canal de actas (solo admins)",
            channel_types=[discord.ChannelType.text],
            min_values=0,
            max_values=1,
            default_values=[log_channel] if log_channel is not None else [],
            row=3,
        )
        self.log_select.callback = self._on_log
        self.add_item(self.log_select)
        self.create_channel.disabled = fixed is not None

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
        await _respond_edit(interaction, embed=_config_embed(self.guild, self.store), view=view)

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
                    self.guild, self.store, channel, f"Canal del consejo elegido por {interaction.user}"
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

    @discord.ui.button(label="Crear canal", emoji="📌", style=discord.ButtonStyle.primary, row=4)
    async def create_channel(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        _ = button
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

    @discord.ui.button(label="Cerrar", style=discord.ButtonStyle.secondary, row=4)
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
        title="Consejo",
        description=(
            f"Canal oculto (**{CHANNEL_NAME}**) para que el consejo decida en privado. Existe "
            "siempre, pero entre consejos está vacío y solo lo ven los admins; cuando el "
            "consejo está listo se deja entrar al rol del consejo. Las votaciones son anónimas "
            "y al terminar queda un acta para los admins."
        ),
        color=COUNCIL_COLOR,
    )
    embed.add_field(
        name=".noah council init",
        value=(
            "Abre una consola privada: tema, descripción y a quién excluir (implicados que no "
            "deben ver el canal aunque estén en el consejo)."
        ),
        inline=False,
    )
    embed.add_field(
        name=".noah council poll",
        value=(
            "Votación de 2 a 4 opciones: un voto por persona, anónimo y sin cambios. Eliges si "
            "los resultados se ven en directo u ocultos hasta cerrar. Con 🔍 **Votos** los "
            "admins ven quién votó qué."
        ),
        inline=False,
    )
    embed.add_field(
        name=".noah council meet",
        value="Deja entrar al rol del consejo y le avisa con una mención.",
        inline=False,
    )
    embed.add_field(
        name=".noah council exclude",
        value="Excluye o readmite gente en cualquier momento (sus votos en polls abiertos se anulan).",
        inline=False,
    )
    embed.add_field(
        name=".noah council finish",
        value=(
            "Cierra los polls, envía el acta (con la conversación en un .txt), borra todos los "
            "mensajes y vuelve a ocultar el canal."
        ),
        inline=False,
    )
    embed.add_field(
        name=".noah council config",
        value="Rol del consejo, canal del consejo (elegir o crear) y canal de actas.",
        inline=False,
    )
    embed.set_footer(
        text=f"{FOOTER} · Todo es solo para admins. El mensaje fijado del canal trae botones para lo mismo."
    )
    return embed


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
        bot.add_view(CouncilControlView())
        bot.add_dynamic_items(PollButton)

        store = _store(bot)
        for guild_id in store.guild_ids():
            guild = bot.get_guild(guild_id)
            active = store.get_active(guild_id)
            if guild is not None and active is not None and _council_channel(guild, active) is None:
                await _handle_lost_channel(guild, store)

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
        await ctx.send(embed=_build_help_embed())

    @council.command(name="help")
    async def council_help(ctx: commands.Context) -> None:
        await ctx.send(embed=_build_help_embed())

    @council.command(name="config")
    async def council_config(ctx: commands.Context) -> None:
        if ctx.guild is None:
            await ctx.send("❌ Este comando solo funciona dentro de un servidor.")
            return
        if not _is_admin(ctx.author):
            await ctx.send("❌ Solo los administradores pueden usar este comando.")
            return
        store = _store(ctx.bot)
        view = CouncilConfigView(ctx.guild, store, ctx.author.id)
        view.message = await ctx.send(embed=_config_embed(ctx.guild, store), view=view)

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
            await _notice(
                ctx, "❌ Falta elegir el rol del consejo. Usa `.noah council config`."
            )
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
