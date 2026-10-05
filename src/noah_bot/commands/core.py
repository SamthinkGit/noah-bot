import asyncio
import re
import time
from io import BytesIO

import discord
from discord.ext import commands
from rich import inspect

from noah_bot.modules.bot_context import get_bot_context, is_guard_active
from noah_bot.modules.discord_formatter import WaifuClaimFormatter
from noah_bot.modules.send_message import send_message
from noah_bot.modules.jarvis import create_jarvis_gif

CLAIM_REGEX = re.compile(
    r"Congrats,\s+(<@!?\d+>|@.+?)\s+you claimed a\s+\[(.*?)\]\s+(.*?)!",
    re.IGNORECASE,
)


async def _add_reactions_concurrently(
    message: discord.Message,
    emojis: list[str],
) -> None:
    async def _add_reaction(emoji: str) -> None:
        try:
            await message.add_reaction(emoji)
        except discord.HTTPException:
            pass

    await asyncio.gather(*(_add_reaction(emoji) for emoji in emojis))


def register_core_commands(bot: commands.Bot) -> None:
    @bot.event
    async def on_ready() -> None:
        print(f"Bot connected as {bot.user}")

    @bot.command(aliases=["ti"])
    async def timeit(ctx: commands.Context) -> None:
        context = get_bot_context(ctx.bot)
        started_at = time.time()
        context.timeit_sessions[ctx.author.id] = started_at
        context.latest_time_it = started_at

    @bot.command()
    async def jarvis(ctx: commands.Context, *, message: str | None = None) -> None:
        if not message or not message.strip():
            await ctx.send("❌ Use `.jarvis <message>`.")
            return

        cleaned_message = " ".join(message.split())
        if len(cleaned_message) > 180:
            await ctx.send("❌ Keep the message under 180 characters.")
            return

        await ctx.typing()

        try:
            gif_bytes = create_jarvis_gif(cleaned_message)
        except Exception:
            await ctx.send("❌ I couldn't render the Jarvis clip.")
            return

        buffer = BytesIO(gif_bytes)
        await ctx.send(file=discord.File(buffer, filename="jarvis.gif"))

    @bot.command()
    async def claim(ctx: commands.Context, *, text: str | None = None) -> None:
        _ = text
        context = get_bot_context(ctx.bot)
        user_id = ctx.author.id
        user_emoji = context.emoji_manager.get_emoji(ctx.author.id)

        if user_emoji:
            try:
                await ctx.message.add_reaction(user_emoji)
            except discord.HTTPException:
                pass

        if user_id not in context.timeit_sessions:
            return

        start_time = context.timeit_sessions.pop(user_id)
        elapsed = time.time() - start_time

        await ctx.send(
            f"{ctx.author.mention} obtained a time of `{elapsed:.4f}` seconds!"
        )

    @bot.command()
    async def debug_history(ctx: commands.Context) -> None:
        channel = ctx.channel
        messages = [message async for message in channel.history(limit=None)]
        text = [message.content for message in messages]
        print(text)

        await ctx.send(
            f"DEBUG: I can see **{len(messages)} messages** in this channel."
        )

    @bot.command()
    async def test_image(ctx: commands.Context) -> None:
        image_urls = [
            "https://i.blogs.es/8dee66/anime/500_333.jpeg",
            "https://placebear.com/400/300",
            "https://picsum.photos/400/300",
        ]

        for idx, url in enumerate(image_urls, start=1):
            embed = discord.Embed(title=f"Test Image {idx}")
            embed.set_image(url=url)
            await ctx.send(embed=embed)

    @bot.command()
    async def get_embed(ctx: commands.Context) -> None:
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

        for embed in replied_msg.embeds:
            inspect(embed)

    @bot.event
    async def on_message(message: discord.Message) -> None:
        if message.author.bot is False:
            await bot.process_commands(message)
            return

        if message.guild is not None and is_guard_active(bot, message.guild.id):
            return

        match = CLAIM_REGEX.search(message.content)
        if not match:
            await bot.process_commands(message)
            return

        _raw_user, rarity_symbol, waifu_name = match.groups()

        user = message.mentions[0] if message.mentions else None
        if not user:
            return
        context = get_bot_context(bot)
        claim_time_seconds = 0.0
        if context.latest_time_it is not None:
            claim_time_seconds = time.time() - context.latest_time_it

        if message.guild is not None:
            context.daily_stats.increment_waifu_claims(
                message.guild.id,
                message.guild.name,
                user.id,
                user.display_name,
            )

        embed = WaifuClaimFormatter.build_embed(
            user=user,
            waifu_name=waifu_name,
            rarity_symbol=rarity_symbol,
            claim_time_seconds=claim_time_seconds,
        )

        claim_message = await message.channel.send(embed=embed)
        context.autogami_claim_messages[claim_message.id] = user.id

        await _add_reactions_concurrently(
            claim_message,
            context.autogami_tokens.get_favorite_emojis(user.id),
        )

        try:
            await message.delete()
        except discord.Forbidden:
            pass
        except discord.NotFound:
            pass

    @bot.event
    async def on_reaction_add(
        reaction: discord.Reaction,
        user: discord.User | discord.Member,
    ) -> None:
        if user.bot:
            return

        message = reaction.message
        if bot.user is None or message.author.id != bot.user.id:
            return

        if message.guild is not None and is_guard_active(bot, message.guild.id):
            return

        context = get_bot_context(bot)
        claimer_id = context.autogami_claim_messages.get(message.id)
        if claimer_id is None or user.id != claimer_id:
            return

        favorite_emoji = str(reaction.emoji)
        if favorite_emoji not in context.autogami_tokens.get_favorite_emojis(user.id):
            return

        token = context.autogami_tokens.get_token(user.id)
        if token is None or message.guild is None:
            return

        try:
            await asyncio.to_thread(
                send_message,
                f".favl {favorite_emoji}",
                token,
                str(user.id),
                str(message.guild.id),
                str(message.channel.id),
            )
            context.daily_stats.increment_autogami_uses(
                message.guild.id,
                message.guild.name,
                user.id,
                user.display_name,
            )
        except Exception:
            return
