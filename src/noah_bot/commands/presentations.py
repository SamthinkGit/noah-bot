import random
from contextlib import suppress

import discord
from discord.ext import commands

from noah_bot.modules.bot_context import get_bot_context


PRESENTATION_NOTICE_DELETE_AFTER = 20
PRESENTATION_NOTICE_TEMPLATES = (
    "{mention} Uhmm... Parece que aún no te has presentado, ¿te echo una mano?",
    "{mention} Oye, oye... ¿y tu presentación? Aún no sé ni quién eres 👀",
    "{mention} Espera un segundo... ¿nos conocemos? Creo que todavía no te has presentado 🖤",
    "{mention} Me encanta que hables, pero antes me gustaría saber quién eres. ¿Te ayudo a presentarte?",
    "{mention} Psst... te has saltado un pasito: ¡la presentación! Pulsa el botón y te cuento.",
    "{mention} Hmm, hmm... mis registros dicen que aún no te has presentado. ¿Lo arreglamos?",
    "{mention} ¡Hola desconocid@! Bueno... desconocid@ hasta que te presentes 😈",
    "{mention} Noah detecta un alma sin presentar. Protocolo de ayuda activado ✨",
    "{mention} No quiero ser pesado, pero... bueno, sí: ¡preséntate! Te dejo la guía aquí abajo.",
    "{mention} Antes de seguir charlando, ¿qué tal si te presentas? Prometo no morder... mucho 🖤",
)


def _is_presentations_admin(member: discord.abc.User) -> bool:
    return (
        isinstance(member, discord.Member)
        and member.guild_permissions.administrator
    )


def _is_presentation_channel(channel: discord.abc.Messageable, channel_id: int) -> bool:
    if getattr(channel, "id", None) == channel_id:
        return True
    return getattr(channel, "parent_id", None) == channel_id


def _build_presentations_help_embed() -> discord.Embed:
    embed = discord.Embed(
        title="Presentaciones",
        description=(
            "Obliga a los usuarios a presentarse antes de hablar en otros canales. "
            "Solo administradores."
        ),
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name=".noah presentations setchannel <#canal>",
        value=(
            "Configura el canal de presentaciones. Revisa su historial y marca como "
            "presentados a todos los que ya escribieron allí."
        ),
        inline=False,
    )
    embed.add_field(
        name=".noah presentations complete <@user>",
        value="Marca manualmente a un usuario como presentado.",
        inline=False,
    )
    embed.add_field(
        name=".noah presentations enable / disable",
        value="Activa o desactiva la obligación de presentarse.",
        inline=False,
    )
    embed.add_field(
        name=".noah presentations status",
        value="Muestra la configuración actual.",
        inline=False,
    )
    return embed


class PresentationNoticeView(discord.ui.View):
    def __init__(self, *, channel_id: int) -> None:
        super().__init__(timeout=PRESENTATION_NOTICE_DELETE_AFTER)
        self.channel_id = channel_id

    @discord.ui.button(label="📜 Ver cómo presentarme", style=discord.ButtonStyle.primary)
    async def show_guide(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        _ = button
        await interaction.response.send_message(
            "\n".join(
                [
                    "¡Es muy fácil! 🖤",
                    f"1. Pásate por <#{self.channel_id}>.",
                    "2. Copia la plantilla que usan todos los usuarios.",
                    "3. Rellénala con tus datos y envíala en ese canal.",
                    "",
                    "En cuanto te presentes dejaré de molestarte <3",
                ]
            ),
            ephemeral=True,
        )


async def _send_presentation_notice(message: discord.Message, channel_id: int) -> None:
    content = random.choice(PRESENTATION_NOTICE_TEMPLATES).format(
        mention=message.author.mention
    )
    send_kwargs = {
        "view": PresentationNoticeView(channel_id=channel_id),
        "delete_after": PRESENTATION_NOTICE_DELETE_AFTER,
        "allowed_mentions": discord.AllowedMentions(
            everyone=False,
            roles=False,
            users=[message.author],
        ),
    }

    try:
        await message.reply(content, mention_author=False, **send_kwargs)
    except discord.HTTPException:
        with suppress(discord.HTTPException):
            await message.channel.send(content, **send_kwargs)


def register_presentations_commands(bot: commands.Bot, noah_group: commands.Group) -> None:
    @bot.listen("on_message")
    async def _handle_presentations(message: discord.Message) -> None:
        if message.author.bot or message.guild is None:
            return

        store = get_bot_context(bot).presentations
        channel_id = store.get_channel_id(message.guild.id)
        if channel_id is None:
            return

        if _is_presentation_channel(message.channel, channel_id):
            store.mark_completed(message.guild.id, [message.author.id])
            return

        if not store.is_enabled(message.guild.id):
            return

        if store.has_completed(message.guild.id, message.author.id):
            return

        await _send_presentation_notice(message, channel_id)

    @noah_group.group()
    async def presentations(ctx: commands.Context) -> None:
        if ctx.invoked_subcommand is None:
            await ctx.send(embed=_build_presentations_help_embed())

    @presentations.command()
    async def help(ctx: commands.Context) -> None:
        await ctx.send(embed=_build_presentations_help_embed())

    async def _check_admin(ctx: commands.Context) -> bool:
        if ctx.guild is None:
            await ctx.send("❌ Este comando solo funciona dentro de un servidor.")
            return False

        if not _is_presentations_admin(ctx.author):
            await ctx.send("❌ Solo los administradores pueden usar este comando.")
            return False

        return True

    @presentations.command()
    async def setchannel(ctx: commands.Context, channel: discord.TextChannel) -> None:
        if not await _check_admin(ctx):
            return

        store = get_bot_context(ctx.bot).presentations
        store.set_channel(ctx.guild.id, channel.id)

        status_message = await ctx.send(
            f"⏳ Canal de presentaciones configurado en {channel.mention}. "
            "Revisando quién se ha presentado ya..."
        )

        try:
            author_ids = {
                history_message.author.id
                async for history_message in channel.history(limit=None)
                if not history_message.author.bot
            }
        except (discord.Forbidden, discord.HTTPException):
            await status_message.edit(
                content=(
                    f"⚠️ Canal de presentaciones configurado en {channel.mention}, pero no "
                    "he podido leer su historial. Usa `.noah presentations complete <@user>` "
                    "para marcar a los que ya se presentaron."
                )
            )
            return

        added = store.mark_completed(ctx.guild.id, list(author_ids))
        await status_message.edit(
            content=(
                f"✅ Canal de presentaciones configurado en {channel.mention}. "
                f"He encontrado `{len(author_ids)}` usuarios presentados "
                f"(`{added}` nuevos)."
            )
        )

    @presentations.command()
    async def complete(ctx: commands.Context, member: discord.Member) -> None:
        if not await _check_admin(ctx):
            return

        store = get_bot_context(ctx.bot).presentations
        added = store.mark_completed(ctx.guild.id, [member.id])
        if added:
            await ctx.send(f"✅ {member.mention} ha quedado marcado como presentado.")
            return

        await ctx.send(f"ℹ️ {member.mention} ya estaba marcado como presentado.")

    @presentations.command()
    async def enable(ctx: commands.Context) -> None:
        if not await _check_admin(ctx):
            return

        store = get_bot_context(ctx.bot).presentations
        channel_id = store.get_channel_id(ctx.guild.id)
        if channel_id is None:
            await ctx.send(
                "❌ Primero configura el canal con `.noah presentations setchannel <#canal>`."
            )
            return

        store.set_enabled(ctx.guild.id, True)
        await ctx.send(
            f"✅ Presentaciones obligatorias activadas. Quien no haya escrito en <#{channel_id}> "
            "recibirá un aviso en cada mensaje."
        )

    @presentations.command()
    async def disable(ctx: commands.Context) -> None:
        if not await _check_admin(ctx):
            return

        store = get_bot_context(ctx.bot).presentations
        store.set_enabled(ctx.guild.id, False)
        await ctx.send("✅ Presentaciones obligatorias desactivadas.")

    @presentations.command()
    async def status(ctx: commands.Context) -> None:
        if not await _check_admin(ctx):
            return

        store = get_bot_context(ctx.bot).presentations
        channel_id = store.get_channel_id(ctx.guild.id)
        embed = discord.Embed(title="Presentaciones", color=discord.Color.blurple())
        embed.add_field(
            name="Canal",
            value=f"<#{channel_id}>" if channel_id is not None else "Sin configurar",
            inline=True,
        )
        embed.add_field(
            name="Estado",
            value="Activado" if store.is_enabled(ctx.guild.id) else "Desactivado",
            inline=True,
        )
        embed.add_field(
            name="Presentados",
            value=f"`{store.completed_count(ctx.guild.id)}`",
            inline=True,
        )
        await ctx.send(embed=embed)

    @setchannel.error
    @complete.error
    async def _presentations_argument_error(
        ctx: commands.Context,
        error: commands.CommandError,
    ) -> None:
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(embed=_build_presentations_help_embed())
            return

        if isinstance(error, (commands.ChannelNotFound, commands.MemberNotFound)):
            await ctx.send(f"❌ {error}")
