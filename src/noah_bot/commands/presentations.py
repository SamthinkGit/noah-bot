import asyncio
import io
import random
from contextlib import suppress

import discord
from discord.ext import commands

from noah_bot.modules.bot_context import get_bot_context, is_guard_active
from noah_bot.modules.discord_formatter import build_quote_text_embed
from noah_bot.modules.presentations import PresentationsStore

PRESENTATION_STICKY_BUTTON_ID = "presentations:sticky:introduce"
PRESENTATION_STICKY_REPOST_DELAY = 2.0
PRESENTATION_STICKY_TEXT = (
    "¿Aún no te has presentado?\n"
    "Pulsa aquí para agregar información sobre ti! <3"
)
# Cada campo termina en ": " y va separado por una línea en blanco.
PRESENTATION_TEMPLATE = "\n\n".join(
    f"{field} "
    for field in (
        "→ 𝙉𝙤𝙢𝙗𝙧𝙚 / 𝘼𝙥𝙤𝙙𝙤:",
        "→ 𝙀𝙙𝙖𝙙:",
        "→ 𝙎𝙚𝙭𝙤 (𝙊𝙥𝙘𝙞𝙤𝙣𝙖𝙡):",
        "→ 𝘿𝙤𝙣𝙙𝙚 𝙫𝙞𝙫𝙚𝙨? (𝙊𝙥𝙘𝙞𝙤𝙣𝙖𝙡):",
        "→ 𝙌𝙪𝙚 𝙝𝙖𝙘𝙚𝙨 𝙚𝙣 𝙩𝙪 𝙩𝙞𝙚𝙢𝙥𝙤 𝙡𝙞𝙗𝙧𝙚?:",
        "→ 𝙌𝙪𝙚 𝙤𝙙𝙞𝙖𝙨?:",
        "→ 𝙌𝙪𝙚 𝙗𝙪𝙨𝙘𝙖𝙨 𝙚𝙣 𝙚𝙨𝙩𝙚 𝙨𝙚𝙧𝙫𝙚𝙧?:",
        "→ 𝙏𝙚 𝙜𝙪𝙨𝙩𝙖𝙧í𝙖 𝙦𝙪𝙚𝙙𝙖𝙧 𝙘𝙤𝙣 𝙡𝙤𝙨 𝙙𝙚𝙡 𝙨𝙚𝙧𝙫𝙚𝙧?:",
        "→ 𝘾𝙪é𝙣𝙩𝙖𝙣𝙤𝙨 𝙖𝙡𝙜𝙤 𝙙𝙚 𝙩𝙞 (𝙊𝙥𝙘𝙞𝙤𝙣𝙖𝙡):",
        "→ 𝑪𝒐𝒎𝒐 𝒏𝒐𝒔 𝒉𝒂𝒔 𝒄𝒐𝒏𝒐𝒄𝒊𝒅𝒐?:",
        "→ 𝑭𝒊𝒓𝒎𝒂 (𝑷𝒐𝒏 𝒍𝒐 𝒒𝒖𝒆 𝒒𝒖𝒊𝒆𝒓𝒂𝒔):",
    )
)

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
        name=".noah presentations sticky on / off",
        value=(
            "Deja siempre al final del canal de presentaciones un mensaje con un botón "
            "que enseña a presentarse con la plantilla."
        ),
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


async def find_presentation(
    store: PresentationsStore,
    guild: discord.Guild,
    user_id: int,
) -> discord.Message | None:
    """Busca la presentación guardada del usuario o, si falta, la busca en el historial."""
    stored = store.get_presentation_message(guild.id, user_id)
    if stored is not None:
        presentation = await _fetch_presentation_message(guild, *stored)
        if presentation is not None:
            return presentation
        store.forget_presentation_message(guild.id, user_id)

    channel_id = store.get_channel_id(guild.id)
    channel = guild.get_channel(channel_id) if channel_id is not None else None
    if not isinstance(channel, discord.TextChannel):
        return None

    presentation = await _search_presentation_message(channel, user_id)
    if presentation is not None:
        store.record_presentations(
            guild.id,
            {user_id: (presentation.channel.id, presentation.id)},
        )
    return presentation


def build_presentation_embed(
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


class PresentationStickyView(discord.ui.View):
    """Botón del mensaje fijo. Persistente: sigue funcionando tras reiniciar el bot."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Presentarse",
        style=discord.ButtonStyle.secondary,
        custom_id=PRESENTATION_STICKY_BUTTON_ID,
    )
    async def introduce(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        _ = button
        channel_id = interaction.channel_id
        if interaction.guild is not None and interaction.client is not None:
            stored_channel_id = get_bot_context(interaction.client).presentations.get_channel_id(
                interaction.guild.id
            )
            channel_id = stored_channel_id or channel_id

        await interaction.response.send_message(
            "\n".join(
                [
                    "¡Presentarse es súper fácil! 🖤",
                    "1. Copia la plantilla que te dejo justo debajo.",
                    "2. Rellénala con tus datos.",
                    f"3. Envíala aquí mismo, en <#{channel_id}>.",
                    "",
                    "¡Tengo muchas ganas de conocerte! <3",
                ]
            ),
            ephemeral=True,
        )
        await interaction.followup.send(PRESENTATION_TEMPLATE, ephemeral=True)


def _is_sticky_message(bot: commands.Bot, message: discord.Message) -> bool:
    if bot.user is None or message.author.id != bot.user.id:
        return False
    return any(
        getattr(child, "custom_id", None) == PRESENTATION_STICKY_BUTTON_ID
        for row in message.components
        for child in getattr(row, "children", [])
    )


async def _delete_sticky_message(store: PresentationsStore, guild: discord.Guild) -> None:
    stored = store.get_sticky_message(guild.id)
    if stored is None:
        return

    channel_id, message_id = stored
    channel = guild.get_channel_or_thread(channel_id)
    if channel is not None and hasattr(channel, "get_partial_message"):
        with suppress(discord.HTTPException):
            await channel.get_partial_message(message_id).delete()
    store.set_sticky_message(guild.id, None, None)


async def _post_sticky_message(
    store: PresentationsStore,
    guild: discord.Guild,
) -> discord.Message | None:
    """Borra el mensaje fijo anterior y lo vuelve a publicar al final del canal."""
    channel_id = store.get_channel_id(guild.id)
    channel = guild.get_channel(channel_id) if channel_id is not None else None
    if not isinstance(channel, discord.TextChannel):
        return None

    await _delete_sticky_message(store, guild)

    embed = build_quote_text_embed(
        guild.me,
        PRESENTATION_STICKY_TEXT,
        discord.utils.utcnow(),
    )
    try:
        sticky = await channel.send(
            embed=embed,
            view=PresentationStickyView(),
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except discord.HTTPException:
        return None

    store.set_sticky_message(guild.id, channel.id, sticky.id)
    return sticky


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
    sticky_view_registered = False
    sticky_locks: dict[int, asyncio.Lock] = {}
    sticky_tasks: dict[int, asyncio.Task] = {}
    sticky_dirty: set[int] = set()

    async def _repost_sticky(guild: discord.Guild) -> discord.Message | None:
        lock = sticky_locks.setdefault(guild.id, asyncio.Lock())
        async with lock:
            store = get_bot_context(bot).presentations
            if not store.is_sticky_enabled(guild.id):
                return None
            return await _post_sticky_message(store, guild)

    async def _sticky_repost_worker(guild: discord.Guild) -> None:
        try:
            while True:
                await asyncio.sleep(PRESENTATION_STICKY_REPOST_DELAY)
                sticky_dirty.discard(guild.id)
                await _repost_sticky(guild)
                # Si llegó otro mensaje mientras se reenviaba, hay que repetir.
                if guild.id not in sticky_dirty:
                    return
        finally:
            if sticky_tasks.get(guild.id) is asyncio.current_task():
                sticky_tasks.pop(guild.id, None)

    def _schedule_sticky_repost(guild: discord.Guild) -> None:
        """Agrupa ráfagas de mensajes en un único reenvío del mensaje fijo."""
        sticky_dirty.add(guild.id)
        pending = sticky_tasks.get(guild.id)
        if pending is not None and not pending.done():
            return
        sticky_tasks[guild.id] = asyncio.create_task(_sticky_repost_worker(guild))

    async def _remove_sticky(guild: discord.Guild) -> None:
        lock = sticky_locks.setdefault(guild.id, asyncio.Lock())
        async with lock:
            await _delete_sticky_message(get_bot_context(bot).presentations, guild)

    @bot.listen("on_ready")
    async def _restore_presentation_sticky() -> None:
        nonlocal sticky_view_registered
        if sticky_view_registered:
            return
        bot.add_view(PresentationStickyView())
        sticky_view_registered = True

        # Si alguien escribió mientras el bot estaba apagado, vuelve a dejarlo el último.
        store = get_bot_context(bot).presentations
        for guild_id in store.get_sticky_guild_ids():
            guild = bot.get_guild(guild_id)
            channel_id = store.get_channel_id(guild_id)
            channel = guild.get_channel(channel_id) if guild and channel_id else None
            if not isinstance(channel, discord.TextChannel):
                continue

            stored = store.get_sticky_message(guild_id)
            if stored is not None and stored == (channel.id, channel.last_message_id):
                continue
            await _repost_sticky(guild)

    @bot.listen("on_message")
    async def _keep_presentation_sticky_last(message: discord.Message) -> None:
        if message.guild is None or _is_sticky_message(bot, message):
            return

        store = get_bot_context(bot).presentations
        if not store.is_sticky_enabled(message.guild.id):
            return

        if message.channel.id != store.get_channel_id(message.guild.id):
            return

        if is_guard_active(bot, message.guild.id):
            return

        _schedule_sticky_repost(message.guild)

    @bot.listen("on_message")
    async def _handle_presentations(message: discord.Message) -> None:
        if message.author.bot or message.guild is None:
            return

        if is_guard_active(bot, message.guild.id):
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
        if store.is_sticky_enabled(ctx.guild.id):
            await _repost_sticky(ctx.guild)

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
    async def sticky(ctx: commands.Context, mode: str | None = None) -> None:
        if not await _check_admin(ctx):
            return

        store = get_bot_context(ctx.bot).presentations
        normalized = (mode or "").strip().casefold()

        if normalized in {"on", "enable", "activar"}:
            channel_id = store.get_channel_id(ctx.guild.id)
            if channel_id is None:
                await ctx.send(
                    "❌ Primero configura el canal con "
                    "`.noah presentations setchannel <#canal>`."
                )
                return

            store.set_sticky_enabled(ctx.guild.id, True)
            sticky_message = await _repost_sticky(ctx.guild)
            if sticky_message is None:
                await ctx.send(
                    f"⚠️ Mensaje fijo activado, pero no he podido publicarlo en <#{channel_id}>. "
                    "Revisa que pueda escribir y enviar embeds allí."
                )
                return

            await ctx.send(
                f"✅ Mensaje fijo de presentaciones activado en <#{channel_id}>. "
                "Se quedará siempre el último del canal."
            )
            return

        if normalized in {"off", "disable", "desactivar"}:
            store.set_sticky_enabled(ctx.guild.id, False)
            await _remove_sticky(ctx.guild)
            await ctx.send("✅ Mensaje fijo de presentaciones desactivado.")
            return

        await ctx.send("❌ Usa `.noah presentations sticky on` o `.noah presentations sticky off`.")

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
            name="Mensaje fijo",
            value="Activado" if store.is_sticky_enabled(ctx.guild.id) else "Desactivado",
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

        async with ctx.typing():
            presentation = await find_presentation(store, ctx.guild, target.id)

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
            embed=build_presentation_embed(target, presentation),
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
