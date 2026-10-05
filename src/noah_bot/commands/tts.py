import asyncio
import os
import random
import re
import tempfile

import discord
from discord.ext import commands

from noah_bot.modules.bot_context import get_bot_context, is_guard_active
from noah_bot.modules.discord_formatter import EmbedTable
from noah_bot.modules.tts import text_to_speech


GREET_MESSAGES = (
    "[happy]Buenas, {user}",
    "[seductive]Hola, {user}",
)

FAREWELL_MESSAGES = (
    "[sad]Adios, {user}",
    "[soft]Hasta luego, {user}",
)


def _build_greet_name(member: discord.Member | discord.User) -> str:
    raw_name = (
        getattr(member, "display_name", None)
        or getattr(member, "global_name", None)
        or member.name
    )
    sanitized_name = re.sub(r"[^0-9A-Za-zÁÉÍÓÚÜÑáéíóúüñ ]+", " ", raw_name)
    sanitized_name = re.sub(r"\s+", " ", sanitized_name).strip()
    return sanitized_name or member.name


def _disable_greet(bot: commands.Bot, guild_id: int | None) -> None:
    if guild_id is None:
        return

    context = get_bot_context(bot)
    context.tts_greet_sessions.pop(guild_id, None)


async def _play_tts(
    voice_client: discord.VoiceClient,
    text: str,
    voice_id: str,
) -> None:
    stream = text_to_speech(text, voice_id=voice_id)
    audio_bytes = b"".join(stream)

    with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as tmp:
        tmp.write(audio_bytes)
        tmp_path = tmp.name

    source = discord.FFmpegPCMAudio(tmp_path)

    def _cleanup(error: Exception | None) -> None:
        try:
            source.cleanup()
        except Exception:
            pass

        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    if voice_client.is_playing():
        voice_client.stop()

    voice_client.play(source, after=_cleanup)


def register_tts_commands(bot: commands.Bot, noah_group: commands.Group) -> None:
    @noah_group.group()
    async def tts(ctx: commands.Context) -> None:
        if ctx.invoked_subcommand is None:
            await ctx.send(
                "Use `.noah tts join`, `.noah tts say`, `.noah tts greet`, "
                "`.noah tts showvoices`, `.noah tts addvoice`, `.noah tts delvoice` "
                "or `.noah tts setvoice`."
            )

    @tts.command()
    async def showvoices(ctx: commands.Context) -> None:
        context = get_bot_context(ctx.bot)
        voices = context.tts_voices.list_voices()

        table = EmbedTable(
            headers=["Name", "Voice ID", "Active"],
            title="🗣️ Noah TTS Voices",
            description="Voces disponibles para `.noah tts say` y `.noah tts greet`.",
            color=discord.Color.blurple(),
            max_columns=3,
        )

        for voice in voices:
            table.add_row(
                [
                    str(voice["name"]),
                    str(voice["voice_id"]),
                    "✅" if bool(voice["active"]) else "",
                ]
            )

        await ctx.send(embed=table.render())

    @tts.command()
    async def addvoice(ctx: commands.Context, voice_id: str, *, name: str) -> None:
        context = get_bot_context(ctx.bot)
        if not context.tts_voices.add_voice(voice_id=voice_id, name=name):
            await ctx.send("❌ Invalid params. Use `.noah tts addvoice <voiceid> <name>`.")
            return

        await ctx.send(f"✅ Voice **{name.strip()}** added.")

    @tts.command()
    async def delvoice(ctx: commands.Context, *, name: str) -> None:
        context = get_bot_context(ctx.bot)
        deleted = context.tts_voices.delete_voice(name=name)
        if not deleted:
            await ctx.send("❌ Voice not found or cannot be deleted.")
            return

        active_voice_name = context.tts_voices.get_active_voice()["name"]
        await ctx.send(
            f"🗑️ Voice **{name.strip()}** deleted. Active voice: **{active_voice_name}**."
        )

    @tts.command()
    async def setvoice(ctx: commands.Context, *, name: str) -> None:
        context = get_bot_context(ctx.bot)
        changed = context.tts_voices.set_active_voice(name=name)
        if not changed:
            await ctx.send("❌ Voice not found. Use `.noah tts showvoices`.")
            return

        active_voice = context.tts_voices.get_active_voice()
        await ctx.send(f"🎙️ Active TTS voice set to **{active_voice['name']}**.")

    @tts.command()
    async def join(ctx: commands.Context) -> None:
        if not ctx.author.voice:
            await ctx.send("❌ You must be in a voice channel.")
            return

        channel = ctx.author.voice.channel
        voice_client = ctx.voice_client

        try:
            if voice_client and voice_client.is_connected():
                if voice_client.channel.id == channel.id:
                    await ctx.send("🎤 Already in your voice channel.")
                    return
                await voice_client.move_to(channel)
            else:
                await channel.connect()
        except Exception as exc:
            await ctx.send(f"❌ Voice connection failed: `{exc}`")
            return

        if ctx.guild is not None:
            _disable_greet(ctx.bot, ctx.guild.id)

        await ctx.send(f"🎤 Joined **{channel.name}**")

    @tts.command(aliases=["nts"])
    async def say(ctx: commands.Context, *, text: str) -> None:
        if not ctx.voice_client:
            await ctx.send("❌ Noah is not in a voice channel. Use `.noah tts join`.")
            return

        context = get_bot_context(ctx.bot)
        active_voice = context.tts_voices.get_active_voice()

        try:
            await _play_tts(ctx.voice_client, text, voice_id=active_voice["voice_id"])
        except Exception as exc:
            await ctx.send(f"❌ TTS failed: `{exc}`")

    @tts.command()
    async def greet(ctx: commands.Context) -> None:
        if ctx.guild is None:
            await ctx.send("❌ This command can only be used in a server.")
            return

        voice_client = ctx.voice_client
        if voice_client is None or not voice_client.is_connected() or voice_client.channel is None:
            await ctx.send("❌ Noah has to be in a voice channel first. Use `.noah tts join`.")
            return

        context = get_bot_context(ctx.bot)
        active_channel_id = context.tts_greet_sessions.get(ctx.guild.id)

        if active_channel_id == voice_client.channel.id:
            context.tts_greet_sessions.pop(ctx.guild.id, None)
            await ctx.send("🔇 TTS greet desactivado.")
            return

        context.tts_greet_sessions[ctx.guild.id] = voice_client.channel.id
        await ctx.send(f"🔊 TTS greet activado en **{voice_client.channel.name}**.")

    @bot.listen("on_voice_state_update")
    async def _tts_greet_on_voice_join(
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        guild = member.guild
        if is_guard_active(bot, guild.id):
            return

        context = get_bot_context(bot)
        active_voice = context.tts_voices.get_active_voice()
        tracked_channel_id = context.tts_greet_sessions.get(guild.id)
        if tracked_channel_id is None:
            return

        me = guild.me
        if me is None:
            _disable_greet(bot, guild.id)
            return

        if member.id == me.id:
            new_channel_id = after.channel.id if after.channel else None
            if new_channel_id != tracked_channel_id:
                _disable_greet(bot, guild.id)
            return

        if member.bot:
            return

        if after.channel is None or after.channel.id != tracked_channel_id:
            if before.channel is not None and before.channel.id == tracked_channel_id:
                farewell_text = random.choice(FAREWELL_MESSAGES).format(
                    user=_build_greet_name(member)
                )
                voice_client = guild.voice_client
                if voice_client is None or not voice_client.is_connected() or voice_client.channel is None:
                    _disable_greet(bot, guild.id)
                    return

                if voice_client.channel.id != tracked_channel_id:
                    _disable_greet(bot, guild.id)
                    return

                try:
                    await _play_tts(
                        voice_client,
                        farewell_text,
                        voice_id=active_voice["voice_id"],
                    )
                except Exception:
                    _disable_greet(bot, guild.id)
            return

        previous_channel_id = before.channel.id if before.channel else None
        if previous_channel_id == tracked_channel_id:
            return

        voice_client = guild.voice_client
        if voice_client is None or not voice_client.is_connected() or voice_client.channel is None:
            _disable_greet(bot, guild.id)
            return

        if voice_client.channel.id != tracked_channel_id:
            _disable_greet(bot, guild.id)
            return

        greet_text = random.choice(GREET_MESSAGES).format(user=_build_greet_name(member))

        try:
            await asyncio.sleep(2)

            refreshed_member = guild.get_member(member.id)
            if refreshed_member is None or refreshed_member.voice is None:
                return

            if refreshed_member.voice.channel is None or refreshed_member.voice.channel.id != tracked_channel_id:
                return

            voice_client = guild.voice_client
            if voice_client is None or not voice_client.is_connected() or voice_client.channel is None:
                _disable_greet(bot, guild.id)
                return

            if voice_client.channel.id != tracked_channel_id:
                _disable_greet(bot, guild.id)
                return

            await _play_tts(
                voice_client,
                greet_text,
                voice_id=active_voice["voice_id"],
            )
        except Exception:
            _disable_greet(bot, guild.id)
