import io
import random
from contextlib import suppress

import discord
from discord.ext import commands

from noah_bot.modules.bot_context import get_bot_context


PRESENTATION_NOTICE_DELETE_AFTER = 20
PRESENTATION_LIST_FIELD_LIMIT = 1024
PRESENTATION_PREVIEW_LIMIT = 3800
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
    embed.add_field(
        name=".noah presentations list",
        value="Muestra quién se ha presentado ya y quién falta por presentarse.",
        inline=False,
    )
    embed.add_field(
        name=".noah presentations show [@user]",
        value="Muestra la presentación de alguien (o la tuya). Disponible para todos.",
        inline=False,
    )
    return embed


def _format_member_list(members: list[discord.Member]) -> tuple[str, bool]:
    """Devuelve las menciones que caben en un field de embed y si se ha truncado."""
    if not members:
        return "Nadie.", False

    lines: list[str] = []
    length = 0
    for index, member in enumerate(members):
        more_line = f"… y {len(members) - index} más"
        if length + len(member.mention) + 1 + len(more_line) > PRESENTATION_LIST_FIELD_LIMIT:
            lines.append(more_line)
            return "\n".join(lines), True
        lines.append(member.mention)
        length += len(member.mention) + 1

    return "\n".join(lines), False


def _build_presentations_list_file(
    presented: list[discord.Member],
    missing: list[discord.Member],
) -> discord.File:
    lines = [f"Presentados ({len(presented)}):"]
    lines.extend(f"- {member.display_name} ({member.id})" for member in presented)
    lines.append("")
    lines.append(f"Sin presentar ({len(missing)}):")
    lines.extend(f"- {member.display_name} ({member.id})" for member in missing)
    buffer = io.BytesIO("\n".join(lines).encode("utf-8"))
    return discord.File(buffer, filename="presentaciones.txt")


async def _fetch_presentation_message(
    guild: discord.Guild,
    channel_id: int,
    message_id: int,
) -> discord.Message | None:
    channel = guild.get_channel_or_thread(channel_id)
    if channel is None or not hasattr(channel, "fetch_message"):
        return None

    try:
        return await channel.fetch_message(message_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return None


async def _search_presentation_message(
    channel: discord.TextChannel,
    user_id: int,
) -> discord.Message | None:
    try:
        async for history_message in channel.history(limit=None, oldest_first=True):
            if history_message.author.id == user_id:
                return history_message
    except (discord.Forbidden, discord.HTTPException):
        return None
    return None


def _build_presentation_embed(
    member: discord.Member,
    presentation: discord.Message,
) -> discord.Embed:
    content = presentation.content.strip() or "*Sin texto.*"
    if len(content) > PRESENTATION_PREVIEW_LIMIT:
        content = content[:PRESENTATION_PREVIEW_LIMIT] + "…"

    embed = discord.Embed(
        description=content,
        color=member.color if member.color.value else discord.Color.blurple(),
        timestamp=presentation.created_at,
    )
    embed.set_author(
        name=f"Presentación de {member.display_name}",
        icon_url=member.display_avatar.url,
        url=presentation.jump_url,
    )
    embed.set_thumbnail(url=member.display_avatar.url)

    for attachment in presentation.attachments:
        if attachment.content_type and attachment.content_type.startswith("image/"):
            embed.set_image(url=attachment.url)
            break

    return embed


class PresentationLinkView(discord.ui.View):
    def __init__(self, jump_url: str) -> None:
        super().__init__()
        self.add_item(
            discord.ui.Button(
                label="Ir a la presentación",
                style=discord.ButtonStyle.link,
                url=jump_url,
            )
        )


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
            store.record_presentations(
                message.guild.id,
                {message.author.id: (message.channel.id, message.id)},
            )
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

        first_messages: dict[int, tuple[int, int]] = {}
        try:
            async for history_message in channel.history(limit=None, oldest_first=True):
                if history_message.author.bot:
                    continue
                first_messages.setdefault(
                    history_message.author.id,
                    (channel.id, history_message.id),
                )
        except (discord.Forbidden, discord.HTTPException):
            await status_message.edit(
                content=(
                    f"⚠️ Canal de presentaciones configurado en {channel.mention}, pero no "
                    "he podido leer su historial. Usa `.noah presentations complete <@user>` "
                    "para marcar a los que ya se presentaron."
                )
            )
            return

        added = store.record_presentations(ctx.guild.id, first_messages)
        await status_message.edit(
            content=(
                f"✅ Canal de presentaciones configurado en {channel.mention}. "
                f"He encontrado `{len(first_messages)}` usuarios presentados "
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

    @presentations.command(name="list", aliases=["who"])
    async def list_presentations(ctx: commands.Context) -> None:
        if not await _check_admin(ctx):
            return

        store = get_bot_context(ctx.bot).presentations
        channel_id = store.get_channel_id(ctx.guild.id)
        if channel_id is None:
            await ctx.send(
                "❌ Primero configura el canal con `.noah presentations setchannel <#canal>`."
            )
            return

        completed_ids = store.get_completed_user_ids(ctx.guild.id)
        members = sorted(
            (member for member in ctx.guild.members if not member.bot),
            key=lambda member: member.display_name.casefold(),
        )
        presented = [member for member in members if member.id in completed_ids]
        missing = [member for member in members if member.id not in completed_ids]

        presented_text, presented_truncated = _format_member_list(presented)
        missing_text, missing_truncated = _format_member_list(missing)

        embed = discord.Embed(
            title="Presentaciones",
            description=f"Canal: <#{channel_id}>",
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name=f"✅ Presentados ({len(presented)})",
            value=presented_text,
            inline=True,
        )
        embed.add_field(
            name=f"⏳ Sin presentar ({len(missing)})",
            value=missing_text,
            inline=True,
        )

        if presented_truncated or missing_truncated:
            embed.set_footer(text="La lista completa va en el archivo adjunto.")
            await ctx.send(
                embed=embed,
                file=_build_presentations_list_file(presented, missing),
            )
            return

        await ctx.send(embed=embed)

    @presentations.command()
    async def show(ctx: commands.Context, member: discord.Member | None = None) -> None:
        if ctx.guild is None:
            await ctx.send("❌ Este comando solo funciona dentro de un servidor.")
            return

        target = member or ctx.author
        store = get_bot_context(ctx.bot).presentations
        channel_id = store.get_channel_id(ctx.guild.id)
        if channel_id is None:
            await ctx.send("❌ Todavía no hay un canal de presentaciones configurado.")
            return

        presentation: discord.Message | None = None
        stored = store.get_presentation_message(ctx.guild.id, target.id)
        if stored is not None:
            presentation = await _fetch_presentation_message(ctx.guild, *stored)
            if presentation is None:
                store.forget_presentation_message(ctx.guild.id, target.id)

        if presentation is None:
            channel = ctx.guild.get_channel(channel_id)
            if isinstance(channel, discord.TextChannel):
                async with ctx.typing():
                    presentation = await _search_presentation_message(channel, target.id)
            if presentation is not None:
                store.record_presentations(
                    ctx.guild.id,
                    {target.id: (presentation.channel.id, presentation.id)},
                )

        if presentation is None:
            if store.has_completed(ctx.guild.id, target.id):
                await ctx.send(
                    f"ℹ️ {target.display_name} está marcado como presentado, pero no encuentro "
                    f"su mensaje en <#{channel_id}>."
                )
                return

            await ctx.send(
                f"❌ {target.display_name} todavía no se ha presentado en <#{channel_id}>."
            )
            return

        await ctx.send(
            f"📜 Aquí tienes la presentación de {target.mention}: {presentation.jump_url}",
            embed=_build_presentation_embed(target, presentation),
            view=PresentationLinkView(presentation.jump_url),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @setchannel.error
    @complete.error
    @show.error
    async def _presentations_argument_error(
        ctx: commands.Context,
        error: commands.CommandError,
    ) -> None:
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(embed=_build_presentations_help_embed())
            return

        if isinstance(error, (commands.ChannelNotFound, commands.MemberNotFound)):
            await ctx.send(f"❌ {error}")
