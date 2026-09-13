import asyncio

import discord
from discord.ext import commands

from noah_bot.modules.bot_context import get_bot_context
from noah_bot.modules.discord_formatter import EmbedTable
from noah_bot.modules.spotify_player import (
    PlaybackState,
    SpotifyError,
    SpotifyGuildState,
    TrackInfo,
    format_ms,
)


MIRROR_POLL_SECONDS = 4.0
DRIFT_TOLERANCE_MS = 4000
SYNC_OFFSET_MS = 1500
CONFIRM_DELETE_AFTER = 20.0


def _build_spotify_help_embed() -> discord.Embed:
    embed = discord.Embed(
        title="🎧 Noah Spotify",
        description=(
            "Reproduce el audio real de tu Spotify en el canal de voz. **No** "
            "transfiere la reproduccion: suena en el bot y en tu PC/Alexa a la "
            "vez. Solo admins."
        ),
        color=discord.Color.green(),
    )
    embed.add_field(
        name=".noah spotify unlock <password>",
        value=(
            "Te da acceso a los comandos de Spotify. Noah borra tu mensaje al "
            "momento para que no se vea la password."
        ),
        inline=False,
    )
    embed.add_field(
        name=".noah spotify lock",
        value="Te quita el acceso a ti mismo.",
        inline=False,
    )
    embed.add_field(
        name=".noah spotify auth",
        value=(
            "Con el `credentials.json` adjunto, en cualquier canal. Noah guarda "
            "las credenciales y borra el mensaje con el fichero."
        ),
        inline=False,
    )
    embed.add_field(
        name=".noah spotify login",
        value="Fuerza reabrir la sesion de Spotify con las credenciales guardadas.",
        inline=False,
    )
    embed.add_field(
        name=".noah spotify join / leave",
        value="Mete o saca a Noah de tu canal de voz.",
        inline=False,
    )
    embed.add_field(
        name=".noah spotify mirror",
        value=(
            "Activa/desactiva el modo espejo: Noah sigue lo que suena en tu "
            f"cuenta, comprobandolo cada {int(MIRROR_POLL_SECONDS)}s."
        ),
        inline=False,
    )
    embed.add_field(
        name=".noah spotify play <link>",
        value="Reproduce un track suelto (link o `spotify:track:...`).",
        inline=False,
    )
    embed.add_field(
        name=".noah spotify stop",
        value="Para lo que este sonando en Noah y apaga el espejo.",
        inline=False,
    )
    embed.add_field(
        name=".noah spotify now",
        value="Muestra que suena en Noah y que suena en tu cuenta.",
        inline=False,
    )
    embed.add_field(
        name=".noah spotify vol <0-200>",
        value="Ajusta el volumen de Noah (no toca el de Spotify).",
        inline=False,
    )
    embed.add_field(
        name=".noah spotify status",
        value="Estado de las credenciales, la sesion y el espejo.",
        inline=False,
    )
    return embed


async def _delete_message(ctx: commands.Context) -> bool:
    try:
        await ctx.message.delete()
        return True
    except (discord.Forbidden, discord.NotFound, discord.HTTPException):
        return False


async def _ensure_admin(ctx: commands.Context) -> bool:
    context = get_bot_context(ctx.bot)

    if context.spotify_access.is_allowed(ctx.author.id):
        return True

    await ctx.send(
        "🔒 Necesitas desbloquear Spotify: `.noah spotify unlock <password>`."
    )
    return False


def _get_state(bot: commands.Bot, guild_id: int) -> SpotifyGuildState:
    context = get_bot_context(bot)
    state = context.spotify_states.get(guild_id)

    if state is None:
        state = SpotifyGuildState()
        context.spotify_states[guild_id] = state

    return state


def _stop_playback(
    state: SpotifyGuildState,
    voice_client: discord.VoiceClient | None,
) -> None:
    if voice_client is not None and (voice_client.is_playing() or voice_client.is_paused()):
        voice_client.stop()

    if state.playback is not None:
        state.playback.close()
        state.playback = None

    state.paused = False


def _cancel_mirror(bot: commands.Bot, guild_id: int) -> None:
    context = get_bot_context(bot)
    task = context.spotify_mirror_tasks.pop(guild_id, None)

    if task is not None and not task.done():
        task.cancel()

    state = context.spotify_states.get(guild_id)
    if state is not None:
        state.mirror = False


async def _notify(bot: commands.Bot, state: SpotifyGuildState, content: str) -> None:
    if not state.text_channel_id:
        return

    channel = bot.get_channel(state.text_channel_id)
    if channel is None:
        return

    try:
        await channel.send(content)
    except (discord.HTTPException, discord.Forbidden):
        pass


async def _start_track(
    bot: commands.Bot,
    guild: discord.Guild,
    uri: str,
    position_ms: int = 0,
) -> TrackInfo:
    context = get_bot_context(bot)
    state = _get_state(bot, guild.id)
    voice_client = guild.voice_client

    if voice_client is None or not voice_client.is_connected():
        raise SpotifyError("Noah no esta en un canal de voz. Usa `.noah spotify join`.")

    playback = await asyncio.to_thread(
        context.spotify_player.open_track,
        uri,
        position_ms,
    )

    _stop_playback(state, voice_client)
    state.playback = playback
    voice_client.play(playback.source(state.volume), after=lambda _: playback.close())
    return playback.info


async def _apply_remote_state(
    bot: commands.Bot,
    guild: discord.Guild,
    state: SpotifyGuildState,
    remote: PlaybackState | None,
) -> None:
    voice_client = guild.voice_client

    if remote is None or remote.uri is None:
        _stop_playback(state, voice_client)
        return

    if not remote.is_track:
        return

    if not remote.is_playing:
        if state.playback is not None and voice_client.is_playing():
            voice_client.pause()
            state.playback.pause()
            state.paused = True
        return

    if state.playback is not None and state.playback.info.uri == remote.uri:
        if state.paused:
            state.playback.resume()
            voice_client.resume()
            state.paused = False

        if abs(state.playback.elapsed_ms - remote.progress_ms) <= DRIFT_TOLERANCE_MS:
            return

    info = await _start_track(
        bot,
        guild,
        remote.uri,
        remote.progress_ms + SYNC_OFFSET_MS,
    )
    await _notify(bot, state, f"🎧 **{info.label}**")


async def _mirror_loop(bot: commands.Bot, guild_id: int) -> None:
    context = get_bot_context(bot)
    state = _get_state(bot, guild_id)

    while True:
        await asyncio.sleep(MIRROR_POLL_SECONDS)

        guild = bot.get_guild(guild_id)
        voice_client = guild.voice_client if guild is not None else None

        if guild is None or voice_client is None or not voice_client.is_connected():
            state.mirror = False
            context.spotify_mirror_tasks.pop(guild_id, None)
            return

        try:
            remote = await asyncio.to_thread(context.spotify_player.current_playback)
        except SpotifyError:
            continue

        try:
            await _apply_remote_state(bot, guild, state, remote)
        except SpotifyError as exc:
            await _notify(bot, state, f"⚠️ Spotify: `{exc}`")


def register_spotify_commands(bot: commands.Bot, noah_group: commands.Group) -> None:
    @noah_group.group()
    async def spotify(ctx: commands.Context) -> None:
        if ctx.invoked_subcommand is None:
            await ctx.send(embed=_build_spotify_help_embed())

    @spotify.command(name="help")
    async def spotify_help(ctx: commands.Context) -> None:
        await ctx.send(embed=_build_spotify_help_embed())

    @spotify.command()
    async def auth(ctx: commands.Context) -> None:
        if not await _ensure_admin(ctx):
            return

        if not ctx.message.attachments:
            await ctx.send(
                "❌ Adjunta el `credentials.json` que genero `auth_local.py` en tu PC."
            )
            return

        context = get_bot_context(ctx.bot)

        try:
            raw = await ctx.message.attachments[0].read()
        except discord.HTTPException as exc:
            await _delete_message(ctx)
            await ctx.send(f"❌ No pude leer el adjunto: `{exc}`", delete_after=CONFIRM_DELETE_AFTER)
            return

        deleted = await _delete_message(ctx)

        try:
            username = await asyncio.to_thread(
                context.spotify_player.save_credentials,
                raw,
            )
        except SpotifyError as exc:
            await ctx.send(f"❌ {exc}", delete_after=CONFIRM_DELETE_AFTER)
            return

        warning = (
            ""
            if deleted
            else "\n⚠️ No pude borrar tu mensaje (me falta `Gestionar mensajes`), borralo tu."
        )
        await ctx.send(
            f"✅ Credenciales guardadas para **{username}**. "
            f"Prueba con `.noah spotify login`.{warning}"
        )

    @spotify.command()
    async def unlock(ctx: commands.Context, *, password: str = "") -> None:
        context = get_bot_context(ctx.bot)
        deleted = await _delete_message(ctx)

        if not context.spotify_access.check_password(password):
            await ctx.send(
                "❌ Password incorrecta.",
                delete_after=CONFIRM_DELETE_AFTER,
            )
            return

        context.spotify_access.unlock(ctx.author.id)
        warning = (
            ""
            if deleted
            else "\n⚠️ No pude borrar tu mensaje (me falta `Gestionar mensajes`), borralo tu."
        )
        await ctx.send(
            f"🔓 {ctx.author.mention}, Spotify desbloqueado. "
            f"Empieza con `.noah spotify join`.{warning}",
            delete_after=CONFIRM_DELETE_AFTER,
        )

    @spotify.command()
    async def lock(ctx: commands.Context) -> None:
        context = get_bot_context(ctx.bot)

        if not context.spotify_access.lock(ctx.author.id):
            await ctx.send("🔒 No tenias acceso.", delete_after=CONFIRM_DELETE_AFTER)
            return

        await ctx.send(
            "🔒 Acceso a Spotify retirado.",
            delete_after=CONFIRM_DELETE_AFTER,
        )

    @spotify.command()
    async def login(ctx: commands.Context) -> None:
        if not await _ensure_admin(ctx):
            return

        context = get_bot_context(ctx.bot)
        context.spotify_player.disconnect()

        try:
            username = await asyncio.to_thread(context.spotify_player.connect)
        except SpotifyError as exc:
            await ctx.send(f"❌ {exc}")
            return

        await ctx.send(f"✅ Sesion de Spotify abierta como **{username}**.")

    @spotify.command()
    async def status(ctx: commands.Context) -> None:
        if not await _ensure_admin(ctx):
            return

        context = get_bot_context(ctx.bot)
        player = context.spotify_player
        state = _get_state(ctx.bot, ctx.guild.id) if ctx.guild else SpotifyGuildState()

        table = EmbedTable(
            headers=["Campo", "Valor"],
            title="🎧 Noah Spotify · Estado",
            color=discord.Color.green(),
            max_columns=2,
        )
        table.add_row(["Credenciales", "✅" if player.has_credentials() else "❌"])
        table.add_row(["Sesion abierta", "✅" if player.is_connected else "❌"])
        table.add_row(["Espejo", "✅" if state.mirror else "❌"])
        table.add_row(["Volumen", f"{int(state.volume * 100)}%"])
        table.add_row(["Desbloqueados", str(len(context.spotify_access.users()))])
        await ctx.send(embed=table.render())

    @spotify.command()
    async def join(ctx: commands.Context) -> None:
        if not await _ensure_admin(ctx):
            return

        if ctx.guild is None or not isinstance(ctx.author, discord.Member):
            await ctx.send("❌ Este comando solo funciona dentro de un servidor.")
            return

        if ctx.author.voice is None or ctx.author.voice.channel is None:
            await ctx.send("❌ Tienes que estar en un canal de voz.")
            return

        channel = ctx.author.voice.channel
        voice_client = ctx.voice_client

        try:
            if voice_client is not None and voice_client.is_connected():
                await voice_client.move_to(channel)
            else:
                await channel.connect()
        except Exception as exc:
            await ctx.send(f"❌ No pude conectarme: `{exc}`")
            return

        state = _get_state(ctx.bot, ctx.guild.id)
        state.text_channel_id = ctx.channel.id

        context = get_bot_context(ctx.bot)

        try:
            username = await asyncio.to_thread(context.spotify_player.connect)
        except SpotifyError as exc:
            await ctx.send(f"🎤 Entre en **{channel.name}**, pero Spotify falla: `{exc}`")
            return

        await ctx.send(
            f"🎤 Entre en **{channel.name}** con la cuenta **{username}**. "
            "Usa `.noah spotify mirror` o `.noah spotify play <link>`."
        )

    @spotify.command()
    async def leave(ctx: commands.Context) -> None:
        if not await _ensure_admin(ctx):
            return

        if ctx.guild is None:
            await ctx.send("❌ Este comando solo funciona dentro de un servidor.")
            return

        _cancel_mirror(ctx.bot, ctx.guild.id)
        state = _get_state(ctx.bot, ctx.guild.id)
        _stop_playback(state, ctx.voice_client)

        if ctx.voice_client is not None:
            await ctx.voice_client.disconnect(force=False)

        await ctx.send("👋 Noah sale del canal de voz.")

    @spotify.command()
    async def mirror(ctx: commands.Context) -> None:
        if not await _ensure_admin(ctx):
            return

        if ctx.guild is None:
            await ctx.send("❌ Este comando solo funciona dentro de un servidor.")
            return

        if ctx.voice_client is None or not ctx.voice_client.is_connected():
            await ctx.send("❌ Noah no esta en un canal de voz. Usa `.noah spotify join`.")
            return

        context = get_bot_context(ctx.bot)
        state = _get_state(ctx.bot, ctx.guild.id)

        if state.mirror:
            _cancel_mirror(ctx.bot, ctx.guild.id)
            _stop_playback(state, ctx.voice_client)
            await ctx.send("🔇 Espejo desactivado.")
            return

        try:
            remote = await asyncio.to_thread(context.spotify_player.current_playback)
        except SpotifyError as exc:
            await ctx.send(f"❌ {exc}")
            return

        state.mirror = True
        state.text_channel_id = ctx.channel.id
        context.spotify_mirror_tasks[ctx.guild.id] = asyncio.create_task(
            _mirror_loop(ctx.bot, ctx.guild.id)
        )

        if remote is None or remote.uri is None or not remote.is_playing:
            await ctx.send(
                "🪞 Espejo activado. Dale al play en tu Spotify y Noah se engancha."
            )
            return

        try:
            await _apply_remote_state(ctx.bot, ctx.guild, state, remote)
        except SpotifyError as exc:
            await ctx.send(f"🪞 Espejo activado, pero no pude engancharme: `{exc}`")
            return

        await ctx.send(
            f"🪞 Espejo activado sobre **{remote.device}** · **{remote.label}**"
        )

    @spotify.command()
    async def play(ctx: commands.Context, *, link: str) -> None:
        if not await _ensure_admin(ctx):
            return

        if ctx.guild is None:
            await ctx.send("❌ Este comando solo funciona dentro de un servidor.")
            return

        context = get_bot_context(ctx.bot)
        _cancel_mirror(ctx.bot, ctx.guild.id)

        try:
            uri = context.spotify_player.resolve_track_uri(link)
            info = await _start_track(ctx.bot, ctx.guild, uri)
        except SpotifyError as exc:
            await ctx.send(f"❌ {exc}")
            return

        state = _get_state(ctx.bot, ctx.guild.id)
        state.text_channel_id = ctx.channel.id
        await ctx.send(
            f"▶️ **{info.label}** · `{format_ms(info.duration_ms)}` ({info.album})"
        )

    @spotify.command()
    async def stop(ctx: commands.Context) -> None:
        if not await _ensure_admin(ctx):
            return

        if ctx.guild is None:
            await ctx.send("❌ Este comando solo funciona dentro de un servidor.")
            return

        _cancel_mirror(ctx.bot, ctx.guild.id)
        state = _get_state(ctx.bot, ctx.guild.id)
        _stop_playback(state, ctx.voice_client)
        await ctx.send("⏹️ Parado. Tu Spotify sigue a lo suyo.")

    @spotify.command()
    async def now(ctx: commands.Context) -> None:
        if not await _ensure_admin(ctx):
            return

        context = get_bot_context(ctx.bot)
        state = _get_state(ctx.bot, ctx.guild.id) if ctx.guild else SpotifyGuildState()

        try:
            remote = await asyncio.to_thread(context.spotify_player.current_playback)
        except SpotifyError as exc:
            await ctx.send(f"❌ {exc}")
            return

        embed = discord.Embed(title="🎧 Now Playing", color=discord.Color.green())

        if state.playback is None:
            embed.add_field(name="En Noah", value="`nada`", inline=False)
        else:
            info = state.playback.info
            embed.add_field(
                name="En Noah",
                value=(
                    f"**{info.label}**\n"
                    f"`{format_ms(state.playback.elapsed_ms)} / "
                    f"{format_ms(info.duration_ms)}`"
                ),
                inline=False,
            )

        if remote is None or remote.uri is None:
            embed.add_field(name="En tu cuenta", value="`nada`", inline=False)
        else:
            embed.add_field(
                name=f"En tu cuenta ({remote.device})",
                value=(
                    f"**{remote.label}**\n"
                    f"`{format_ms(remote.progress_ms)} / "
                    f"{format_ms(remote.duration_ms)}`"
                    f"{'' if remote.is_playing else ' · ⏸️'}"
                ),
                inline=False,
            )

        embed.set_footer(text="Espejo activo" if state.mirror else "Espejo apagado")
        await ctx.send(embed=embed)

    @spotify.command()
    async def vol(ctx: commands.Context, value: int) -> None:
        if not await _ensure_admin(ctx):
            return

        if ctx.guild is None:
            await ctx.send("❌ Este comando solo funciona dentro de un servidor.")
            return

        if not 0 <= value <= 200:
            await ctx.send("❌ El volumen va de 0 a 200.")
            return

        state = _get_state(ctx.bot, ctx.guild.id)
        state.volume = value / 100

        source = ctx.voice_client.source if ctx.voice_client is not None else None
        if isinstance(source, discord.PCMVolumeTransformer):
            source.volume = state.volume

        await ctx.send(f"🔊 Volumen de Noah al **{value}%**.")

    @bot.listen("on_voice_state_update")
    async def _spotify_on_voice_state_update(
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        guild = member.guild
        me = guild.me

        if me is None or member.id != me.id:
            return

        if after.channel is not None:
            return

        _cancel_mirror(bot, guild.id)
        state = _get_state(bot, guild.id)
        _stop_playback(state, None)
