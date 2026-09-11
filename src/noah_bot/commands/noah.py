import asyncio
import os
import re
import time

import discord
from discord.ext import commands

from noah_bot.commands.autobump import register_autobump_commands
from noah_bot.commands.autogami import register_autogami_commands
from noah_bot.commands.gochi import register_gotchi_commands
from noah_bot.commands.relics import register_relics_commands
from noah_bot.commands.tts import register_tts_commands
from noah_bot.commands.vc_stats import register_vc_stats_commands
from noah_bot.commands.waifu import register_waifu_commands
from noah_bot.modules.bot_context import get_bot_context
from noah_bot.modules.daily_stats import overlap_seconds_for_current_day
from noah_bot.modules.discord_formatter import (
    EmbedTable,
    RARITY_COLORS,
    RARITY_DISPLAY,
    RARITY_SYMBOLS,
    _parse_embed_metadata,
    render_embeds_to_png,
)


def _truncate_discord_message(content: str) -> str:
    if len(content) > 1900:
        return content[:1900] + "..."
    return content


WAIFUGAMI_BOT_USER_ID = 722418701852344391
HUSBANDO_TRIGGER_PATTERN = re.compile(
    r"husbando appeared",
    re.IGNORECASE,
)
USER_FLAG_PATTERN = re.compile(r"(?<!\S)-user(?!\S)")


def _is_husbando_spawn_message(message: discord.Message) -> bool:
    if message.author.id != WAIFUGAMI_BOT_USER_ID or not message.embeds:
        return False

    first_embed = message.embeds[0]
    description = (first_embed.description or "").strip()
    return bool(HUSBANDO_TRIGGER_PATTERN.search(description))


def _resolve_daily_target(
    ctx: commands.Context,
    args: str,
) -> discord.Member:
    sanitized_args = args.strip()
    if not sanitized_args:
        return ctx.author

    if not USER_FLAG_PATTERN.search(sanitized_args):
        raise ValueError("❌ Usa `.noah daily` o `.noah daily -user @user`.")

    if not ctx.message.mentions:
        raise ValueError("❌ Debes mencionar un usuario válido después de `-user`.")

    target = ctx.message.mentions[0]
    if not isinstance(target, discord.Member):
        raise ValueError("❌ El usuario indicado debe pertenecer a este servidor.")

    return target


def _format_daily_vc(seconds: int) -> str:
    sanitized_seconds = max(0, int(seconds))
    hours, remainder = divmod(sanitized_seconds, 3600)
    minutes, secs = divmod(remainder, 60)

    if hours > 0:
        return f"{hours}h {minutes}m"
    if minutes > 0:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _build_metric_lines(
    guild: discord.Guild,
    user_stats: dict[int, dict],
    metric: str,
    limit: int,
    formatter,
) -> str:
    ranked = [
        payload
        for payload in user_stats.values()
        if int(payload.get(metric, 0)) > 0
    ]
    ranked.sort(
        key=lambda payload: (
            int(payload.get(metric, 0)),
            int(payload.get("user_id", 0)),
        ),
        reverse=True,
    )

    if not ranked:
        return "Sin datos todavía."

    lines: list[str] = []
    for position, payload in enumerate(ranked[:limit], start=1):
        user_id = int(payload.get("user_id", 0))
        member = guild.get_member(user_id)
        username = (
            member.mention
            if member
            else payload.get("display_name", f"<@{user_id}>")
        )
        value = formatter(int(payload.get(metric, 0)))
        lines.append(f"`#{position}` {username} • `{value}`")

    return "\n".join(lines)


def _collect_daily_user_stats(
    ctx: commands.Context,
) -> dict[int, dict]:
    context = get_bot_context(ctx.bot)
    merged_stats = context.daily_stats.get_guild_user_stats(ctx.guild.id)

    active_sessions = context.voice_manager.get_active_sessions(guild_id=ctx.guild.id)
    for session in active_sessions:
        try:
            user_id = int(session["user_id"])
        except (KeyError, TypeError, ValueError):
            continue

        active_seconds = overlap_seconds_for_current_day(session.get("started_at"))
        if active_seconds <= 0:
            continue

        member = ctx.guild.get_member(user_id)
        display_name = (
            member.display_name
            if member is not None
            else str(session.get("display_name") or f"<@{user_id}>")
        )
        payload = merged_stats.setdefault(
            user_id,
            {
                "user_id": user_id,
                "display_name": display_name,
                "messages": 0,
                "vc_seconds": 0,
                "waifu_claims": 0,
                "autogami_uses": 0,
            },
        )
        payload["display_name"] = display_name
        payload["vc_seconds"] = int(payload.get("vc_seconds", 0)) + active_seconds

    return merged_stats


def _count_leaf_commands(group: commands.Group) -> int:
    return sum(
        1 for command in group.walk_commands() if not isinstance(command, commands.Group)
    )


def _format_noah_category_name(name: str) -> str:
    special_names = {
        "vc": "VC",
        "tts": "TTS",
    }
    return special_names.get(name, name.capitalize())


def _build_noah_command_stats_embed(bot: commands.Bot) -> discord.Embed:
    noah_group = bot.get_command("noah")
    if not isinstance(noah_group, commands.Group):
        raise RuntimeError("Noah group is not registered.")

    general_count = 0
    category_counts: list[tuple[str, int]] = []

    for command in noah_group.commands:
        if isinstance(command, commands.Group):
            category_counts.append(
                (
                    _format_noah_category_name(command.name),
                    _count_leaf_commands(command),
                )
            )
            continue

        general_count += 1

    category_counts.sort(key=lambda item: item[0])
    total_commands = general_count + sum(count for _, count in category_counts)

    embed = discord.Embed(
        title="Noah status",
        description="Comandos implementados actualmente en Noah.",
        color=discord.Color.green(),
    )

    embed.add_field(name="General", value=f"`{general_count}` comandos", inline=True)

    for category_name, count in category_counts:
        embed.add_field(
            name=category_name,
            value=f"`{count}` comandos",
            inline=True,
        )

    embed.add_field(
        name="Total",
        value=f"`{total_commands}` comandos",
        inline=False,
    )
    embed.set_footer(
        text="Conteo generado automaticamente desde los comandos registrados."
    )

    return embed


def register_noah_commands(bot: commands.Bot) -> None:
    @bot.group()
    async def noah(ctx: commands.Context) -> None:
        if ctx.invoked_subcommand is None:
            await ctx.send("Use `.noah help` to see commands.")

    @noah.command()
    async def ping(ctx: commands.Context) -> None:
        await ctx.send("Im alive! 🖤")

    @noah.command()
    async def areyouok(ctx: commands.Context) -> None:
        await ctx.send(embed=_build_noah_command_stats_embed(ctx.bot))

    @noah.command()
    async def kill(ctx: commands.Context) -> None:
        await ctx.send("💀 Noah is shutting down.")
        await asyncio.sleep(0.5)
        os._exit(0)

    @noah.command()
    async def help(ctx: commands.Context) -> None:
        chart = EmbedTable(headers=["Command", "Description"], title="Noah AI Commands")
        chart.add_row([".noah ask <question>", "Ask a question to Noah AI."])
        chart.add_row([".noah summary", "Summarize recent channel messages."])
        chart.add_row([".noah behonest <question>", "Ask Noah without filters."])
        chart.add_row(
            [".noah quote <user>", "Quote the replied message in a styled embed."]
        )
        chart.add_row([".noah merge", "Render all images from a replied message."])
        chart.add_row([".noah if <type>", "Invert embed rarity symbol and color."])
        chart.add_row([".noah ping", "Check if Noah is responsive."])
        chart.add_row(
            [".noah areyouok", "Muestra el total de comandos y por categoria."]
        )
        chart.add_row([".noah kill", "Stop the bot process immediately."])
        chart.add_row([".noah help", "Show Noah AI commands."])
        chart.add_row([".noah daily", "Resumen diario del servidor."])
        chart.add_row([".noah daily -user @user", "Resumen diario de un usuario."])
        chart.add_row([".noah gochi help", "Muestra los comandos de Noah Gochi."])
        chart.add_row([".noah autogami help", "Show Autogami sync commands."])
        chart.add_row([".noah vc help", "Show voice stats commands."])
        chart.add_row(
            [".waifuracer setemoji <emoji>", "Set your claim reaction emoji."]
        )
        chart.add_row([".waifuracer help", "Show waifuracer commands."])
        chart.add_row([".noah waifu help", "Show waifu battle commands"])
        chart.add_row([".noah relics help", "Muestra los comandos de noah relics."])
        chart.add_row([".steallist help", "Show steallist commands."])

        await ctx.send(embed=chart.render())

    @noah.command()
    async def daily(ctx: commands.Context, *, args: str = "") -> None:
        if ctx.guild is None:
            await ctx.send("❌ Este comando solo funciona dentro de un servidor.")
            return

        try:
            target_user = _resolve_daily_target(ctx, args)
        except ValueError as exc:
            await ctx.send(str(exc))
            return

        context = get_bot_context(ctx.bot)
        current_date = context.daily_stats.get_current_date()
        user_stats = _collect_daily_user_stats(ctx)

        if args.strip():
            payload = user_stats.get(
                target_user.id,
                {
                    "messages": 0,
                    "vc_seconds": 0,
                    "waifu_claims": 0,
                    "autogami_uses": 0,
                },
            )
            embed = discord.Embed(
                title=f"Daily Stats · {target_user.display_name}",
                description=f"Fecha: `{current_date}`",
                color=discord.Color.blurple(),
            )
            embed.set_thumbnail(url=target_user.display_avatar.url)
            embed.add_field(
                name="Mensajes",
                value=f"`{int(payload.get('messages', 0))}`",
                inline=True,
            )
            embed.add_field(
                name="VC hoy",
                value=f"`{_format_daily_vc(int(payload.get('vc_seconds', 0)))}`",
                inline=True,
            )
            embed.add_field(
                name="Waifus claimeadas",
                value=f"`{int(payload.get('waifu_claims', 0))}`",
                inline=True,
            )
            embed.add_field(
                name="Autogami",
                value=f"`{int(payload.get('autogami_uses', 0))}`",
                inline=True,
            )
            await ctx.send(embed=embed)
            return

        embed = discord.Embed(
            title="Daily Stats",
            description=f"Fecha: `{current_date}`",
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="Top mensajes",
            value=_build_metric_lines(
                ctx.guild,
                user_stats,
                "messages",
                5,
                lambda value: str(value),
            ),
            inline=False,
        )
        embed.add_field(
            name="Top VC hoy",
            value=_build_metric_lines(
                ctx.guild,
                user_stats,
                "vc_seconds",
                5,
                _format_daily_vc,
            ),
            inline=False,
        )
        embed.add_field(
            name="Top waifu claims",
            value=_build_metric_lines(
                ctx.guild,
                user_stats,
                "waifu_claims",
                5,
                lambda value: str(value),
            ),
            inline=False,
        )
        embed.add_field(
            name="Top autogami",
            value=_build_metric_lines(
                ctx.guild,
                user_stats,
                "autogami_uses",
                5,
                lambda value: str(value),
            ),
            inline=False,
        )
        await ctx.send(embed=embed)

    @noah.command()
    async def ask(ctx: commands.Context, *, question: str) -> None:
        context = get_bot_context(ctx.bot)
        await ctx.typing()

        try:
            response = context.ai_responder.ask(question)
        except Exception:
            await ctx.send("❌ Lo siento, hubo un error al procesar tu solicitud.")
            return

        await ctx.send(_truncate_discord_message(response))

    @noah.command()
    async def summary(ctx: commands.Context) -> None:
        context = get_bot_context(ctx.bot)
        await ctx.typing()

        messages = [message async for message in ctx.channel.history(limit=100)]
        text = [message.content for message in messages]

        try:
            summary_text = context.ai_responder.summarize(str(text))
        except Exception:
            await ctx.send("💀 Noah couldn't survive this summary.")
            return

        await ctx.send(_truncate_discord_message(summary_text))

    @noah.command()
    async def behonest(ctx: commands.Context, *, question: str) -> None:
        context = get_bot_context(ctx.bot)
        await ctx.typing()

        try:
            response = context.ai_responder.ask_without_filters(question)
        except Exception:
            await ctx.send("❌ Lo siento, hubo un error al procesar tu solicitud.")
            return

        await ctx.send(_truncate_discord_message(response))

    @noah.command()
    async def merge(ctx: commands.Context) -> None:
        await ctx.typing()
        if not ctx.message.reference:
            await ctx.send("❌ You must reply to a message containing embeds.")
            return

        try:
            replied_msg = await ctx.channel.fetch_message(
                ctx.message.reference.message_id
            )
        except discord.NotFound:
            await ctx.send("❌ Original message not found.")
            return

        buffer = render_embeds_to_png(replied_msg.embeds)
        if buffer is None:
            await ctx.send("❌ No images found in the embeds.")
            return

        await ctx.send(file=discord.File(buffer, filename="rendered_images.png"))

    @noah.command()
    async def quote(
        ctx: commands.Context, user: discord.Member | discord.User | None = None
    ) -> None:
        if user is None:
            await ctx.send("❌ Use `.noah quote <user>` while replying to a message.")
            return

        if not ctx.message.reference:
            await ctx.send("❌ You must reply to a message to quote it.")
            return

        try:
            replied_msg = await ctx.channel.fetch_message(
                ctx.message.reference.message_id
            )
        except discord.NotFound:
            await ctx.send("❌ Original message not found.")
            return

        content = replied_msg.content.strip()
        if not content:
            await ctx.send("❌ The replied message has no text content to quote.")
            return

        created_at = replied_msg.created_at.strftime("%d/%m/%Y")
        embed_color = (
            user.color
            if isinstance(user, discord.Member) and user.color.value
            else discord.Color.from_rgb(245, 187, 87)
        )

        quoted_lines = "\n".join(
            f"> {line}" if line.strip() else ">"
            for line in content.splitlines()
        )

        embed = discord.Embed(
            description=f"{quoted_lines}\n\n*{user.mention} - {created_at}*",
            color=embed_color,
        )
        embed.set_thumbnail(url=user.display_avatar.url)

        sent_message = await ctx.send(embed=embed)

        try:
            await ctx.message.delete()
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            pass

        try:
            await replied_msg.delete()
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            pass

        for emoji in ("⬆️", "⬇️"):
            try:
                await sent_message.add_reaction(emoji)
            except discord.HTTPException:
                pass

    @noah.command(name="if")
    async def invert_rarity(ctx: commands.Context, rarity: str) -> None:
        await ctx.typing()
        rarity = rarity.lower()

        if rarity not in RARITY_COLORS:
            await ctx.send("❌ Unknown rarity type.")
            return

        if not ctx.message.reference:
            await ctx.send("❌ You must reply to a message containing an embed.")
            return

        try:
            replied_msg = await ctx.channel.fetch_message(
                ctx.message.reference.message_id
            )
        except Exception:
            await ctx.send("❌ Original message not found.")
            return

        if not replied_msg.embeds:
            await ctx.send("❌ No embed found in the replied message.")
            return

        original = replied_msg.embeds[0]
        meta = _parse_embed_metadata(original)

        new_embed = discord.Embed(
            title=original.title,
            description=original.description,
            color=RARITY_COLORS[rarity],
            url=original.url,
        )

        if new_embed.description:
            new_name = RARITY_DISPLAY[rarity]
            new_symbol = RARITY_SYMBOLS[rarity]

            new_embed.description = re.sub(
                r"(Type:\s*)(Alpha|Beta|Gamma|Delta|Sigma|Epsilon|Zeta|Omega)\s*\([^)]*\)",
                rf"\1{new_name} ({new_symbol})",
                new_embed.description,
                count=1,
            )

            if meta.get("rarity"):
                old_symbol = meta["rarity"]
                new_embed.description = new_embed.description.replace(
                    f"({old_symbol})", f"({new_symbol})"
                )

        if original.image and original.image.url:
            new_embed.set_image(url=original.image.url)

        if original.thumbnail and original.thumbnail.url:
            new_embed.set_thumbnail(url=original.thumbnail.url)

        if original.footer and original.footer.text:
            new_embed.set_footer(
                text=original.footer.text,
                icon_url=original.footer.icon_url,
            )

        if original.author and original.author.name:
            new_embed.set_author(
                name=original.author.name,
                icon_url=original.author.icon_url,
                url=original.author.url,
            )

        await ctx.send(embed=new_embed)

    @noah.command()
    async def setlist(ctx: commands.Context) -> None:
        if not ctx.author.guild_permissions.administrator:
            await ctx.send("❌ Only administrators can use this command.")
            return

        members = ctx.message.mentions
        if not members:
            await ctx.send("❌ You must mention at least one user.")
            return

        guild = ctx.guild
        role = discord.utils.get(guild.roles, name="Lista")

        if not role:
            await ctx.send("❌ Role 'Lista' not found.")
            return

        target_ids = {member.id for member in members}
        failed = []

        await ctx.send("Fetching Users...")

        async for member in guild.fetch_members(limit=None):
            if member.id not in target_ids:
                try:
                    await member.remove_roles(role)
                except discord.Forbidden:
                    failed.append(member.display_name)

        for member in members:
            if role not in member.roles:
                try:
                    await member.add_roles(role)
                except discord.Forbidden:
                    failed.append(member.display_name)

        message = "✅ Lista updated.\n"
        if failed:
            message += f"\n⚠ Could not modify: {', '.join(failed)}"

        await ctx.send(message)

    @bot.listen("on_message")
    async def _handle_husbando_spawn_timer(message: discord.Message) -> None:
        if message.author.bot is False:
            return

        if message.guild is None or not _is_husbando_spawn_message(message):
            return

        context = get_bot_context(bot)
        context.latest_time_it = time.time()

    @bot.listen("on_message")
    async def _track_daily_messages(message: discord.Message) -> None:
        if message.author.bot or message.guild is None:
            return

        if not isinstance(message.author, discord.Member):
            return

        context = get_bot_context(bot)
        context.daily_stats.increment_messages(
            message.guild.id,
            message.guild.name,
            message.author.id,
            message.author.display_name,
        )

    register_waifu_commands(noah)
    register_gotchi_commands(noah)
    register_autogami_commands(bot, noah)
    register_autobump_commands(bot, noah)
    register_relics_commands(bot, noah)
    register_tts_commands(bot, noah)
    register_vc_stats_commands(bot, noah)
