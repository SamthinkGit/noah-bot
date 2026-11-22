import time

import discord
from discord.ext import commands

from noah_bot.modules.bot_context import get_bot_context
from noah_bot.modules.discord_formatter import DiscordChart
from noah_bot.modules.leaderboard import generate_date
from noah_bot.modules.help_ui import HelpEntry, HelpModule, HelpSection, register_help, send_help


HELP = register_help(
    HelpModule(
        key="waifuracer",
        emoji="⏱️",
        name="Waifuracer",
        summary="Cronometra cuánto tardas en claimear y guarda récords",
        description="Mide el tiempo entre que aparece una waifu y la claimeas, y guarda los récords. Alias `.wfr`.",
        sections=(
            HelpSection(
                "Cronómetro",
                (
                    HelpEntry(".timeit", "Empieza un cronómetro (alias `.ti`)."),
                    HelpEntry(".claim", "Lo para y enseña el resultado."),
                    HelpEntry(".waifuracer elapsed", "Tiempo desde el último `.timeit`."),
                    HelpEntry(".waifuracer predict", "Predice cuándo aparecerá la próxima waifu."),
                    HelpEntry(".waifuracer setemoji <emoji>", "Tu emoji de reacción al claimear."),
                ),
            ),
            HelpSection(
                "Récords",
                (
                    HelpEntry(".waifuracer add @user <tiempo> [-tag tag]", "Añade un récord."),
                    HelpEntry(".waifuracer remove <id>", "Borra un récord por ID."),
                    HelpEntry(".waifuracer top [-user @user]", "Los 20 mejores (opcionalmente de alguien)."),
                    HelpEntry(".waifuracer latest [-user @user]", "Los 20 últimos (opcionalmente de alguien)."),
                ),
            ),
        ),
    )
)


def register_waifuracer_commands(bot: commands.Bot) -> None:
    @bot.group(name="waifuracer", aliases=["wfr"])
    async def waifuracer(ctx: commands.Context) -> None:
        if ctx.invoked_subcommand is None:
            await ctx.send("Use `.waifuracer help` or `.wfr help` to see commands.")

    @waifuracer.command()
    async def help(ctx: commands.Context) -> None:
        await send_help(ctx, "waifuracer")

    @waifuracer.command()
    async def elapsed(ctx: commands.Context) -> None:
        context = get_bot_context(ctx.bot)

        if context.latest_time_it is None:
            await ctx.send("No active timers.")
            return

        elapsed_seconds = time.time() - context.latest_time_it
        await ctx.send(
            f"Elapsed time since last .timeit: `{elapsed_seconds:.4f}` seconds."
        )

    @waifuracer.command()
    async def predict(ctx: commands.Context) -> None:
        context = get_bot_context(ctx.bot)

        if context.latest_time_it is None:
            await ctx.send("No active timers.")
            return

        elapsed_seconds = 240 - (time.time() - context.latest_time_it)
        await ctx.send(f"Next waifu should appear on `{elapsed_seconds:.4f}` seconds.")

    @waifuracer.command()
    async def setemoji(ctx: commands.Context, emoji: str) -> None:
        context = get_bot_context(ctx.bot)

        try:
            await ctx.message.add_reaction(emoji)
        except discord.HTTPException:
            await ctx.send("Invalid emoji. Please provide a valid emoji.")
            return

        context.emoji_manager.set_emoji(ctx.author.id, emoji)
        await ctx.send(f"{ctx.author.mention} your claim emoji is now {emoji}")

    @waifuracer.command()
    async def add(
        ctx: commands.Context,
        user: discord.Member,
        record_time: float,
        *,
        args: str | None = None,
    ) -> None:
        context = get_bot_context(ctx.bot)
        tag = None

        if args and args.startswith("-tag"):
            tag = args.replace("-tag", "").strip()

        record_id = context.leaderboard.add_record(
            user_id=user.id,
            username=user.display_name,
            record_time=record_time,
            created_at=generate_date(),
            tag=tag,
        )

        await ctx.send(
            f"Record `{record_id}` added for {user.mention}: "
            f"`{record_time:.4f}` seconds" + (f" (tag: `{tag}`)" if tag else "")
        )

    @waifuracer.command()
    async def remove(ctx: commands.Context, record_id: int) -> None:
        context = get_bot_context(ctx.bot)
        success = context.leaderboard.delete_record_by_id(record_id)

        if success:
            await ctx.send(f"Record `{record_id}` removed.")
        else:
            await ctx.send(f"Record `{record_id}` not found.")

    @waifuracer.command()
    async def top(ctx: commands.Context, *, args: str | None = None) -> None:
        context = get_bot_context(ctx.bot)
        user = None

        if args and args.startswith("-user") and ctx.message.mentions:
            user = ctx.message.mentions[0]

        if user:
            records = context.leaderboard.get_records_by_user(user.id)
            records = sorted(records, key=lambda record: record["record_time"])[:20]
        else:
            records = context.leaderboard.get_top_n(20)

        if not records:
            await ctx.send("No records available.")
            return

        chart = DiscordChart(headers=["ID", "User", "Time (s)", "Date", "Tag"])

        for record in records:
            chart.add_row(
                [
                    str(record["id"]),
                    record["username"],
                    f'{record["record_time"]:.4f}',
                    record["created_at"],
                    record["tag"] or "-",
                ]
            )

        await ctx.send(chart.render())

    @waifuracer.command()
    async def latest(ctx: commands.Context, *, args: str | None = None) -> None:
        context = get_bot_context(ctx.bot)
        user = None

        if args and args.startswith("-user") and ctx.message.mentions:
            user = ctx.message.mentions[0]

        if user:
            records = context.leaderboard.get_records_by_user(user.id)[-20:]
        else:
            records = context.leaderboard.get_all_records()[-20:]

        if not records:
            await ctx.send("No records available.")
            return

        chart = DiscordChart(headers=["ID", "User", "Time (s)", "Date", "Tag"])

        for record in records:
            chart.add_row(
                [
                    str(record["id"]),
                    record["username"],
                    f'{record["record_time"]:.4f}',
                    record["created_at"],
                    record["tag"] or "-",
                ]
            )

        await ctx.send(chart.render())
