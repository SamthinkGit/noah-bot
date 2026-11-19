from datetime import datetime

import discord
from discord.ext import commands, tasks

from noah_bot.modules.bot_context import get_bot_context
from noah_bot.modules.daily_stats import overlap_seconds_for_current_day
from noah_bot.modules.discord_formatter import EmbedTable


def _format_minutes(total_minutes: int) -> str:
    hours, minutes = divmod(max(total_minutes, 0), 60)
    if hours == 0:
        return f"{minutes} min"
    return f"{hours}h {minutes}m"


def _format_hours(hours_value: float | None) -> str:
    if hours_value is None:
        return "-"

    if float(hours_value).is_integer():
        return f"{int(hours_value)}h"

    return f"{hours_value:g}h"


def _format_total_hours(total_minutes: int) -> str:
    return f"{max(total_minutes, 0) / 60:.2f}h"


def _format_timestamp(iso_value: str | None) -> str:
    if not iso_value:
        return "-"

    try:
        timestamp = int(datetime.fromisoformat(iso_value).timestamp())
    except ValueError:
        return "-"

    return f"<t:{timestamp}:R>"


def _can_manage_vc_settings(member: discord.Member) -> bool:
    if member.guild_permissions.administrator:
        return True

    return any(role.name == "Funcionarios" for role in member.roles)


def _is_listening(state: discord.VoiceState) -> bool:
    if state.channel is None:
        return False

    return not (state.self_deaf or state.deaf)


def _get_trackable_channel(
    state: discord.VoiceState,
    voice_manager,
) -> discord.VoiceChannel | discord.StageChannel | None:
    if not _is_listening(state):
        return None

    channel = state.channel
    if channel is None or voice_manager.is_channel_banned(channel.id):
        return None

    return channel


async def _send_level_alert(
    guild: discord.Guild,
    voice_manager,
    member: discord.Member,
    bot_user: discord.ClientUser | None,
    level: int,
) -> bool:
    alert_config = voice_manager.get_alert_channel(guild.id)
    if alert_config is None:
        return False

    channel = guild.get_channel(int(alert_config["channel_id"]))
    if channel is None or not isinstance(channel, discord.TextChannel):
        return False

    alert_type = voice_manager.get_user_alert_type(member.id)
    if alert_type == "noah":
        noah_mention = bot_user.mention if bot_user else "@Noah"
        message = (
            f"{noah_mention} ha apuntado `{level}` veces a {member.mention} en su "
            "𝖉𝖊𝖆𝖙𝖍𝖓𝖔𝖙𝖊"
        )
    else:
        message = (
            f"{member.mention} ha alcanzado el nivel `{level}` bajo la atenta mirada de "
            "𝑴𝒐𝒏𝒕𝒂𝒅𝒊𝒕𝒐 𝑽𝑰𝑰"
        )

    await channel.send(message)
    return True


def _voice_rooms(bot: commands.Bot) -> dict[int, list[list[int]]]:
    """Quién cuenta en cada canal de voz (mismo criterio que los stats de voz)."""
    voice_manager = get_bot_context(bot).voice_manager
    rooms: dict[int, list[list[int]]] = {}
    for guild in bot.guilds:
        channels: list[list[int]] = []
        for channel in [*guild.voice_channels, *guild.stage_channels]:
            if guild.afk_channel is not None and channel.id == guild.afk_channel.id:
                continue
            members = [
                member.id
                for member in channel.members
                if not member.bot
                and member.voice is not None
                and _get_trackable_channel(member.voice, voice_manager) is not None
            ]
            if members:
                channels.append(members)
        rooms[guild.id] = channels
    return rooms


def register_vc_stats_commands(bot: commands.Bot, noah_group: commands.Group) -> None:
    @tasks.loop(seconds=60)
    async def voice_social_tick() -> None:
        # Registro aparte (coincidencias y medias): no toca niveles ni voice_stats.json.
        get_bot_context(bot).voice_social.tick(_voice_rooms(bot))

    @voice_social_tick.error
    async def voice_social_tick_error(error: BaseException) -> None:
        _ = error
        voice_social_tick.restart()

    @bot.listen("on_ready")
    async def sync_voice_tracking() -> None:
        context = get_bot_context(bot)
        if not voice_social_tick.is_running():
            voice_social_tick.start()
        connected_members: list[dict[str, int | str | None]] = []

        for guild in bot.guilds:
            voice_channels = [*guild.voice_channels, *guild.stage_channels]

            for channel in voice_channels:
                for member in channel.members:
                    if not member.voice:
                        continue
                    if _get_trackable_channel(member.voice, context.voice_manager) is None:
                        continue

                    connected_members.append(
                        {
                            "user_id": member.id,
                            "display_name": member.display_name,
                            "channel_id": channel.id,
                            "channel_name": channel.name,
                            "guild_id": guild.id,
                            "guild_name": guild.name,
                        }
                    )

        context.voice_manager.sync_connected_members(connected_members)

    @bot.listen("on_voice_state_update")
    async def track_voice_state(
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        context = get_bot_context(bot)
        before_channel = _get_trackable_channel(before, context.voice_manager)
        after_channel = _get_trackable_channel(after, context.voice_manager)

        if before_channel == after_channel:
            return

        result = context.voice_manager.handle_voice_state_change(
            member.id,
            member.display_name,
            before_channel_id=before_channel.id if before_channel else None,
            before_channel_name=before_channel.name if before_channel else None,
            after_channel_id=after_channel.id if after_channel else None,
            after_channel_name=after_channel.name if after_channel else None,
            guild_id=member.guild.id,
            guild_name=member.guild.name,
        )

        if result and result.get("event") == "leave":
            tracked_seconds = overlap_seconds_for_current_day(
                result.get("started_at"),
                result.get("ended_at"),
            )
            if tracked_seconds > 0:
                context.daily_stats.add_vc_seconds(
                    member.guild.id,
                    member.guild.name,
                    member.id,
                    member.display_name,
                    tracked_seconds,
                )

        if result and result.get("leveled_up") and result.get("new_level") is not None:
            await _send_level_alert(
                member.guild,
                context.voice_manager,
                member,
                bot.user,
                int(result["new_level"]),
            )

    @noah_group.group(name="vc")
    async def vc(ctx: commands.Context) -> None:
        if ctx.invoked_subcommand is None:
            await ctx.send("Usa `.noah vc help` para ver los comandos de voz.")

    @vc.command()
    async def help(ctx: commands.Context) -> None:
        chart = EmbedTable(headers=["Command", "Description"], title="Voice Stats")
        chart.add_row([".noah vc summary", "Resumen visual de tus stats de voz."])
        chart.add_row([".noah vc summary @user", "Resumen visual de otro usuario."])
        chart.add_row([".noah vc stats", "Muestra tus stats de voz."])
        chart.add_row([".noah vc stats @user", "Muestra las stats de otro usuario."])
        chart.add_row([".noah vc top", "Ranking de tiempo en voice chat."])
        chart.add_row([".noah vc active", "Usuarios conectados ahora mismo."])
        chart.add_row(
            [
                ".noah vc alerttype <montadito|noah>",
                "Elige tu estilo personal de alerta de nivel.",
            ]
        )
        chart.add_row(
            [
                ".noah vc setminutes @user <minutes>",
                "[Admin/Funcionarios] Fija los minutos totales.",
            ]
        )
        chart.add_row(
            [
                ".noah vc startleveling <hours>",
                "[Admin/Funcionarios] Activa los niveles por horas acumuladas.",
            ]
        )
        chart.add_row(
            [
                ".noah vc banchannel #canal",
                "[Admin/Funcionarios] Excluye un canal del conteo.",
            ]
        )
        chart.add_row(
            [
                ".noah vc alertmessages #canal",
                "[Admin/Funcionarios] Define el canal unico para alertas de nivel.",
            ]
        )
        chart.add_row(
            [
                ".noah vc testalert",
                "[Admin/Funcionarios] Envia una alerta de prueba con tu usuario.",
            ]
        )
        await ctx.send(embed=chart.render())

    @vc.command()
    async def stats(
        ctx: commands.Context,
        member: discord.Member | None = None,
    ) -> None:
        target = member or ctx.author
        context = get_bot_context(ctx.bot)
        stats_data = context.voice_manager.get_user_stats(target.id)

        if stats_data is None:
            await ctx.send(f"🔇 {target.display_name} todavía no tiene stats de voz.")
            return

        table = EmbedTable(
            headers=["Campo", "Valor"],
            title=f"🎙️ Voice Stats - {target.display_name}",
            color=discord.Color.teal(),
        )
        table.add_row(["Usuario", target.mention])
        table.add_row(
            [
                "Tiempo total",
                _format_minutes(int(stats_data.get("effective_total_minutes", 0))),
            ]
        )
        table.add_row(["Sesiones", str(int(stats_data.get("sessions", 0)))])
        table.add_row(
            [
                "Estado",
                "Conectado" if stats_data.get("is_connected") else "Desconectado",
            ]
        )

        if stats_data.get("is_connected"):
            table.add_row(
                [
                    "Sesion actual",
                    _format_minutes(int(stats_data.get("current_session_minutes", 0))),
                ]
            )

        active_session = stats_data.get("active_session") or {}
        current_channel_name = active_session.get("channel_name")
        last_channel_name = stats_data.get("last_channel_name")
        table.add_row(["Ultimo canal", current_channel_name or last_channel_name or "-"])
        table.add_row(["Ultima entrada", _format_timestamp(stats_data.get("last_joined_at"))])
        table.add_row(["Ultima salida", _format_timestamp(stats_data.get("last_left_at"))])

        level_data = stats_data.get("level_data")
        if level_data:
            table.add_row(["Nivel", str(level_data["level"])])
            table.add_row(
                [
                    "Req. por nivel",
                    _format_hours(level_data.get("hours_per_level")),
                ]
            )
            table.add_row(
                [
                    "Progreso",
                    (
                        f"{_format_minutes(int(level_data['progress_minutes']))} / "
                        f"{_format_minutes(int(level_data['minutes_per_level']))} "
                        f"({int(level_data['progress_percent'])}%)"
                    ),
                ]
            )
            table.add_row(
                [
                    "Siguiente nivel",
                    f"En {_format_minutes(int(level_data['remaining_minutes']))}",
                ]
            )

        await ctx.send(embed=table.render())

    @vc.command()
    async def summary(
        ctx: commands.Context,
        member: discord.Member | None = None,
    ) -> None:
        target = member or ctx.author
        context = get_bot_context(ctx.bot)
        stats_data = context.voice_manager.get_user_stats(target.id)

        if stats_data is None:
            await ctx.send(f"🔇 {target.display_name} todavía no tiene stats de voz.")
            return

        total_minutes = int(stats_data.get("effective_total_minutes", 0))
        level_data = stats_data.get("level_data")
        embed = discord.Embed(
            title=f"🎙️ Voice Summary - {target.display_name}",
            color=discord.Color.teal(),
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        if level_data:
            embed.description = "\n".join(
                [
                    f"Nivel: `{level_data['level']}`",
                    f"Siguiente nivel en: `{_format_minutes(int(level_data['remaining_minutes']))}`",
                    f"Horas totales: `{_format_total_hours(total_minutes)}`",
                ]
            )
        else:
            embed.description = "\n".join(
                [
                    "Nivel: `Sistema desactivado`",
                    "Siguiente nivel en: `-`",
                    f"Horas totales: `{_format_total_hours(total_minutes)}`",
                ]
            )

        await ctx.send(embed=embed)

    @vc.command()
    async def top(ctx: commands.Context, limit: int = 10) -> None:
        context = get_bot_context(ctx.bot)
        safe_limit = max(1, min(limit, 15))
        guild_member_ids = {member.id for member in ctx.guild.members}
        leveling_config = context.voice_manager.get_leveling_config()
        top_users = context.voice_manager.get_top_users(
            safe_limit,
            guild_member_ids=guild_member_ids,
        )

        if not top_users:
            await ctx.send("📭 Todavía no hay stats de voz registradas.")
            return

        table = EmbedTable(
            headers=["Ranking"],
            title="🏆 Voice Chat Ranking",
            color=discord.Color.gold(),
        )

        for position, user_data in enumerate(top_users, start=1):
            member = ctx.guild.get_member(int(user_data["user_id"]))
            username = member.mention if member else user_data.get("display_name", "Unknown")
            total_time = _format_minutes(int(user_data.get("effective_total_minutes", 0)))
            row_text = f"`#{position}` {username} • `{total_time}`"

            if leveling_config.get("enabled"):
                level_data = user_data.get("level_data")
                level = str(level_data["level"]) if level_data else "1"
                row_text += f" • `Lv {level}`"

            table.add_row([row_text])

        await ctx.send(embed=table.render())

    @vc.command()
    async def active(ctx: commands.Context) -> None:
        context = get_bot_context(ctx.bot)
        active_sessions = context.voice_manager.get_active_sessions(guild_id=ctx.guild.id)

        if not active_sessions:
            await ctx.send("🔇 No hay nadie conectado a voice chat ahora mismo.")
            return

        table = EmbedTable(
            headers=["Usuario", "Canal", "Tiempo"],
            title="🔊 Usuarios en Voice Chat",
            color=discord.Color.green(),
        )

        for session in active_sessions:
            member = ctx.guild.get_member(int(session["user_id"]))
            username = member.mention if member else session.get("display_name", "Unknown")
            table.add_row(
                [
                    username,
                    session.get("channel_name") or "-",
                    _format_minutes(int(session.get("elapsed_minutes", 0))),
                ]
            )

        await ctx.send(embed=table.render())

    @vc.command()
    async def setminutes(
        ctx: commands.Context,
        member: discord.Member,
        minutes: int,
    ) -> None:
        if not _can_manage_vc_settings(ctx.author):
            await ctx.send(
                "❌ Solo administradores o el rol `Funcionarios` pueden usar este comando."
            )
            return

        context = get_bot_context(ctx.bot)
        result = context.voice_manager.set_total_minutes(
            member.id,
            member.display_name,
            minutes,
            guild_id=ctx.guild.id,
            guild_name=ctx.guild.name,
        )

        message = (
            f"✅ Tiempo total de {member.mention} fijado en "
            f"`{_format_minutes(int(result['total_minutes']))}`."
        )

        level_data = result.get("level_data")
        if level_data:
            message += f" Nivel actual: `{level_data['level']}`."

        await ctx.send(message)

    @vc.command()
    async def startleveling(ctx: commands.Context, hours_requisite: float) -> None:
        if not _can_manage_vc_settings(ctx.author):
            await ctx.send(
                "❌ Solo administradores o el rol `Funcionarios` pueden usar este comando."
            )
            return

        if hours_requisite <= 0:
            await ctx.send("❌ Las horas requeridas deben ser mayores que 0.")
            return

        context = get_bot_context(ctx.bot)
        config = context.voice_manager.configure_leveling(hours_requisite)

        await ctx.send(
            "✅ Sistema de niveles activado. "
            f"Cada `{_format_hours(config['hours_per_level'])}` acumuladas "
            f"se sube un nivel."
        )

    @vc.command()
    async def banchannel(
        ctx: commands.Context,
        channel: discord.abc.GuildChannel,
    ) -> None:
        if not _can_manage_vc_settings(ctx.author):
            await ctx.send(
                "❌ Solo administradores o el rol `Funcionarios` pueden usar este comando."
            )
            return

        if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
            await ctx.send("❌ Debes mencionar un canal de voz o stage.")
            return

        context = get_bot_context(ctx.bot)
        context.voice_manager.ban_channel(
            channel.id,
            channel.name,
            guild_id=ctx.guild.id,
            guild_name=ctx.guild.name,
        )

        for member in channel.members:
            if not member.voice:
                continue
            if not _is_listening(member.voice):
                continue

            context.voice_manager.handle_voice_state_change(
                member.id,
                member.display_name,
                before_channel_id=channel.id,
                before_channel_name=channel.name,
                after_channel_id=None,
                after_channel_name=None,
                guild_id=ctx.guild.id,
                guild_name=ctx.guild.name,
            )

        await ctx.send(
            f"🚫 El canal {channel.mention} ha sido baneado del conteo de voice stats."
        )

    @vc.command()
    async def alertmessages(
        ctx: commands.Context,
        channel: discord.TextChannel,
    ) -> None:
        if not _can_manage_vc_settings(ctx.author):
            await ctx.send(
                "❌ Solo administradores o el rol `Funcionarios` pueden usar este comando."
            )
            return

        context = get_bot_context(ctx.bot)
        context.voice_manager.set_alert_channel(
            ctx.guild.id,
            ctx.guild.name,
            channel.id,
            channel.name,
        )

        await ctx.send(
            f"✅ Las alertas de nivel de voice chat ahora se enviaran en {channel.mention}."
        )

    @vc.command()
    async def testalert(ctx: commands.Context) -> None:
        if not _can_manage_vc_settings(ctx.author):
            await ctx.send(
                "❌ Solo administradores o el rol `Funcionarios` pueden usar este comando."
            )
            return

        context = get_bot_context(ctx.bot)
        stats_data = context.voice_manager.get_user_stats(ctx.author.id)
        level_data = stats_data.get("level_data") if stats_data else None
        level = int(level_data["level"]) if level_data else 1

        sent = await _send_level_alert(
            ctx.guild,
            context.voice_manager,
            ctx.author,
            ctx.bot.user,
            level,
        )
        if not sent:
            await ctx.send(
                "❌ No hay un canal de alertas configurado. Usa `.noah vc alertmessages #canal`."
            )
            return

        await ctx.send("✅ Alerta de prueba enviada.")

    @vc.command()
    async def alerttype(ctx: commands.Context, alert_type: str) -> None:
        sanitized_alert_type = alert_type.strip().lower()
        if sanitized_alert_type not in {"montadito", "noah"}:
            await ctx.send("❌ Debes elegir `montadito` o `noah`.")
            return

        context = get_bot_context(ctx.bot)
        context.voice_manager.set_user_alert_type(
            ctx.author.id,
            ctx.author.display_name,
            sanitized_alert_type,
            guild_id=ctx.guild.id,
            guild_name=ctx.guild.name,
        )

        if sanitized_alert_type == "noah":
            await ctx.send(
                "✅ Tu alerta de nivel ahora usara el formato `noah`."
            )
            return

        await ctx.send(
            "✅ Tu alerta de nivel ahora usara el formato `montadito`."
        )
