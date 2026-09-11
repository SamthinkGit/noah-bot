import asyncio

import discord
from discord.ext import commands

from noah_bot.modules.autobump import (
    BUMP_COMMAND_NAME,
    BUMP_RETRY_SECONDS,
    DISBOARD_APPLICATION_ID,
    discord_timestamp,
    format_delay,
    next_bump_timestamp,
    random_bump_delay,
    seconds_until,
)
from noah_bot.modules.bot_context import get_bot_context
from noah_bot.modules.slash_command import (
    SlashCommandError,
    resolve_application_command,
    trigger_slash_command,
)


def _build_autobump_help_embed() -> discord.Embed:
    embed = discord.Embed(
        title="Autobump",
        description=(
            "Bumpea el servidor en Disboard usando el `/bump` real y tu token de "
            "Autogami, cada 3-4 horas aleatorias."
        ),
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name=".noah autobump",
        value="Activa el autobump en este canal y hace el primer bump al momento. Solo admins.",
        inline=False,
    )
    embed.add_field(
        name=".noah autobump stop",
        value="Desactiva el autobump de este servidor. Solo admins.",
        inline=False,
    )
    embed.add_field(
        name=".noah autobump status",
        value="Muestra si el autobump está activo y cuándo toca el siguiente.",
        inline=False,
    )
    embed.add_field(
        name=".noah autobump debug",
        value="Muestra qué responde Discord al buscar `/bump` con tu token. Solo admins.",
        inline=False,
    )
    return embed


def _is_autobump_admin(author: discord.abc.User) -> bool:
    return isinstance(author, discord.Member) and author.guild_permissions.administrator


async def _resolve_channel(
    bot: commands.Bot,
    channel_id: int,
) -> discord.abc.Messageable | None:
    channel = bot.get_channel(channel_id)
    if channel is not None:
        return channel

    try:
        return await bot.fetch_channel(channel_id)
    except (discord.HTTPException, discord.Forbidden, discord.NotFound):
        return None


async def _announce(bot: commands.Bot, channel_id: int, content: str) -> None:
    channel = await _resolve_channel(bot, channel_id)
    if channel is None:
        return

    try:
        await channel.send(content)
    except (discord.HTTPException, discord.Forbidden):
        pass


async def _send_bump(
    bot: commands.Bot,
    guild_id: int,
    channel_id: int,
    user_id: int,
) -> tuple[int, str]:
    context = get_bot_context(bot)
    token = context.autogami_tokens.get_token(user_id)
    if token is None:
        raise SlashCommandError(
            "el usuario configurado ya no tiene token de Autogami sincronizado."
        )

    return await asyncio.to_thread(
        trigger_slash_command,
        BUMP_COMMAND_NAME,
        DISBOARD_APPLICATION_ID,
        token,
        str(guild_id),
        str(channel_id),
    )


async def _autobump_loop(bot: commands.Bot, guild_id: int) -> None:
    context = get_bot_context(bot)
    config = context.autogami_tokens.get_autobump(guild_id)
    if config is None:
        return

    pending_seconds = seconds_until(config["next_bump_at"])
    if pending_seconds > 0:
        await asyncio.sleep(pending_seconds)

    while True:
        config = context.autogami_tokens.get_autobump(guild_id)
        if config is None:
            return

        channel_id = config["channel_id"]
        user_id = config["user_id"]

        try:
            status, body = await _send_bump(bot, guild_id, channel_id, user_id)
        except SlashCommandError as exc:
            context.autogami_tokens.clear_autobump(guild_id)
            await _announce(
                bot,
                channel_id,
                f"❌ Autobump detenido: {exc} Vuelve a activarlo con `.noah autobump`.",
            )
            return
        except Exception as exc:
            status, body = 0, str(exc)

        if 200 <= status < 300:
            delay_seconds = random_bump_delay()
            await _announce(
                bot,
                channel_id,
                "🚀 Autobump enviado a Disboard. Siguiente bump "
                f"{discord_timestamp(delay_seconds)} (~{format_delay(delay_seconds)}).",
            )
        else:
            delay_seconds = float(BUMP_RETRY_SECONDS)
            error_body = body[:200] if body else "sin respuesta"
            await _announce(
                bot,
                channel_id,
                f"⚠️ El autobump falló (HTTP {status}): {error_body}. "
                f"Reintento en {format_delay(delay_seconds)}.",
            )

        context.autogami_tokens.set_autobump_next_bump(
            guild_id,
            next_bump_timestamp(delay_seconds),
        )
        await asyncio.sleep(delay_seconds)


def _cancel_autobump_task(bot: commands.Bot, guild_id: int) -> None:
    context = get_bot_context(bot)
    task = context.autobump_tasks.pop(guild_id, None)
    if task is not None and not task.done():
        task.cancel()


def _ensure_autobump_task(bot: commands.Bot, guild_id: int) -> None:
    context = get_bot_context(bot)
    task = context.autobump_tasks.get(guild_id)
    if task is not None and not task.done():
        return

    context.autobump_tasks[guild_id] = asyncio.create_task(_autobump_loop(bot, guild_id))


def register_autobump_commands(bot: commands.Bot, noah_group: commands.Group) -> None:
    @bot.listen("on_ready")
    async def _resume_autobump_loops() -> None:
        context = get_bot_context(bot)
        for guild_id in context.autogami_tokens.get_autobump_guild_ids():
            _ensure_autobump_task(bot, guild_id)

    @noah_group.group(invoke_without_command=True)
    async def autobump(ctx: commands.Context) -> None:
        if ctx.guild is None:
            await ctx.send("❌ Este comando solo funciona dentro de un servidor.")
            return

        if not _is_autobump_admin(ctx.author):
            await ctx.send("❌ Solo los administradores pueden usar el autobump.")
            return

        context = get_bot_context(ctx.bot)
        if context.autogami_tokens.get_token(ctx.author.id) is None:
            await ctx.send(
                "❌ Necesitas un token sincronizado. Usa `.noah autogami sync` primero."
            )
            return

        _cancel_autobump_task(ctx.bot, ctx.guild.id)
        context.autogami_tokens.set_autobump(ctx.guild.id, ctx.channel.id, ctx.author.id)

        try:
            status, body = await _send_bump(
                ctx.bot,
                ctx.guild.id,
                ctx.channel.id,
                ctx.author.id,
            )
        except Exception as exc:
            context.autogami_tokens.clear_autobump(ctx.guild.id)
            await ctx.send(f"❌ No he podido activar el autobump: {exc}")
            return

        if not 200 <= status < 300:
            context.autogami_tokens.clear_autobump(ctx.guild.id)
            error_body = body[:300] if body else "sin respuesta"
            await ctx.send(
                f"❌ El primer bump falló con estado HTTP {status}: {error_body}"
            )
            return

        delay_seconds = random_bump_delay()
        context.autogami_tokens.set_autobump_next_bump(
            ctx.guild.id,
            next_bump_timestamp(delay_seconds),
        )
        _ensure_autobump_task(ctx.bot, ctx.guild.id)
        await ctx.send(
            f"✅ Autobump activado en este canal como {ctx.author.mention}. "
            "Primer `/bump` enviado y el siguiente llegará "
            f"{discord_timestamp(delay_seconds)} (~{format_delay(delay_seconds)})."
        )

    @autobump.command()
    async def help(ctx: commands.Context) -> None:
        await ctx.send(embed=_build_autobump_help_embed())

    @autobump.command()
    async def stop(ctx: commands.Context) -> None:
        if ctx.guild is None:
            await ctx.send("❌ Este comando solo funciona dentro de un servidor.")
            return

        if not _is_autobump_admin(ctx.author):
            await ctx.send("❌ Solo los administradores pueden usar el autobump.")
            return

        context = get_bot_context(ctx.bot)
        _cancel_autobump_task(ctx.bot, ctx.guild.id)
        if not context.autogami_tokens.clear_autobump(ctx.guild.id):
            await ctx.send("ℹ️ El autobump no estaba activo en este servidor.")
            return

        await ctx.send("🛑 Autobump desactivado.")

    @autobump.command()
    async def debug(ctx: commands.Context) -> None:
        if ctx.guild is None:
            await ctx.send("❌ Este comando solo funciona dentro de un servidor.")
            return

        if not _is_autobump_admin(ctx.author):
            await ctx.send("❌ Solo los administradores pueden usar el autobump.")
            return

        context = get_bot_context(ctx.bot)
        token = context.autogami_tokens.get_token(ctx.author.id)
        if token is None:
            await ctx.send(
                "❌ No tienes un token sincronizado. Usa `.noah autogami sync` primero."
            )
            return

        command, attempts = await asyncio.to_thread(
            resolve_application_command,
            BUMP_COMMAND_NAME,
            DISBOARD_APPLICATION_ID,
            token,
            str(ctx.guild.id),
            str(ctx.channel.id),
        )
        header = (
            f"🔎 Buscando `/{BUMP_COMMAND_NAME}` como {ctx.author.mention} "
            f"(app `{DISBOARD_APPLICATION_ID}`):"
        )
        detail = "\n".join(f"- {attempt}" for attempt in attempts)
        verdict = (
            f"✅ Resuelto: id `{command['id']}`, version `{command['version']}`."
            if command is not None
            else "❌ Ninguna fuente devolvió el comando."
        )
        await ctx.send(f"{header}\n{detail}\n{verdict}"[:1900])

    @autobump.command()
    async def status(ctx: commands.Context) -> None:
        if ctx.guild is None:
            await ctx.send("❌ Este comando solo funciona dentro de un servidor.")
            return

        context = get_bot_context(ctx.bot)
        config = context.autogami_tokens.get_autobump(ctx.guild.id)
        if config is None:
            await ctx.send("ℹ️ El autobump no está activo en este servidor.")
            return

        channel_id = config["channel_id"]
        user_id = config["user_id"]
        remaining_seconds = seconds_until(config["next_bump_at"])
        await ctx.send(
            f"✅ Autobump activo en <#{channel_id}> como <@{user_id}>. "
            f"Siguiente bump {discord_timestamp(remaining_seconds)} "
            f"(~{format_delay(remaining_seconds)})."
        )
