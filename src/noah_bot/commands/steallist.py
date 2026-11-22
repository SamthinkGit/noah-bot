import discord
from discord.ext import commands

from noah_bot.modules.bot_context import get_bot_context
from noah_bot.modules.help_ui import HelpEntry, HelpModule, HelpSection, register_help, send_help


HELP = register_help(
    HelpModule(
        key="steallist",
        emoji="🫳",
        name="Steallist",
        summary="Apunta las waifus que te han robado",
        description="Lista de waifus que te han robado y a quién. Alias `.sl`.",
        sections=(
            HelpSection(
                "Comandos",
                (
                    HelpEntry(".steallist add @user <waifu>", "Añade una waifu a tu lista."),
                    HelpEntry(".steallist show", "Tus robos pendientes."),
                    HelpEntry(".steallist show @user", "Waifus que te ha robado alguien."),
                    HelpEntry(".steallist remove <id>", "Quita una waifu por ID."),
                    HelpEntry(".steallist clear @user", "Borra todas las tuyas que tiene esa persona."),
                ),
            ),
        ),
    )
)


def register_steallist_commands(bot: commands.Bot) -> None:
    @bot.group(name="steallist", aliases=["sl"])
    async def steallist_cmd(ctx: commands.Context) -> None:
        if ctx.invoked_subcommand is None:
            await ctx.send("Use `.steallist help` or `.sl help` to see commands.")

    @steallist_cmd.command()
    async def help(ctx: commands.Context) -> None:
        await send_help(ctx, "steallist")

    @steallist_cmd.command()
    async def add(
        ctx: commands.Context, user: discord.Member, *waifu_words: str
    ) -> None:
        context = get_bot_context(ctx.bot)
        waifu_name = " ".join(waifu_words).strip()
        record_id = context.steallist.add_waifu(
            owner_id=ctx.author.id,
            owner_name=ctx.author.display_name,
            target_id=user.id,
            target_name=user.display_name,
            waifu_name=waifu_name,
        )

        await ctx.send(
            f"🗡️ `{waifu_name}` added to your steal list from {user.mention} "
            f"(ID `{record_id}`)"
        )

    @steallist_cmd.command()
    async def show(ctx: commands.Context, user: discord.Member | None = None) -> None:
        _ = user
        context = get_bot_context(ctx.bot)
        records = context.steallist.get_by_owner(ctx.author.id)
        embed = context.steallist.render_embed(
            records,
            title="🗡️ Steal List",
            subtitle=f"{ctx.author.display_name}'s pending waifus",
        )
        await ctx.send(embed=embed)

    @steallist_cmd.command()
    async def remove(ctx: commands.Context, record_id: int) -> None:
        context = get_bot_context(ctx.bot)
        record = context.steallist.get_by_id(record_id)

        if not record:
            await ctx.send("Record not found.")
            return

        if record["owner_id"] != ctx.author.id:
            await ctx.send("❌ You can only remove your own waifus.")
            return

        context.steallist.remove_by_id(record_id)
        await ctx.send(f"Removed `{record['waifu_name']}` from your steal list.")

    @steallist_cmd.command()
    async def clear(ctx: commands.Context, user: discord.Member) -> None:
        _ = user
        context = get_bot_context(ctx.bot)
        count = context.steallist.clear_owner(ctx.author.id)

        if count == 0:
            await ctx.send("Nothing to clear.")
        else:
            await ctx.send(f"🧹 Cleared `{count}` waifus from your steal list.")
