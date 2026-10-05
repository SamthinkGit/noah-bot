import asyncio
import io
import logging
import math
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

import discord
from discord.ext import commands

from noah_bot.modules.bot_context import get_bot_context
from noah_bot.modules.guard import (
    MASS_MENTION_PATTERN,
    OVERWRITE_MEMBER,
    OverwriteChange,
    account_age_days,
    plan_channel_lockdown,
    plan_restore,
    score_suspect,
)


_log = logging.getLogger("discord.ext.commands.bot")

CONTINGENCY_TEXT_NAME = "canal-de-contingencia"
CONTINGENCY_VOICE_NAME = "vc-de-contingencia"
NOTICE_EVERY_MESSAGES = 50
BLOCKED_REPLY_COOLDOWN_SECONDS = 60
CHANNEL_CONCURRENCY = 4
PROGRESS_EDIT_INTERVAL_SECONDS = 3.0
DEFAULT_SUSPECT_SCORE = 5
LIST_PREVIEW_LIMIT = 15
FIELD_LIMIT = 1024
SELECT_LIMIT = 25

COLOR_ALERT = discord.Color.from_rgb(170, 32, 38)
COLOR_DRILL = discord.Color.from_rgb(201, 125, 22)
COLOR_INFO = discord.Color.from_rgb(54, 57, 63)
COLOR_OK = discord.Color.from_rgb(46, 125, 50)
FOOTER = "Noah Guard"

REQUIRED_PERMISSIONS = (
    ("manage_channels", "Gestionar canales"),
    ("manage_roles", "Gestionar permisos"),
)
RECOMMENDED_PERMISSIONS = (
    ("administrator", "Administrador (recomendado para editar cualquier canal)"),
    ("ban_members", "Banear miembros"),
    ("kick_members", "Expulsar miembros"),
    ("move_members", "Mover miembros"),
    ("manage_messages", "Gestionar mensajes (fijar el aviso)"),
)


class GuardCommandBlocked(commands.CheckFailure):
    """El protocolo de contingencia bloquea los comandos de usuarios normales."""


# ---------------------------------------------------------------------------
# Estado en memoria
# ---------------------------------------------------------------------------

_guild_locks: dict[int, asyncio.Lock] = {}
_drill_tasks: dict[int, asyncio.Task] = {}
_blocked_replies: dict[tuple[int, int], float] = {}
_mass_mention_handled: dict[int, set[int]] = {}


def _lock_for(guild_id: int) -> asyncio.Lock:
    return _guild_locks.setdefault(guild_id, asyncio.Lock())


# ---------------------------------------------------------------------------
# Staff y exenciones
# ---------------------------------------------------------------------------


def is_guard_staff(member: discord.abc.User, config: dict[str, Any]) -> bool:
    if not isinstance(member, discord.Member):
        return False
    if member.id == member.guild.owner_id or member.guild_permissions.administrator:
        return True
    if member.id in config["users"]:
        return True
    role_ids = set(config["roles"])
    return any(role.id in role_ids for role in member.roles)


@dataclass(slots=True)
class ExemptSets:
    members: list[discord.Member]
    member_ids: set[int]
    roles: list[discord.Role]
    bot_ids: list[int]
    admins: int
    staff_users: int
    staff_roles: int


def _collect_exempt(guild: discord.Guild, config: dict[str, Any]) -> ExemptSets:
    bot_ids = {member.id for member in guild.members if member.bot}
    config_role_ids = set(config["roles"])
    roles = [
        role
        for role in guild.roles
        if not role.is_default()
        and (
            role.id in config_role_ids
            or (role.tags is not None and role.tags.bot_id in bot_ids)
        )
    ]

    members: list[discord.Member] = []
    member_ids: set[int] = set()
    admins = 0
    for member in guild.members:
        if member.id not in bot_ids and not is_guard_staff(member, config):
            continue
        member_ids.add(member.id)
        if member.id == guild.owner_id or member.guild_permissions.administrator:
            admins += 0 if member.bot else 1
            continue
        members.append(member)

    return ExemptSets(
        members=members,
        member_ids=member_ids,
        roles=roles,
        bot_ids=sorted(bot_ids),
        admins=admins,
        staff_users=len([uid for uid in config["users"] if guild.get_member(uid)]),
        staff_roles=len([rid for rid in config_role_ids if guild.get_role(rid)]),
    )


def _is_exempt_author(
    bot: commands.Bot,
    message: discord.Message,
    config: dict[str, Any],
    session: dict[str, Any],
) -> bool:
    if message.webhook_id is not None:
        return True
    if bot.user is not None and message.author.id == bot.user.id:
        return True
    if message.author.id in session.get("exempt_bot_ids", []):
        return True
    return is_guard_staff(message.author, config)


# ---------------------------------------------------------------------------
# Formato
# ---------------------------------------------------------------------------


def _ts(value: float, style: str = "R") -> str:
    return f"<t:{int(value)}:{style}>"


def _format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours} h {minutes:02d} min"
    if minutes:
        return f"{minutes} min {secs:02d} s"
    return f"{secs} s"


def _format_age(days: float) -> str:
    if days < 1:
        hours = int(days * 24)
        return "menos de una hora" if hours < 1 else f"{hours} h"
    if days < 60:
        return f"{int(days)} días"
    if days < 730:
        return f"{int(days // 30)} meses"
    return f"{int(days // 365)} años"


def _user_label(guild: discord.Guild, user_id: int, name: str | None = None) -> str:
    if guild.get_member(user_id) is not None:
        return f"<@{user_id}>"
    return f"{name or 'Usuario desconocido'} (`{user_id}`)"


def _clip_lines(lines: list[str], limit: int = FIELD_LIMIT) -> str:
    if not lines:
        return "Sin datos."

    output: list[str] = []
    length = 0
    for index, line in enumerate(lines):
        more = f"y {len(lines) - index} más."
        if length + len(line) + 1 + len(more) > limit:
            output.append(more)
            break
        output.append(line)
        length += len(line) + 1
    return "\n".join(output)


def _base_embed(title: str, description: str | None, color: discord.Color) -> discord.Embed:
    embed = discord.Embed(title=title, description=description, color=color)
    embed.set_footer(text=FOOTER)
    return embed


def _error_embed(description: str) -> discord.Embed:
    return _base_embed("Noah Guard", description, COLOR_ALERT)


def _text_file(name: str, lines: list[str]) -> discord.File:
    return discord.File(io.BytesIO("\n".join(lines).encode("utf-8")), filename=name)


def _build_help_embed() -> discord.Embed:
    embed = _base_embed(
        "Noah Guard",
        (
            "Protocolo de contingencia ante raids. Oculta todos los canales para los "
            "usuarios, habilita un canal de texto y uno de voz temporales, restringe los "
            "comandos de Noah y registra la actividad. Al finalizar, el servidor vuelve "
            "a su estado anterior.\n\nSolo para administración y usuarios autorizados."
        ),
        COLOR_INFO,
    )
    embed.add_field(
        name="Control",
        value=(
            "`.noah guard start` Activa el protocolo de contingencia.\n"
            "`.noah guard stop` Lo desactiva y restaura el servidor.\n"
            "`.noah guard status` Muestra el estado actual.\n"
            "`.noah guard retry` Reintenta restaurar permisos pendientes."
        ),
        inline=False,
    )
    embed.add_field(
        name="Simulacro",
        value=(
            "`.noah guard drill` Ensayo sin cambios: qué se tocaría y qué falta.\n"
            "`.noah guard drill live` Simulacro real, sin baneos y con parada automática."
        ),
        inline=False,
    )
    embed.add_field(
        name="Estadísticas",
        value=(
            "`.noah guard stats` Resumen y usuarios más activos.\n"
            "`.noah guard joins` Entradas al servidor durante la contingencia.\n"
            "`.noah guard user @usuario` Detalle de un usuario.\n"
            "`.noah guard suspects` Usuarios ordenados por puntuación de sospecha."
        ),
        inline=False,
    )
    embed.add_field(
        name="Acciones en bloque",
        value=(
            "`.noah guard ban joins` Banea a quienes entraron durante la contingencia.\n"
            "`.noah guard ban suspects [puntos]` Banea a los sospechosos "
            f"(por defecto, {DEFAULT_SUSPECT_SCORE} puntos o más).\n"
            "`.noah guard kick joins` y `.noah guard kick suspects [puntos]` "
            "hacen lo mismo expulsando.\n"
            "Todas piden confirmación antes de ejecutarse."
        ),
        inline=False,
    )
    embed.add_field(
        name="Configuración",
        value=(
            "`.noah guard config` Abre el panel de configuración.\n"
            "`.noah guard config show` Muestra la configuración.\n"
            "`.noah guard config adduser @usuario` y `removeuser`\n"
            "`.noah guard config addrole @rol` y `removerole`\n"
            "`.noah guard config newdays <días>` Antigüedad de cuenta nueva.\n"
            "`.noah guard config drillminutes <min>` Duración del simulacro."
        ),
        inline=False,
    )
    return embed


def _build_notice_embed(session: dict[str, Any], *, reminder: bool = False) -> discord.Embed:
    drill = session.get("drill") is True
    starter = f"<@{session['started_by']}>"

    if drill:
        title = "Simulacro de contingencia"
        color = COLOR_DRILL
        description = (
            "Se está realizando un **simulacro** del protocolo de seguridad del servidor. "
            "Durante unos minutos los canales habituales permanecerán ocultos y este será "
            "el único canal de texto disponible. No es necesario que hagas nada."
        )
        rule = (
            "Cualquier usuario que utilice `@everyone` o `@here` será **baneado "
            "automáticamente**. Durante el simulacro solo se avisará, sin aplicar el baneo."
        )
    else:
        title = "Protocolo de contingencia activado"
        color = COLOR_ALERT
        description = (
            "El servidor ha activado su protocolo de seguridad ante una posible "
            "incidencia. Mientras dure, los canales habituales permanecerán ocultos de "
            "forma temporal y este será el único canal de texto disponible."
        )
        rule = (
            "Cualquier usuario que utilice `@everyone` o `@here` será **baneado "
            "automáticamente**, sin excepciones."
        )

    if reminder:
        title = f"Recordatorio · {title}"

    embed = _base_embed(title, description, color)
    embed.add_field(name="Norma durante la contingencia", value=rule, inline=False)
    embed.add_field(
        name="Canales disponibles",
        value=(
            f"Texto: <#{session.get('text_channel_id')}>\n"
            f"Voz: <#{session.get('voice_channel_id')}>\n"
            "Los comandos de Noah quedan reservados temporalmente a la administración."
        ),
        inline=False,
    )
    embed.add_field(
        name="Responsable",
        value=(
            f"Contingencia iniciada por {starter} {_ts(session['started_at'])}.\n"
            "Estamos trabajando para resolverlo lo antes posible. Si tienes cualquier "
            f"duda, puedes contactar con {starter} sin problema."
        ),
        inline=False,
    )
    if drill and session.get("drill_ends_at"):
        embed.add_field(
            name="Duración",
            value=f"El simulacro finalizará automáticamente {_ts(session['drill_ends_at'])}.",
            inline=False,
        )
    embed.set_footer(text=f"{FOOTER} · Gracias por tu paciencia")
    return embed


def _build_config_embed(guild: discord.Guild, config: dict[str, Any]) -> discord.Embed:
    embed = _base_embed(
        "Noah Guard · Configuración",
        (
            "Los administradores tienen acceso siempre. Los usuarios y roles autorizados "
            "pueden gestionar Noah Guard y conservan su acceso durante la contingencia."
        ),
        COLOR_INFO,
    )
    users = [_user_label(guild, user_id) for user_id in config["users"]]
    roles = [
        role.mention
        for role_id in config["roles"]
        if (role := guild.get_role(role_id)) is not None
    ]
    embed.add_field(
        name=f"Usuarios autorizados ({len(users)})",
        value=_clip_lines(users) if users else "Ninguno.",
        inline=False,
    )
    embed.add_field(
        name=f"Roles autorizados ({len(roles)})",
        value=_clip_lines(roles) if roles else "Ninguno.",
        inline=False,
    )
    embed.add_field(
        name="Parámetros",
        value=(
            f"Cuenta nueva: creada hace menos de **{config['new_account_days']}** días\n"
            f"Duración del simulacro: **{config['drill_minutes']}** minutos"
        ),
        inline=False,
    )
    return embed


# ---------------------------------------------------------------------------
# Progreso
# ---------------------------------------------------------------------------


class _Progress:
    def __init__(
        self,
        message: discord.Message,
        title: str,
        color: discord.Color,
        label: str,
        total: int,
    ) -> None:
        self.message = message
        self.title = title
        self.color = color
        self.label = label
        self.total = total
        self.done = 0
        self._last_edit = 0.0

    async def tick(self) -> None:
        self.done += 1
        if time.monotonic() - self._last_edit < PROGRESS_EDIT_INTERVAL_SECONDS:
            return
        self._last_edit = time.monotonic()
        with suppress(discord.HTTPException):
            await self.message.edit(embed=self.render())

    def render(self) -> discord.Embed:
        return _base_embed(
            self.title,
            f"{self.label}: **{self.done}** de **{self.total}**",
            self.color,
        )


# ---------------------------------------------------------------------------
# Activación
# ---------------------------------------------------------------------------


async def _edit_overwrite(
    bot: commands.Bot,
    change: OverwriteChange,
    pair: tuple[int, int],
    reason: str,
) -> None:
    await bot.http.edit_channel_permissions(
        change.channel_id,
        change.target_id,
        str(pair[0]),
        str(pair[1]),
        change.target_type,
        reason=reason,
    )


async def _create_temp_channels(
    guild: discord.Guild,
    reason: str,
) -> tuple[discord.TextChannel, discord.VoiceChannel]:
    me = guild.me
    my_perms = me.guild_permissions

    everyone_text = discord.PermissionOverwrite(
        view_channel=True,
        send_messages=True,
        read_message_history=True,
    )
    if my_perms.administrator or my_perms.mention_everyone:
        everyone_text.mention_everyone = False

    own_text = discord.PermissionOverwrite(
        view_channel=True,
        send_messages=True,
        embed_links=True,
        read_message_history=True,
    )
    if my_perms.administrator or my_perms.manage_messages:
        own_text.manage_messages = True

    text_channel = await guild.create_text_channel(
        CONTINGENCY_TEXT_NAME,
        overwrites={guild.default_role: everyone_text, me: own_text},
        position=0,
        topic="Canal temporal del protocolo de contingencia. Se eliminará al finalizar.",
        reason=reason,
    )

    own_voice = discord.PermissionOverwrite(view_channel=True, connect=True)
    if my_perms.administrator or my_perms.move_members:
        own_voice.move_members = True

    try:
        voice_channel = await guild.create_voice_channel(
            CONTINGENCY_VOICE_NAME,
            overwrites={
                guild.default_role: discord.PermissionOverwrite(
                    view_channel=True,
                    connect=True,
                    speak=True,
                ),
                me: own_voice,
            },
            position=0,
            reason=reason,
        )
    except discord.HTTPException:
        with suppress(discord.HTTPException):
            await text_channel.delete(reason=reason)
        raise

    return text_channel, voice_channel


async def _apply_lockdown(
    bot: commands.Bot,
    guild: discord.Guild,
    plans: dict[int, list[OverwriteChange]],
    progress: _Progress,
    reason: str,
) -> tuple[int, list[int]]:
    store = get_bot_context(bot).guard
    semaphore = asyncio.Semaphore(CHANNEL_CONCURRENCY)
    applied_total = 0
    failed_channels: list[int] = []

    async def _lock_channel(channel_id: int, changes: list[OverwriteChange]) -> None:
        nonlocal applied_total
        applied: list[OverwriteChange] = []
        async with semaphore:
            try:
                for change in changes:
                    await _edit_overwrite(bot, change, change.after, reason)
                    applied.append(change)
            except discord.HTTPException:
                failed_channels.append(channel_id)
                store.record_failed_channel(guild.id, channel_id)
            finally:
                store.record_changes(guild.id, applied)
                applied_total += len(applied)
        await progress.tick()

    await asyncio.gather(
        *(_lock_channel(channel_id, changes) for channel_id, changes in plans.items())
    )
    return applied_total, failed_channels


async def _move_voice_members(
    bot: commands.Bot,
    guild: discord.Guild,
    exempt_ids: set[int],
    target: discord.VoiceChannel,
    reason: str,
) -> int:
    store = get_bot_context(bot).guard
    moved = 0
    for channel in [*guild.voice_channels, *guild.stage_channels]:
        if channel.id == target.id:
            continue
        for member in list(channel.members):
            if member.id in exempt_ids:
                continue
            try:
                await member.move_to(target, reason=reason)
            except discord.HTTPException:
                continue
            store.record_moved_member(guild.id, member.id, channel.id)
            moved += 1
    return moved


async def _activate(ctx: commands.Context, *, drill: bool) -> None:
    bot = ctx.bot
    guild = ctx.guild
    store = get_bot_context(bot).guard
    lock = _lock_for(guild.id)

    if lock.locked():
        await ctx.send(embed=_error_embed("Hay otra operación de Noah Guard en curso."))
        return

    async with lock:
        if store.is_active(guild.id):
            await ctx.send(
                embed=_error_embed(
                    "El protocolo de contingencia ya está activo. Usa `.noah guard stop` "
                    "para finalizarlo."
                )
            )
            return

        my_perms = guild.me.guild_permissions
        missing = [
            label
            for perm, label in REQUIRED_PERMISSIONS
            if not (my_perms.administrator or getattr(my_perms, perm))
        ]
        if missing:
            await ctx.send(
                embed=_error_embed(
                    "No puedo activar el protocolo porque me faltan permisos: "
                    + ", ".join(missing)
                    + "."
                )
            )
            return

        if not guild.chunked:
            await guild.chunk()

        config = store.get_config(guild.id)
        exempt = _collect_exempt(guild, config)
        drill_ends_at = time.time() + config["drill_minutes"] * 60 if drill else None
        title = "Activando simulacro" if drill else "Activando protocolo de contingencia"
        color = COLOR_DRILL if drill else COLOR_ALERT
        reason = (
            f"Noah Guard: {'simulacro' if drill else 'contingencia'} iniciado por {ctx.author}"
        )

        status_message = await ctx.send(
            embed=_base_embed(title, "Preparando canales temporales...", color)
        )

        session = store.begin_session(
            guild.id,
            started_by=ctx.author.id,
            origin_channel_id=ctx.channel.id,
            drill=drill,
            exempt_bot_ids=exempt.bot_ids,
            drill_ends_at=drill_ends_at,
        )
        _mass_mention_handled.pop(guild.id, None)

        try:
            text_channel, voice_channel = await _create_temp_channels(guild, reason)
        except discord.HTTPException as exc:
            store.end_session(guild.id, ctx.author.id)
            await status_message.edit(
                embed=_error_embed(
                    f"No he podido crear los canales temporales ({exc}). "
                    "No se ha modificado nada."
                )
            )
            return

        store.update_session(
            guild.id,
            text_channel_id=text_channel.id,
            voice_channel_id=voice_channel.id,
        )

        try:
            notice = await text_channel.send(
                embed=_build_notice_embed(session),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            with suppress(discord.HTTPException):
                await notice.pin(reason=reason)
        except discord.HTTPException:
            pass

        try:
            plans = {
                channel.id: plan_channel_lockdown(
                    channel,
                    exempt.members,
                    exempt.member_ids,
                    exempt.roles,
                )
                for channel in guild.channels
                if channel.id not in (text_channel.id, voice_channel.id)
            }
            pending = {channel_id: changes for channel_id, changes in plans.items() if changes}
            progress = _Progress(status_message, title, color, "Canales ocultados", len(pending))
            applied, failed_channels = await _apply_lockdown(
                bot,
                guild,
                pending,
                progress,
                reason,
            )
            moved = await _move_voice_members(
                bot,
                guild,
                exempt.member_ids,
                voice_channel,
                reason,
            )
        except Exception:
            _log.exception("Noah Guard: error al activar la contingencia")
            await status_message.edit(
                embed=_error_embed(
                    "Ha ocurrido un error inesperado durante la activación. Los cambios "
                    "aplicados hasta ahora están registrados: usa `.noah guard stop` "
                    "para revertirlos."
                )
            )
            return

        summary = _base_embed(
            "Simulacro en curso" if drill else "Protocolo de contingencia activo",
            (
                "Los canales están ocultos para los usuarios. El staff y los bots que ya "
                "estaban en el servidor conservan su acceso."
            ),
            color,
        )
        summary.add_field(
            name="Canales temporales",
            value=f"Texto: {text_channel.mention}\nVoz: {voice_channel.mention}",
            inline=False,
        )
        summary.add_field(
            name="Cambios aplicados",
            value=(
                f"Canales ocultados: **{len(pending) - len(failed_channels)}** "
                f"de **{len(plans)}** (el resto ya estaba oculto)\n"
                f"Ajustes de permisos: **{applied}**\n"
                f"Usuarios movidos al canal de voz de respaldo: **{moved}**"
            ),
            inline=False,
        )
        if failed_channels:
            names = [
                f"<#{channel_id}>" for channel_id in failed_channels
            ]
            summary.add_field(
                name="Incidencias",
                value=(
                    "No he podido ocultar estos canales; revisa mis permisos en ellos:\n"
                    + _clip_lines(names, FIELD_LIMIT - 80)
                ),
                inline=False,
            )
        if drill and drill_ends_at:
            summary.add_field(
                name="Duración",
                value=(
                    f"Finaliza automáticamente {_ts(drill_ends_at)}. Durante el simulacro "
                    "las menciones masivas solo generan un aviso."
                ),
                inline=False,
            )
        summary.add_field(
            name="Siguientes pasos",
            value=(
                "`.noah guard stats` · `.noah guard joins` · `.noah guard suspects`\n"
                "`.noah guard stop` para finalizar y restaurar el servidor."
            ),
            inline=False,
        )
        with suppress(discord.HTTPException):
            await status_message.edit(embed=summary)

        if drill and drill_ends_at:
            _schedule_drill_stop(bot, guild.id, drill_ends_at)


# ---------------------------------------------------------------------------
# Desactivación
# ---------------------------------------------------------------------------


async def _revert_changes(
    bot: commands.Bot,
    guild: discord.Guild,
    changes: list[OverwriteChange],
    reason: str,
) -> tuple[int, int, list[OverwriteChange]]:
    by_channel: dict[int, list[OverwriteChange]] = {}
    for change in changes:
        by_channel.setdefault(change.channel_id, []).append(change)

    semaphore = asyncio.Semaphore(CHANNEL_CONCURRENCY)
    restored = 0
    kept = 0
    failed: list[OverwriteChange] = []

    async def _restore_channel(channel_id: int, channel_changes: list[OverwriteChange]) -> None:
        nonlocal restored, kept
        channel = guild.get_channel(channel_id)
        if channel is None:
            kept += len(channel_changes)
            return

        async with semaphore:
            for index, change in enumerate(reversed(channel_changes)):
                action, pair = plan_restore(channel, change)
                try:
                    if action == "restore" and pair is not None:
                        await _edit_overwrite(bot, change, pair, reason)
                        restored += 1
                    elif action == "delete":
                        await bot.http.delete_channel_permissions(
                            change.channel_id,
                            change.target_id,
                            reason=reason,
                        )
                        restored += 1
                    else:
                        kept += 1
                except discord.NotFound:
                    kept += 1
                except discord.HTTPException:
                    failed.extend(list(reversed(channel_changes))[index:][::-1])
                    return

    await asyncio.gather(
        *(_restore_channel(channel_id, items) for channel_id, items in by_channel.items())
    )
    return restored, kept, failed


async def _deactivate(
    bot: commands.Bot,
    guild: discord.Guild,
    stopped_by: discord.abc.User | None,
) -> discord.Embed:
    store = get_bot_context(bot).guard
    session = store.get_session(guild.id) or {}
    drill = session.get("drill") is True
    actor = str(stopped_by) if stopped_by else "fin del simulacro"
    reason = f"Noah Guard: {'simulacro' if drill else 'contingencia'} finalizado ({actor})"

    restored, kept, failed = await _revert_changes(
        bot,
        guild,
        store.get_changes(guild.id),
        reason,
    )

    voice_channel = guild.get_channel(session.get("voice_channel_id") or 0)
    returned = 0
    if isinstance(voice_channel, discord.VoiceChannel):
        for member_key, original_id in session.get("moved_members", {}).items():
            member = guild.get_member(int(member_key))
            original = guild.get_channel(int(original_id))
            if (
                member is None
                or original is None
                or member.voice is None
                or member.voice.channel is None
                or member.voice.channel.id != voice_channel.id
            ):
                continue
            with suppress(discord.HTTPException):
                await member.move_to(original, reason=reason)
                returned += 1

    for channel_id in (session.get("text_channel_id"), session.get("voice_channel_id")):
        channel = guild.get_channel(channel_id or 0)
        if channel is not None:
            with suppress(discord.HTTPException):
                await channel.delete(reason=reason)

    store.update_session(
        guild.id,
        pending_changes=[change.to_payload() for change in failed],
    )
    store.end_session(guild.id, stopped_by.id if stopped_by else None)

    session = store.get_session(guild.id) or session
    duration = (session.get("ended_at") or time.time()) - session.get("started_at", time.time())
    users = session.get("users", {})
    actions = session.get("actions", [])

    embed = _base_embed(
        "Simulacro finalizado" if drill else "Protocolo de contingencia finalizado",
        "Los canales temporales se han eliminado y los permisos han vuelto a su estado anterior.",
        COLOR_OK if not failed else COLOR_DRILL,
    )
    embed.add_field(
        name="Resumen",
        value=(
            f"Duración: **{_format_duration(duration)}**\n"
            + (
                f"Finalizado por: {stopped_by.mention}"
                if isinstance(stopped_by, discord.abc.User)
                else "Finalizado automáticamente al terminar el simulacro"
            )
        ),
        inline=False,
    )
    embed.add_field(
        name="Permisos",
        value=(
            f"Ajustes restaurados: **{restored}**\n"
            f"Ajustes conservados por cambios manuales del staff: **{kept}**\n"
            f"Usuarios devueltos a su canal de voz: **{returned}**"
        ),
        inline=False,
    )
    embed.add_field(
        name="Actividad registrada",
        value=(
            f"Mensajes: **{sum(int(entry.get('messages', 0)) for entry in users.values())}**"
            f" de **{len(users)}** usuarios\n"
            f"Entradas al servidor: **{len(session.get('joins', {}))}**\n"
            f"Baneos: **{len([a for a in actions if a['action'] in ('autoban', 'ban')])}**"
            f" · Expulsiones: **{len([a for a in actions if a['action'] == 'kick'])}**\n"
            "Las estadísticas siguen disponibles con `.noah guard stats` hasta la "
            "próxima activación."
        ),
        inline=False,
    )
    if failed:
        embed.add_field(
            name="Pendiente",
            value=(
                f"**{len(failed)}** ajustes de permisos no se han podido restaurar. "
                "Usa `.noah guard retry` para reintentarlo."
            ),
            inline=False,
        )
    return embed


def _schedule_drill_stop(bot: commands.Bot, guild_id: int, ends_at: float) -> None:
    previous = _drill_tasks.pop(guild_id, None)
    if previous is not None and not previous.done():
        previous.cancel()
    _drill_tasks[guild_id] = asyncio.create_task(_drill_timer(bot, guild_id, ends_at))


async def _drill_timer(bot: commands.Bot, guild_id: int, ends_at: float) -> None:
    await asyncio.sleep(max(0.0, ends_at - time.time()))
    _drill_tasks.pop(guild_id, None)

    guild = bot.get_guild(guild_id)
    store = get_bot_context(bot).guard
    if guild is None:
        return

    async with _lock_for(guild_id):
        session = store.get_session(guild_id)
        if session is None or not session.get("active") or not session.get("drill"):
            return
        embed = await _deactivate(bot, guild, None)

    channel = guild.get_channel(session.get("origin_channel_id") or 0)
    if isinstance(channel, discord.abc.Messageable):
        with suppress(discord.HTTPException):
            await channel.send(embed=embed)


def _cancel_drill_timer(guild_id: int) -> None:
    task = _drill_tasks.pop(guild_id, None)
    if task is not None and not task.done():
        task.cancel()


# ---------------------------------------------------------------------------
# Simulacro sin cambios
# ---------------------------------------------------------------------------


async def _dry_run(ctx: commands.Context) -> None:
    guild = ctx.guild
    store = get_bot_context(ctx.bot).guard
    if not guild.chunked:
        await guild.chunk()

    config = store.get_config(guild.id)
    exempt = _collect_exempt(guild, config)
    me = guild.me
    my_perms = me.guild_permissions

    plans = {
        channel.id: plan_channel_lockdown(
            channel,
            exempt.members,
            exempt.member_ids,
            exempt.roles,
        )
        for channel in guild.channels
    }
    all_changes = [change for changes in plans.values() for change in changes]
    affected = [guild.get_channel(cid) for cid, changes in plans.items() if changes]
    by_kind = {kind: 0 for kind in ("everyone", "strip", "grant")}
    for change in all_changes:
        by_kind[change.kind] = by_kind.get(change.kind, 0) + 1

    categories = len([c for c in affected if isinstance(c, discord.CategoryChannel)])
    texts = len([c for c in affected if isinstance(c, discord.TextChannel)])
    voices = len(
        [c for c in affected if isinstance(c, (discord.VoiceChannel, discord.StageChannel))]
    )
    others = len(affected) - categories - texts - voices
    to_move = sum(
        1
        for channel in [*guild.voice_channels, *guild.stage_channels]
        for member in channel.members
        if member.id not in exempt.member_ids
    )
    estimated = max(3, math.ceil(len(all_changes) * 0.5 / CHANNEL_CONCURRENCY) + 2)

    embed = _base_embed(
        "Simulacro · Ensayo sin cambios",
        "Esto es lo que haría `.noah guard start` ahora mismo. No se ha modificado nada.",
        COLOR_DRILL,
    )
    embed.add_field(
        name="Alcance",
        value=(
            f"Canales que se ocultarían: **{len(affected)}** de **{len(plans)}**\n"
            f"Categorías: {categories} · Texto: {texts} · Voz: {voices} · Otros: {others}\n"
            f"Canales que ya están ocultos para los usuarios: **{len(plans) - len(affected)}**"
        ),
        inline=False,
    )
    embed.add_field(
        name="Permisos",
        value=(
            f"Ajustes totales: **{len(all_changes)}**\n"
            f"Ocultar a @everyone: {by_kind['everyone']}\n"
            f"Retirar accesos concedidos a roles o usuarios: {by_kind['strip']}\n"
            f"Conservar el acceso del staff y los bots: {by_kind['grant']}\n"
            f"Tiempo estimado: unos **{_format_duration(estimated)}**"
        ),
        inline=False,
    )
    embed.add_field(
        name="Exentos",
        value=(
            f"Administradores: {exempt.admins}\n"
            f"Usuarios autorizados: {exempt.staff_users}\n"
            f"Roles autorizados: {exempt.staff_roles}\n"
            f"Bots actuales: {len(exempt.bot_ids)}\n"
            f"Usuarios que se moverían al canal de voz de respaldo: {to_move}"
        ),
        inline=False,
    )

    checks = [
        f"{'Correcto' if my_perms.administrator or getattr(my_perms, perm) else 'Falta'}"
        f" · {label}"
        for perm, label in (*REQUIRED_PERMISSIONS, *RECOMMENDED_PERMISSIONS)
    ]
    embed.add_field(name="Permisos de Noah", value="\n".join(checks), inline=False)

    warnings: list[str] = []
    if not my_perms.administrator:
        blocked = [
            channel.mention
            for channel in affected
            if channel is not None and not channel.permissions_for(me).manage_roles
        ]
        if blocked:
            warnings.append(
                f"Sin permiso para editar {len(blocked)} canales: " + ", ".join(blocked[:10])
            )
    above = [
        role.mention
        for role in guild.roles
        if role >= me.top_role
        and not role.is_default()
        and not role.managed
        and role.id not in {r.id for r in exempt.roles}
        and not role.permissions.administrator
    ]
    if above:
        warnings.append(
            "No podría banear ni expulsar a quien tenga estos roles (están por encima "
            "del mío): " + ", ".join(above[:10])
        )
    if warnings:
        embed.add_field(
            name="Avisos",
            value=_clip_lines(warnings),
            inline=False,
        )

    embed.set_footer(
        text=f"{FOOTER} · Usa .noah guard drill live para un simulacro real con parada automática"
    )
    await ctx.send(embed=embed)


# ---------------------------------------------------------------------------
# Estadísticas
# ---------------------------------------------------------------------------


def _session_or_none(ctx: commands.Context) -> dict[str, Any] | None:
    return get_bot_context(ctx.bot).guard.get_session(ctx.guild.id)


def _no_data_embed() -> discord.Embed:
    return _error_embed(
        "No hay datos registrados. Las estadísticas se recogen mientras el protocolo "
        "de contingencia está activo."
    )


def _state_line(session: dict[str, Any]) -> str:
    if session.get("active"):
        state = "Simulacro en curso" if session.get("drill") else "Activo"
    else:
        state = "Finalizado"
    end = session.get("ended_at") or time.time()
    return (
        f"**Estado:** {state}\n"
        f"**Iniciado por:** <@{session['started_by']}> {_ts(session['started_at'], 'f')}\n"
        f"**Duración:** {_format_duration(end - session['started_at'])}"
    )


def _suspect_candidates(
    bot: commands.Bot,
    guild: discord.Guild,
    session: dict[str, Any],
    config: dict[str, Any],
) -> list[tuple[int, str, int, list[str]]]:
    users = session.get("users", {})
    joins = session.get("joins", {})
    exempt_ids = set(session.get("exempt_bot_ids", []))
    if bot.user is not None:
        exempt_ids.add(bot.user.id)

    candidates: list[tuple[int, str, int, list[str]]] = []
    for key in set(users) | set(joins):
        user_id = int(key)
        member = guild.get_member(user_id)
        if user_id in exempt_ids or (member is not None and is_guard_staff(member, config)):
            continue
        stats = users.get(key)
        join = joins.get(key)
        score, reasons = score_suspect(user_id, stats, join, config["new_account_days"])
        name = (stats or join or {}).get("name") or str(user_id)
        candidates.append((user_id, name, score, reasons))

    candidates.sort(key=lambda item: (item[2], item[0]), reverse=True)
    return candidates


def _build_stats_embed(guild: discord.Guild, session: dict[str, Any], config: dict[str, Any]) -> discord.Embed:
    users = session.get("users", {})
    joins = session.get("joins", {})
    actions = session.get("actions", [])
    total_messages = sum(int(entry.get("messages", 0)) for entry in users.values())
    recent_joins = [
        key
        for key in joins
        if account_age_days(int(key)) < config["new_account_days"]
    ]
    mass_mentions = sum(int(entry.get("mass_mentions", 0)) for entry in users.values())

    embed = _base_embed("Noah Guard · Estadísticas", _state_line(session), COLOR_INFO)
    embed.add_field(
        name="Resumen",
        value=(
            f"Mensajes registrados: **{total_messages}**\n"
            f"Usuarios que han escrito: **{len(users)}**\n"
            f"Entradas al servidor: **{len(joins)}**, de las cuales **{len(recent_joins)}** "
            f"con cuenta de menos de {config['new_account_days']} días\n"
            f"Menciones masivas: **{mass_mentions}**\n"
            f"Baneos: **{len([a for a in actions if a['action'] in ('autoban', 'ban')])}**"
            f" · Expulsiones: **{len([a for a in actions if a['action'] == 'kick'])}**"
        ),
        inline=False,
    )

    ranked = sorted(
        users.items(),
        key=lambda item: (int(item[1].get("messages", 0)), item[0]),
        reverse=True,
    )
    lines = []
    for position, (key, entry) in enumerate(ranked[:10], start=1):
        extras = [f"pico {entry.get('peak_per_minute', 0)}/min"]
        if int(entry.get("duplicates", 0)):
            extras.append(f"{entry['duplicates']} repetidos")
        lines.append(
            f"`{position:02d}` {_user_label(guild, int(key), entry.get('name'))} · "
            f"**{entry.get('messages', 0)}** mensajes · " + " · ".join(extras)
        )
    embed.add_field(
        name="Usuarios más activos",
        value=_clip_lines(lines) if lines else "Nadie ha escrito todavía.",
        inline=False,
    )
    embed.set_footer(text=f"{FOOTER} · Detalle: .noah guard user @usuario")
    return embed


def _join_line(guild: discord.Guild, key: str, join: dict[str, Any], new_days: int) -> str:
    user_id = int(key)
    age = account_age_days(user_id)
    parts = [
        f"entró {_ts(join['joined_at'])}",
        f"cuenta de {_format_age(age)}",
    ]
    if join.get("default_avatar"):
        parts.append("sin avatar")
    if join.get("bot"):
        parts.append("bot")
    if age < new_days:
        parts.append("**cuenta nueva**")
    if join.get("left_at"):
        parts.append("ya ha salido")
    return f"{_user_label(guild, user_id, join.get('name'))} · " + " · ".join(parts)


def _join_text_line(key: str, join: dict[str, Any], new_days: int) -> str:
    user_id = int(key)
    age = account_age_days(user_id)
    joined = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(join["joined_at"]))
    flags = []
    if join.get("default_avatar"):
        flags.append("sin avatar")
    if join.get("bot"):
        flags.append("bot")
    if age < new_days:
        flags.append("cuenta nueva")
    if join.get("left_at"):
        flags.append("ha salido")
    return (
        f"- {join.get('name')} ({user_id}) | entró {joined} | cuenta de {_format_age(age)}"
        + (f" | {', '.join(flags)}" if flags else "")
    )


def _build_user_embed(
    bot: commands.Bot,
    guild: discord.Guild,
    user: discord.User,
    session: dict[str, Any] | None,
    config: dict[str, Any],
) -> discord.Embed:
    member = guild.get_member(user.id)
    stats = (session or {}).get("users", {}).get(str(user.id))
    join = (session or {}).get("joins", {}).get(str(user.id))
    age = account_age_days(user.id)

    embed = _base_embed(f"Noah Guard · {user}", f"{user.mention} (`{user.id}`)", COLOR_INFO)
    embed.set_thumbnail(url=user.display_avatar.url)
    embed.add_field(
        name="Cuenta",
        value=(
            f"Creada {_ts(user.created_at.timestamp(), 'D')} (hace {_format_age(age)})\n"
            f"Avatar personalizado: {'no' if user.avatar is None else 'sí'}"
            + ("\nCuenta de bot" if user.bot else "")
        ),
        inline=False,
    )

    if join is not None:
        server = f"Entró durante la contingencia {_ts(join['joined_at'])}"
        if join.get("left_at"):
            server += f"\nSalió {_ts(join['left_at'])}"
    elif member is not None and member.joined_at is not None:
        server = f"Miembro desde {_ts(member.joined_at.timestamp(), 'D')}"
    else:
        server = "No está en el servidor."
    if member is not None:
        roles = [role.mention for role in reversed(member.roles) if not role.is_default()]
        server += "\nRoles: " + (", ".join(roles[:10]) if roles else "ninguno")
        if is_guard_staff(member, config):
            server += "\nForma parte del staff exento."
    embed.add_field(name="Servidor", value=server, inline=False)

    if stats is not None:
        embed.add_field(
            name="Actividad durante la contingencia",
            value=(
                f"Mensajes: **{stats.get('messages', 0)}**\n"
                f"Primer mensaje: {_ts(stats['first_at'])} · Último: {_ts(stats['last_at'])}\n"
                f"Pico: **{stats.get('peak_per_minute', 0)}** mensajes/min\n"
                f"Repetidos: {stats.get('duplicates', 0)} · Menciones: {stats.get('mentions', 0)}"
                f" · Enlaces: {stats.get('links', 0)} · Adjuntos: {stats.get('attachments', 0)}\n"
                f"Canales usados: {len(stats.get('channels', []))}"
                f" · Menciones masivas: {stats.get('mass_mentions', 0)}"
            ),
            inline=False,
        )
    elif session is not None:
        embed.add_field(
            name="Actividad durante la contingencia",
            value="No ha escrito ningún mensaje.",
            inline=False,
        )

    if session is not None and not (member is not None and is_guard_staff(member, config)):
        score, reasons = score_suspect(user.id, stats, join, config["new_account_days"])
        embed.add_field(
            name="Valoración",
            value=f"Puntuación de sospecha: **{score}**\n"
            + (", ".join(reasons).capitalize() + "." if reasons else "Sin indicios."),
            inline=False,
        )

    actions = [
        action for action in (session or {}).get("actions", []) if action["user_id"] == user.id
    ]
    if actions:
        labels = {
            "autoban": "Baneo automático",
            "ban": "Baneo",
            "kick": "Expulsión",
            "warning": "Aviso de simulacro",
        }
        embed.add_field(
            name="Acciones",
            value="\n".join(
                f"{labels.get(action['action'], action['action'])} {_ts(action['at'])}"
                for action in actions
            ),
            inline=False,
        )
    return embed


# ---------------------------------------------------------------------------
# Acciones en bloque
# ---------------------------------------------------------------------------


def _bulk_targets(
    bot: commands.Bot,
    guild: discord.Guild,
    session: dict[str, Any],
    config: dict[str, Any],
    action: str,
    scope: str,
    min_score: int,
) -> list[tuple[int, str, str]]:
    banned = {
        entry["user_id"]
        for entry in session.get("actions", [])
        if entry["action"] in ("autoban", "ban")
    }
    targets: list[tuple[int, str, str]] = []

    if scope == "joins":
        exempt_ids = set(session.get("exempt_bot_ids", []))
        if bot.user is not None:
            exempt_ids.add(bot.user.id)
        for key, join in sorted(
            session.get("joins", {}).items(), key=lambda item: item[1]["joined_at"]
        ):
            user_id = int(key)
            member = guild.get_member(user_id)
            if user_id in exempt_ids or user_id in banned:
                continue
            if member is not None and is_guard_staff(member, config):
                continue
            if action == "kick" and member is None:
                continue
            targets.append(
                (user_id, join.get("name") or key, f"cuenta de {_format_age(account_age_days(user_id))}")
            )
        return targets

    for user_id, name, score, reasons in _suspect_candidates(bot, guild, session, config):
        if score < min_score or user_id in banned:
            continue
        if action == "kick" and guild.get_member(user_id) is None:
            continue
        targets.append((user_id, name, f"{score} puntos: {', '.join(reasons)}"))
    return targets


class BulkActionView(discord.ui.View):
    def __init__(self, author_id: int, label: str) -> None:
        super().__init__(timeout=60)
        self.author_id = author_id
        self.confirmed = False
        self.confirm.label = label

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Solo quien ha ejecutado el comando puede confirmarlo.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="Confirmar", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        _ = button
        self.confirmed = True
        await interaction.response.edit_message(view=None)
        self.stop()

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        _ = button
        await interaction.response.edit_message(view=None)
        self.stop()


async def _run_bulk_action(ctx: commands.Context, action: str, scope: str, min_score: int) -> None:
    bot = ctx.bot
    guild = ctx.guild
    store = get_bot_context(bot).guard
    session = store.get_session(guild.id)
    if session is None:
        await ctx.send(embed=_no_data_embed())
        return

    scope = scope.lower()
    if scope not in ("joins", "suspects"):
        await ctx.send(
            embed=_error_embed(
                f"Usa `.noah guard {action} joins` o `.noah guard {action} suspects [puntos]`."
            )
        )
        return

    config = store.get_config(guild.id)
    targets = _bulk_targets(bot, guild, session, config, action, scope, min_score)
    verb = "banear" if action == "ban" else "expulsar"
    if not targets:
        await ctx.send(embed=_error_embed(f"No hay ningún usuario que {verb} con ese criterio."))
        return

    scope_label = (
        "entraron durante la contingencia"
        if scope == "joins"
        else f"tienen {min_score} o más puntos de sospecha"
    )
    preview = [
        f"{_user_label(guild, user_id, name)} · {detail}"
        for user_id, name, detail in targets
    ]
    embed = _base_embed(
        f"Confirmar acción en bloque · {verb.capitalize()}",
        (
            f"Vas a {verb} a **{len(targets)}** usuarios que {scope_label}. "
            "El staff y los bots previos quedan excluidos. "
            + ("No se borrará ningún mensaje. " if action == "ban" else "")
            + "Esta acción no se puede deshacer desde Noah."
        ),
        COLOR_ALERT,
    )
    embed.add_field(name="Usuarios afectados", value=_clip_lines(preview), inline=False)
    view = BulkActionView(ctx.author.id, f"Confirmar ({len(targets)})")
    send_kwargs: dict[str, Any] = {"embed": embed, "view": view}
    if len(targets) > LIST_PREVIEW_LIMIT:
        send_kwargs["file"] = _text_file(
            f"guard_{action}_{scope}.txt",
            [f"- {name} ({user_id}) | {detail}" for user_id, name, detail in targets],
        )
    message = await ctx.send(**send_kwargs)

    timed_out = await view.wait()
    if not view.confirmed:
        with suppress(discord.HTTPException):
            await message.edit(
                embed=_base_embed(
                    "Acción en bloque cancelada",
                    "Se ha agotado el tiempo de confirmación." if timed_out else "No se ha aplicado ningún cambio.",
                    COLOR_INFO,
                ),
                view=None,
            )
        return

    reason = f"Noah Guard: acción en bloque ({scope}) por {ctx.author}"
    done = 0
    failed: list[str] = []
    for index, (user_id, name, _) in enumerate(targets, start=1):
        try:
            if action == "ban":
                await guild.ban(discord.Object(id=user_id), reason=reason, delete_message_seconds=0)
            else:
                await guild.kick(discord.Object(id=user_id), reason=reason)
        except discord.HTTPException:
            failed.append(name)
            continue
        store.record_action(guild.id, action, user_id, name, ctx.author.id)
        done += 1
        if index % 10 == 0:
            with suppress(discord.HTTPException):
                await message.edit(
                    embed=_base_embed(
                        f"{verb.capitalize()} en curso",
                        f"Procesados: **{index}** de **{len(targets)}**",
                        COLOR_ALERT,
                    )
                )

    result = _base_embed(
        "Acción en bloque completada",
        f"Usuarios {'baneados' if action == 'ban' else 'expulsados'}: **{done}** de **{len(targets)}**",
        COLOR_OK if not failed else COLOR_DRILL,
    )
    if failed:
        result.add_field(
            name="No se ha podido aplicar a",
            value=_clip_lines(failed) + "\nRevisa la jerarquía de roles y mis permisos.",
            inline=False,
        )
    with suppress(discord.HTTPException):
        await message.edit(embed=result, view=None)


# ---------------------------------------------------------------------------
# Panel de configuración
# ---------------------------------------------------------------------------


class GuardSettingsModal(discord.ui.Modal, title="Ajustes de Noah Guard"):
    def __init__(self, panel: "GuardConfigView") -> None:
        super().__init__()
        self.panel = panel
        config = panel.store.get_config(panel.guild.id)
        self.new_days = discord.ui.TextInput(
            label="Días para considerar una cuenta como nueva",
            default=str(config["new_account_days"]),
            max_length=3,
        )
        self.drill_minutes = discord.ui.TextInput(
            label="Duración del simulacro en minutos",
            default=str(config["drill_minutes"]),
            max_length=3,
        )
        self.add_item(self.new_days)
        self.add_item(self.drill_minutes)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            days = int(self.new_days.value)
            minutes = int(self.drill_minutes.value)
        except ValueError:
            await interaction.response.send_message(
                "Los dos valores deben ser números enteros.",
                ephemeral=True,
            )
            return

        if not 1 <= days <= 365 or not 1 <= minutes <= 120:
            await interaction.response.send_message(
                "Los días deben estar entre 1 y 365 y los minutos entre 1 y 120.",
                ephemeral=True,
            )
            return

        self.panel.store.set_new_account_days(self.panel.guild.id, days)
        self.panel.store.set_drill_minutes(self.panel.guild.id, minutes)
        await self.panel.refresh(interaction)


class GuardConfigView(discord.ui.View):
    def __init__(self, bot: commands.Bot, guild: discord.Guild, author_id: int) -> None:
        super().__init__(timeout=300)
        self.bot = bot
        self.guild = guild
        self.author_id = author_id
        self.store = get_bot_context(bot).guard
        self.message: discord.Message | None = None

        config = self.store.get_config(guild.id)
        self.user_select = discord.ui.UserSelect(
            placeholder="Usuarios autorizados (marca la lista completa)",
            min_values=0,
            max_values=SELECT_LIMIT,
            default_values=[
                discord.Object(id=user_id, type=discord.User)
                for user_id in config["users"][:SELECT_LIMIT]
            ],
            row=0,
        )
        self.user_select.callback = self._on_users
        self.role_select = discord.ui.RoleSelect(
            placeholder="Roles autorizados (marca la lista completa)",
            min_values=0,
            max_values=SELECT_LIMIT,
            default_values=[
                discord.Object(id=role_id, type=discord.Role)
                for role_id in config["roles"][:SELECT_LIMIT]
                if guild.get_role(role_id) is not None
            ],
            row=1,
        )
        self.role_select.callback = self._on_roles
        self.add_item(self.user_select)
        self.add_item(self.role_select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Solo quien ha abierto el panel puede usarlo.",
                ephemeral=True,
            )
            return False
        return True

    async def refresh(self, interaction: discord.Interaction) -> None:
        view = GuardConfigView(self.bot, self.guild, self.author_id)
        view.message = self.message
        self.stop()
        await interaction.response.edit_message(
            embed=_build_config_embed(self.guild, self.store.get_config(self.guild.id)),
            view=view,
        )

    async def _on_users(self, interaction: discord.Interaction) -> None:
        self.store.set_users(self.guild.id, [user.id for user in self.user_select.values])
        await self.refresh(interaction)

    async def _on_roles(self, interaction: discord.Interaction) -> None:
        self.store.set_roles(
            self.guild.id,
            [role.id for role in self.role_select.values if not role.is_default()],
        )
        await self.refresh(interaction)

    @discord.ui.button(label="Ajustes", style=discord.ButtonStyle.secondary, row=2)
    async def settings(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        _ = button
        await interaction.response.send_modal(GuardSettingsModal(self))

    @discord.ui.button(label="Cerrar", style=discord.ButtonStyle.secondary, row=2)
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        _ = button
        self.stop()
        await interaction.response.edit_message(view=None)

    async def on_timeout(self) -> None:
        if self.message is not None:
            with suppress(discord.HTTPException):
                await self.message.edit(view=None)


# ---------------------------------------------------------------------------
# Menciones masivas y comandos bloqueados
# ---------------------------------------------------------------------------


def _has_mass_mention(message: discord.Message) -> bool:
    return message.mention_everyone or bool(MASS_MENTION_PATTERN.search(message.content))


async def _handle_mass_mention(
    bot: commands.Bot,
    message: discord.Message,
    session: dict[str, Any],
) -> None:
    guild = message.guild
    author = message.author
    handled = _mass_mention_handled.setdefault(guild.id, set())
    if author.id in handled:
        return
    handled.add(author.id)

    store = get_bot_context(bot).guard
    if session.get("drill"):
        store.record_action(guild.id, "warning", author.id, str(author), None)
        embed = _base_embed(
            "Simulacro · Mención masiva detectada",
            (
                f"{author.mention} ha utilizado una mención masiva. En un incidente real "
                "habría sido baneado automáticamente."
            ),
            COLOR_DRILL,
        )
    else:
        try:
            await guild.ban(
                author,
                reason="Noah Guard: mención masiva durante la contingencia",
                delete_message_seconds=0,
            )
        except discord.HTTPException:
            embed = _base_embed(
                "Mención masiva detectada",
                (
                    f"No he podido banear automáticamente a {author.mention}. "
                    "Un administrador debe revisarlo."
                ),
                COLOR_ALERT,
            )
        else:
            store.record_action(guild.id, "autoban", author.id, str(author), None)
            embed = _base_embed(
                "Usuario baneado",
                (
                    f"**{author}** ha sido baneado automáticamente por utilizar una "
                    "mención masiva durante el protocolo de contingencia."
                ),
                COLOR_ALERT,
            )

    with suppress(discord.HTTPException):
        await message.channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())


async def _reply_blocked(ctx: commands.Context) -> None:
    key = (ctx.guild.id, ctx.author.id)
    now = time.monotonic()
    if now - _blocked_replies.get(key, 0.0) < BLOCKED_REPLY_COOLDOWN_SECONDS:
        return
    _blocked_replies[key] = now

    session = get_bot_context(ctx.bot).guard.get_session(ctx.guild.id) or {}
    starter = f"<@{session.get('started_by')}>" if session.get("started_by") else "la administración"
    embed = _base_embed(
        "Comandos restringidos",
        (
            "Noah se encuentra en modo de contingencia y sus comandos están reservados "
            "temporalmente a la administración. Volverán a estar disponibles en cuanto "
            f"se resuelva la incidencia. Si necesitas algo, contacta con {starter}."
        ),
        COLOR_DRILL if session.get("drill") else COLOR_ALERT,
    )
    with suppress(discord.HTTPException):
        await ctx.reply(
            embed=embed,
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )


# ---------------------------------------------------------------------------
# Registro
# ---------------------------------------------------------------------------


def register_guard_commands(bot: commands.Bot, noah_group: commands.Group) -> None:
    async def _guard_command_check(ctx: commands.Context) -> bool:
        if ctx.guild is None:
            return True
        store = get_bot_context(ctx.bot).guard
        if not store.is_active(ctx.guild.id):
            return True
        if is_guard_staff(ctx.author, store.get_config(ctx.guild.id)):
            return True
        raise GuardCommandBlocked()

    bot.add_check(_guard_command_check)

    @bot.listen("on_command_error")
    async def _guard_command_error(ctx: commands.Context, error: commands.CommandError) -> None:
        if isinstance(error, GuardCommandBlocked):
            await _reply_blocked(ctx)
            return

        # Registrar un listener desactiva el manejador por defecto de discord.py,
        # así que replicamos su comportamiento para el resto de errores.
        command = ctx.command
        if command is not None and command.has_error_handler():
            return
        if ctx.cog is not None and ctx.cog.has_error_handler():
            return
        _log.error("Ignoring exception in command %s", command, exc_info=error)

    @bot.listen("on_ready")
    async def _resume_guard_sessions() -> None:
        store = get_bot_context(bot).guard
        for guild_id in store.active_guild_ids():
            session = store.get_session(guild_id) or {}
            if session.get("drill") and session.get("drill_ends_at"):
                _schedule_drill_stop(bot, guild_id, float(session["drill_ends_at"]))

    @bot.listen("on_message")
    async def _guard_on_message(message: discord.Message) -> None:
        guild = message.guild
        if guild is None:
            return

        store = get_bot_context(bot).guard
        session = store.get_session(guild.id)
        if session is None or not session.get("active"):
            return
        if bot.user is not None and message.author.id == bot.user.id:
            return

        if message.channel.id == session.get("text_channel_id") and store.bump_notice_counter(
            guild.id,
            NOTICE_EVERY_MESSAGES,
        ):
            with suppress(discord.HTTPException):
                await message.channel.send(
                    embed=_build_notice_embed(session, reminder=True),
                    allowed_mentions=discord.AllowedMentions.none(),
                )

        if _is_exempt_author(bot, message, store.get_config(guild.id), session):
            return

        mass_mention = _has_mass_mention(message)
        store.record_message(message, mass_mention)
        if mass_mention:
            await _handle_mass_mention(bot, message, session)

    @bot.listen("on_message_edit")
    async def _guard_on_message_edit(before: discord.Message, after: discord.Message) -> None:
        guild = after.guild
        if guild is None or _has_mass_mention(before) or not _has_mass_mention(after):
            return

        store = get_bot_context(bot).guard
        session = store.get_session(guild.id)
        if session is None or not session.get("active"):
            return
        if _is_exempt_author(bot, after, store.get_config(guild.id), session):
            return
        await _handle_mass_mention(bot, after, session)

    @bot.listen("on_member_join")
    async def _guard_on_member_join(member: discord.Member) -> None:
        get_bot_context(bot).guard.record_join(member)

    @bot.listen("on_member_remove")
    async def _guard_on_member_remove(member: discord.Member) -> None:
        get_bot_context(bot).guard.record_leave(member)

    async def _ensure_staff(ctx: commands.Context) -> bool:
        if ctx.guild is None:
            await ctx.send(embed=_error_embed("Este comando solo funciona dentro de un servidor."))
            return False

        config = get_bot_context(ctx.bot).guard.get_config(ctx.guild.id)
        if not is_guard_staff(ctx.author, config):
            await ctx.send(
                embed=_error_embed(
                    "Solo la administración y los usuarios autorizados pueden usar Noah Guard."
                )
            )
            return False
        return True

    @noah_group.group(invoke_without_command=True)
    async def guard(ctx: commands.Context) -> None:
        if not await _ensure_staff(ctx):
            return
        await ctx.send(embed=_build_help_embed())

    @guard.command(name="help")
    async def guard_help(ctx: commands.Context) -> None:
        if not await _ensure_staff(ctx):
            return
        await ctx.send(embed=_build_help_embed())

    @guard.command()
    async def start(ctx: commands.Context) -> None:
        if not await _ensure_staff(ctx):
            return
        await _activate(ctx, drill=False)

    @guard.command()
    async def stop(ctx: commands.Context) -> None:
        if not await _ensure_staff(ctx):
            return

        store = get_bot_context(ctx.bot).guard
        lock = _lock_for(ctx.guild.id)
        if lock.locked():
            await ctx.send(embed=_error_embed("Hay otra operación de Noah Guard en curso."))
            return

        async with lock:
            session = store.get_session(ctx.guild.id)
            if session is None or not session.get("active"):
                await ctx.send(embed=_error_embed("El protocolo de contingencia no está activo."))
                return

            _cancel_drill_timer(ctx.guild.id)
            in_temp_channel = ctx.channel.id == session.get("text_channel_id")
            status_message = None
            if not in_temp_channel:
                status_message = await ctx.send(
                    embed=_base_embed(
                        "Finalizando contingencia",
                        "Restaurando permisos y eliminando los canales temporales...",
                        COLOR_INFO,
                    )
                )
            embed = await _deactivate(ctx.bot, ctx.guild, ctx.author)

        if status_message is not None:
            with suppress(discord.HTTPException):
                await status_message.edit(embed=embed)
            return

        # El canal temporal ya no existe: el resumen se envía por privado.
        with suppress(discord.HTTPException):
            await ctx.author.send(embed=embed)

    @guard.command()
    async def retry(ctx: commands.Context) -> None:
        if not await _ensure_staff(ctx):
            return

        store = get_bot_context(ctx.bot).guard
        async with _lock_for(ctx.guild.id):
            session = store.get_session(ctx.guild.id)
            if session is None or session.get("active"):
                await ctx.send(
                    embed=_error_embed(
                        "Solo se puede reintentar cuando el protocolo ya está finalizado."
                    )
                )
                return

            pending = [
                OverwriteChange.from_payload(payload)
                for payload in session.get("pending_changes", [])
            ]
            if not pending:
                await ctx.send(
                    embed=_base_embed(
                        "Noah Guard",
                        "No hay ajustes de permisos pendientes de restaurar.",
                        COLOR_OK,
                    )
                )
                return

            restored, kept, failed = await _revert_changes(
                ctx.bot,
                ctx.guild,
                pending,
                f"Noah Guard: reintento de restauración por {ctx.author}",
            )
            store.update_session(
                ctx.guild.id,
                pending_changes=[change.to_payload() for change in failed],
            )

        await ctx.send(
            embed=_base_embed(
                "Restauración pendiente",
                (
                    f"Ajustes restaurados: **{restored}**\n"
                    f"Conservados por cambios manuales: **{kept}**\n"
                    f"Siguen pendientes: **{len(failed)}**"
                ),
                COLOR_OK if not failed else COLOR_DRILL,
            )
        )

    @guard.command()
    async def drill(ctx: commands.Context, mode: str = "") -> None:
        if not await _ensure_staff(ctx):
            return

        if mode.lower() == "live":
            await _activate(ctx, drill=True)
            return
        if mode:
            await ctx.send(embed=_error_embed("Usa `.noah guard drill` o `.noah guard drill live`."))
            return
        await _dry_run(ctx)

    @guard.command()
    async def status(ctx: commands.Context) -> None:
        if not await _ensure_staff(ctx):
            return

        store = get_bot_context(ctx.bot).guard
        session = store.get_session(ctx.guild.id)
        config = store.get_config(ctx.guild.id)
        if session is None or not session.get("active"):
            embed = _base_embed(
                "Noah Guard · Estado",
                "**Estado:** Inactivo\nUsa `.noah guard start` para activar el protocolo.",
                COLOR_INFO,
            )
            pending = len((session or {}).get("pending_changes", []))
            if pending:
                embed.add_field(
                    name="Pendiente",
                    value=f"**{pending}** ajustes sin restaurar. Usa `.noah guard retry`.",
                    inline=False,
                )
        else:
            embed = _base_embed(
                "Noah Guard · Estado",
                _state_line(session),
                COLOR_DRILL if session.get("drill") else COLOR_ALERT,
            )
            embed.add_field(
                name="Canales temporales",
                value=(
                    f"Texto: <#{session.get('text_channel_id')}>\n"
                    f"Voz: <#{session.get('voice_channel_id')}>"
                ),
                inline=False,
            )
            embed.add_field(
                name="Cambios",
                value=(
                    f"Ajustes de permisos aplicados: **{len(session.get('changes', []))}**\n"
                    f"Canales con incidencias: **{len(session.get('failed_channels', []))}**\n"
                    f"Usuarios movidos de canal de voz: **{len(session.get('moved_members', {}))}**"
                ),
                inline=False,
            )
            if session.get("drill") and session.get("drill_ends_at"):
                embed.add_field(
                    name="Duración",
                    value=f"El simulacro finaliza {_ts(session['drill_ends_at'])}.",
                    inline=False,
                )
        embed.add_field(
            name="Configuración",
            value=(
                f"Usuarios autorizados: {len(config['users'])} · "
                f"Roles autorizados: {len(config['roles'])}\n"
                "Consulta el detalle con `.noah guard config show`."
            ),
            inline=False,
        )
        await ctx.send(embed=embed)

    @guard.command()
    async def stats(ctx: commands.Context) -> None:
        if not await _ensure_staff(ctx):
            return

        session = _session_or_none(ctx)
        if session is None:
            await ctx.send(embed=_no_data_embed())
            return
        config = get_bot_context(ctx.bot).guard.get_config(ctx.guild.id)
        await ctx.send(embed=_build_stats_embed(ctx.guild, session, config))

    @guard.command()
    async def joins(ctx: commands.Context) -> None:
        if not await _ensure_staff(ctx):
            return

        session = _session_or_none(ctx)
        if session is None:
            await ctx.send(embed=_no_data_embed())
            return

        config = get_bot_context(ctx.bot).guard.get_config(ctx.guild.id)
        new_days = config["new_account_days"]
        entries = sorted(
            session.get("joins", {}).items(),
            key=lambda item: item[1]["joined_at"],
            reverse=True,
        )
        recent = [key for key, _ in entries if account_age_days(int(key)) < new_days]
        embed = _base_embed(
            "Noah Guard · Entradas",
            (
                f"{_state_line(session)}\n\n"
                f"Entradas registradas: **{len(entries)}**\n"
                f"Cuentas nuevas (menos de {new_days} días): **{len(recent)}**"
            ),
            COLOR_INFO,
        )
        lines = [
            f"`{position:02d}` {_join_line(ctx.guild, key, join, new_days)}"
            for position, (key, join) in enumerate(entries[:LIST_PREVIEW_LIMIT], start=1)
        ]
        embed.add_field(
            name="Más recientes",
            value=_clip_lines(lines) if lines else "Nadie ha entrado durante la contingencia.",
            inline=False,
        )

        if len(entries) > LIST_PREVIEW_LIMIT:
            embed.set_footer(text=f"{FOOTER} · Lista completa en el archivo adjunto")
            await ctx.send(
                embed=embed,
                file=_text_file(
                    "guard_entradas.txt",
                    [_join_text_line(key, join, new_days) for key, join in entries],
                ),
            )
            return
        await ctx.send(embed=embed)

    @guard.command()
    async def user(ctx: commands.Context, target: discord.User) -> None:
        if not await _ensure_staff(ctx):
            return

        config = get_bot_context(ctx.bot).guard.get_config(ctx.guild.id)
        await ctx.send(
            embed=_build_user_embed(ctx.bot, ctx.guild, target, _session_or_none(ctx), config)
        )

    @guard.command()
    async def suspects(ctx: commands.Context) -> None:
        if not await _ensure_staff(ctx):
            return

        session = _session_or_none(ctx)
        if session is None:
            await ctx.send(embed=_no_data_embed())
            return

        config = get_bot_context(ctx.bot).guard.get_config(ctx.guild.id)
        candidates = [
            item
            for item in _suspect_candidates(ctx.bot, ctx.guild, session, config)
            if item[2] > 0
        ]
        embed = _base_embed(
            "Noah Guard · Sospechosos",
            (
                f"{_state_line(session)}\n\n"
                "Puntuación orientativa basada en la antigüedad de la cuenta, la entrada "
                "durante la contingencia, el avatar y el ritmo y contenido de los mensajes."
            ),
            COLOR_INFO,
        )
        lines = [
            f"`{position:02d}` {_user_label(ctx.guild, user_id, name)} · **{score}** puntos · "
            + ", ".join(reasons)
            for position, (user_id, name, score, reasons) in enumerate(
                candidates[:LIST_PREVIEW_LIMIT],
                start=1,
            )
        ]
        embed.add_field(
            name=f"Usuarios con indicios ({len(candidates)})",
            value=_clip_lines(lines) if lines else "No hay usuarios con indicios.",
            inline=False,
        )
        embed.set_footer(
            text=f"{FOOTER} · Acción en bloque: .noah guard ban suspects <puntos>"
        )

        if len(candidates) > LIST_PREVIEW_LIMIT:
            await ctx.send(
                embed=embed,
                file=_text_file(
                    "guard_sospechosos.txt",
                    [
                        f"- {name} ({user_id}) | {score} puntos | {', '.join(reasons)}"
                        for user_id, name, score, reasons in candidates
                    ],
                ),
            )
            return
        await ctx.send(embed=embed)

    @guard.command(name="ban")
    async def guard_ban(
        ctx: commands.Context,
        scope: str = "",
        min_score: int = DEFAULT_SUSPECT_SCORE,
    ) -> None:
        if not await _ensure_staff(ctx):
            return
        await _run_bulk_action(ctx, "ban", scope, min_score)

    @guard.command(name="kick")
    async def guard_kick(
        ctx: commands.Context,
        scope: str = "",
        min_score: int = DEFAULT_SUSPECT_SCORE,
    ) -> None:
        if not await _ensure_staff(ctx):
            return
        await _run_bulk_action(ctx, "kick", scope, min_score)

    # -- Configuración -----------------------------------------------------

    @guard.group(invoke_without_command=True)
    async def config(ctx: commands.Context) -> None:
        if not await _ensure_staff(ctx):
            return

        store = get_bot_context(ctx.bot).guard
        view = GuardConfigView(ctx.bot, ctx.guild, ctx.author.id)
        view.message = await ctx.send(
            embed=_build_config_embed(ctx.guild, store.get_config(ctx.guild.id)),
            view=view,
        )

    @config.command(name="show")
    async def config_show(ctx: commands.Context) -> None:
        if not await _ensure_staff(ctx):
            return
        store = get_bot_context(ctx.bot).guard
        await ctx.send(embed=_build_config_embed(ctx.guild, store.get_config(ctx.guild.id)))

    async def _config_result(ctx: commands.Context, changed: bool, done: str, unchanged: str) -> None:
        store = get_bot_context(ctx.bot).guard
        embed = _build_config_embed(ctx.guild, store.get_config(ctx.guild.id))
        embed.description = done if changed else unchanged
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @config.command(name="adduser")
    async def config_adduser(ctx: commands.Context, member: discord.Member) -> None:
        if not await _ensure_staff(ctx):
            return
        changed = get_bot_context(ctx.bot).guard.add_user(ctx.guild.id, member.id)
        await _config_result(
            ctx,
            changed,
            f"{member.mention} ahora es un usuario autorizado.",
            f"{member.mention} ya era un usuario autorizado.",
        )

    @config.command(name="removeuser")
    async def config_removeuser(ctx: commands.Context, member: discord.User) -> None:
        if not await _ensure_staff(ctx):
            return
        changed = get_bot_context(ctx.bot).guard.remove_user(ctx.guild.id, member.id)
        await _config_result(
            ctx,
            changed,
            f"{member.mention} ya no es un usuario autorizado.",
            f"{member.mention} no estaba en la lista de usuarios autorizados.",
        )

    @config.command(name="addrole")
    async def config_addrole(ctx: commands.Context, role: discord.Role) -> None:
        if not await _ensure_staff(ctx):
            return
        if role.is_default():
            await ctx.send(embed=_error_embed("No se puede autorizar al rol @everyone."))
            return
        changed = get_bot_context(ctx.bot).guard.add_role(ctx.guild.id, role.id)
        await _config_result(
            ctx,
            changed,
            f"{role.mention} ahora es un rol autorizado.",
            f"{role.mention} ya era un rol autorizado.",
        )

    @config.command(name="removerole")
    async def config_removerole(ctx: commands.Context, role: discord.Role) -> None:
        if not await _ensure_staff(ctx):
            return
        changed = get_bot_context(ctx.bot).guard.remove_role(ctx.guild.id, role.id)
        await _config_result(
            ctx,
            changed,
            f"{role.mention} ya no es un rol autorizado.",
            f"{role.mention} no estaba en la lista de roles autorizados.",
        )

    @config.command(name="newdays")
    async def config_newdays(ctx: commands.Context, days: int) -> None:
        if not await _ensure_staff(ctx):
            return
        if not 1 <= days <= 365:
            await ctx.send(embed=_error_embed("Indica un número de días entre 1 y 365."))
            return
        get_bot_context(ctx.bot).guard.set_new_account_days(ctx.guild.id, days)
        await _config_result(
            ctx,
            True,
            f"Una cuenta se considera nueva si se creó hace menos de {days} días.",
            "",
        )

    @config.command(name="drillminutes")
    async def config_drillminutes(ctx: commands.Context, minutes: int) -> None:
        if not await _ensure_staff(ctx):
            return
        if not 1 <= minutes <= 120:
            await ctx.send(embed=_error_embed("Indica una duración entre 1 y 120 minutos."))
            return
        get_bot_context(ctx.bot).guard.set_drill_minutes(ctx.guild.id, minutes)
        await _config_result(
            ctx,
            True,
            f"El simulacro en vivo durará {minutes} minutos.",
            "",
        )
