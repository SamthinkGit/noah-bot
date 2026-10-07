from contextlib import suppress
from typing import Any

import discord
from discord.ext import commands

from noah_bot.commands.presentations import build_presentation_embed, find_presentation
from noah_bot.modules.bot_context import get_bot_context
from noah_bot.modules.discord_formatter import build_quote_embed
from noah_bot.modules.rrpp import RrppStore


SELECT_LIMIT = 25
FIELD_LIMIT = 1024
RRPP_COLOR = discord.Color.from_rgb(235, 69, 158)


def _is_rrpp_admin(member: discord.abc.User) -> bool:
    return (
        isinstance(member, discord.Member)
        and member.guild_permissions.administrator
    )


def _emoji_matches(stored: str, emoji: discord.PartialEmoji) -> bool:
    expected = discord.PartialEmoji.from_str(stored)
    if expected.id is not None or emoji.id is not None:
        return expected.id == emoji.id
    return (expected.name or "").replace("️", "") == (emoji.name or "").replace(
        "️", ""
    )


def _jump_url(guild_id: int, channel_id: Any, message_id: int) -> str:
    return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"


def _clip_lines(lines: list[str], empty: str) -> str:
    if not lines:
        return empty

    kept: list[str] = []
    length = 0
    for index, line in enumerate(lines):
        more_line = f"… y {len(lines) - index} más"
        if length + len(line) + 1 + len(more_line) > FIELD_LIMIT:
            kept.append(more_line)
            break
        kept.append(line)
        length += len(line) + 1
    return "\n".join(kept)


def _role_problem(guild: discord.Guild, role: discord.Role) -> str | None:
    """Devuelve por qué Noah no puede gestionar el rol, o None si puede."""
    me = guild.me
    if role.managed or role.is_default():
        return f"El rol {role.mention} lo gestiona Discord o una integración."
    if not (me.guild_permissions.administrator or me.guild_permissions.manage_roles):
        return "Me falta el permiso **Gestionar roles**."
    if role >= me.top_role:
        return f"El rol {role.mention} está por encima de mi rol más alto."
    return None


def _build_help_embed() -> discord.Embed:
    embed = discord.Embed(
        title="RRPP",
        description=(
            "Publica citas con una reacción. Cuando alguien reacciona, se avisa a los "
            "RRPP en su canal oculto para que puedan contactar con esa persona. "
            "Solo administradores."
        ),
        color=RRPP_COLOR,
    )
    embed.add_field(
        name=".noah rrpp",
        value="Abre el panel: rol RRPP, canal de avisos, miembros RRPP, emoji y mensajes activos.",
        inline=False,
    )
    embed.add_field(
        name=".noah rrpp quote <@user>",
        value=(
            "Respondiendo a un mensaje: lo cita como `.noah quote` y añade la reacción RRPP."
        ),
        inline=False,
    )
    embed.add_field(
        name=".noah rrpp help",
        value="Muestra esta ayuda.",
        inline=False,
    )
    return embed


def _build_panel_embed(guild: discord.Guild, store: RrppStore) -> discord.Embed:
    role_id = store.get_role_id(guild.id)
    role = guild.get_role(role_id) if role_id is not None else None
    channel_id = store.get_channel_id(guild.id)
    channel = guild.get_channel(channel_id) if channel_id is not None else None

    embed = discord.Embed(
        title="Panel RRPP",
        description=(
            "Elige el rol RRPP, el canal oculto de avisos y quién es RRPP. "
            "El selector de miembros da o quita el rol (marca la lista completa)."
        ),
        color=RRPP_COLOR,
    )
    embed.add_field(
        name="Rol",
        value=role.mention if role is not None else "Sin configurar",
        inline=True,
    )
    embed.add_field(
        name="Canal de avisos",
        value=channel.mention if channel is not None else "Sin configurar",
        inline=True,
    )
    embed.add_field(name="Emoji", value=store.get_emoji(guild.id), inline=True)

    if role is not None:
        members = sorted(role.members, key=lambda member: member.display_name.casefold())
        embed.add_field(
            name=f"RRPPs ({len(members)})",
            value=_clip_lines([member.mention for member in members], "Nadie."),
            inline=False,
        )
        problem = _role_problem(guild, role)
        if problem is not None:
            embed.add_field(name="⚠️ No puedo asignar el rol", value=problem, inline=False)

    message_lines: list[str] = []
    for message_id, payload in store.list_messages(guild.id):
        notified = payload.get("notified_users")
        count = len(notified) if isinstance(notified, list) else 0
        created_at = int(payload.get("created_at", 0))
        message_lines.append(
            f"[Cita de <@{payload.get('quoted_user_id')}>]"
            f"({_jump_url(guild.id, payload.get('channel_id'), message_id)}) · "
            f"<t:{created_at}:d> · {payload.get('emoji')} `{count}` avisos"
        )
    embed.add_field(
        name=f"Mensajes activos ({len(message_lines)})",
        value=_clip_lines(message_lines, "Ninguno. Usa `.noah rrpp quote <@user>`."),
        inline=False,
    )
    embed.set_footer(text="Cambiar el emoji solo afecta a los mensajes nuevos.")
    return embed


async def _build_notice(
    bot: commands.Bot,
    guild: discord.Guild,
    member: discord.Member,
    role: discord.Role,
    record: dict[str, Any],
) -> tuple[str, list[discord.Embed]]:
    content = (
        f"{role.mention} {member.mention} ha reaccionado al mensaje RRPP "
        f"de <@{record.get('quoted_user_id')}>. ¡Podéis contactar con esta persona!"
    )

    embed = discord.Embed(
        color=member.color if member.color.value else RRPP_COLOR,
        timestamp=discord.utils.utcnow(),
    )
    embed.set_author(name=member.display_name, icon_url=member.display_avatar.url)
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="Usuario", value=f"{member.mention}\n`{member.id}`", inline=False)

    if member.joined_at is not None:
        joined = int(member.joined_at.timestamp())
        embed.add_field(
            name="Entró al servidor",
            value=f"<t:{joined}:F>\n<t:{joined}:R>",
            inline=True,
        )
    created = int(member.created_at.timestamp())
    embed.add_field(
        name="Cuenta creada",
        value=f"<t:{created}:F>\n<t:{created}:R>",
        inline=True,
    )

    presentations = get_bot_context(bot).presentations
    presentation: discord.Message | None = None
    if presentations.get_channel_id(guild.id) is None:
        status = "Presentaciones sin configurar"
    elif not presentations.has_completed(guild.id, member.id):
        status = "❌ Sin presentar"
    else:
        presentation = await find_presentation(presentations, guild, member.id)
        status = (
            "✅ Presentado (abajo)"
            if presentation is not None
            else "✅ Presentado, pero no encuentro su mensaje"
        )
    embed.add_field(name="Presentación", value=status, inline=False)

    embeds = [embed]
    if presentation is not None:
        embeds.append(build_presentation_embed(member, presentation))
    return content, embeds


class RrppEmojiModal(discord.ui.Modal, title="Emoji de la reacción RRPP"):
    def __init__(self, panel: "RrppPanelView") -> None:
        super().__init__()
        self.panel = panel
        self.emoji_input = discord.ui.TextInput(
            label="Emoji (Unicode o emoji del servidor)",
            placeholder="📩  ·  :nombre_emoji:  ·  <:nombre:id>",
            default=panel.store.get_emoji(panel.guild.id),
            max_length=100,
        )
        self.add_item(self.emoji_input)

    def _resolve(self, raw: str) -> str:
        raw = raw.strip()
        name = raw.strip(":")
        custom = discord.utils.get(self.panel.guild.emojis, name=name)
        if custom is not None:
            return str(custom)
        return raw

    async def on_submit(self, interaction: discord.Interaction) -> None:
        emoji = self._resolve(self.emoji_input.value)
        message = self.panel.message or interaction.message
        try:
            if message is None:
                raise discord.HTTPException
            await message.add_reaction(emoji)
            with suppress(discord.HTTPException):
                await message.remove_reaction(emoji, self.panel.guild.me)
        except (discord.HTTPException, TypeError):
            await interaction.response.send_message(
                f"❌ No puedo usar `{emoji}` como reacción. Usa un emoji Unicode o uno de este servidor.",
                ephemeral=True,
            )
            return

        self.panel.store.set_emoji(self.panel.guild.id, emoji)
        await self.panel.refresh(interaction)


class RrppPanelView(discord.ui.View):
    def __init__(self, bot: commands.Bot, guild: discord.Guild, author_id: int) -> None:
        super().__init__(timeout=300)
        self.bot = bot
        self.guild = guild
        self.author_id = author_id
        self.store = get_bot_context(bot).rrpp
        self.message: discord.Message | None = None

        role_id = self.store.get_role_id(guild.id)
        self.role = guild.get_role(role_id) if role_id is not None else None
        channel_id = self.store.get_channel_id(guild.id)

        self.role_select = discord.ui.RoleSelect(
            placeholder="Rol RRPP",
            min_values=1,
            max_values=1,
            default_values=[self.role] if self.role is not None else [],
            row=0,
        )
        self.role_select.callback = self._on_role
        self.add_item(self.role_select)

        channel = guild.get_channel(channel_id) if channel_id is not None else None
        self.channel_select = discord.ui.ChannelSelect(
            placeholder="Canal oculto de avisos",
            channel_types=[discord.ChannelType.text],
            min_values=1,
            max_values=1,
            default_values=[channel] if channel is not None else [],
            row=1,
        )
        self.channel_select.callback = self._on_channel
        self.add_item(self.channel_select)

        # Solo se pueden quitar los que aparecen marcados por defecto en el selector.
        self.shown_member_ids: set[int] = set()
        if self.role is not None:
            shown = sorted(
                self.role.members, key=lambda member: member.display_name.casefold()
            )[:SELECT_LIMIT]
            self.shown_member_ids = {member.id for member in shown}
            self.member_select = discord.ui.UserSelect(
                placeholder="Miembros RRPP (marca la lista completa)",
                min_values=0,
                max_values=SELECT_LIMIT,
                default_values=shown,
                row=2,
            )
            self.member_select.callback = self._on_members
            self.add_item(self.member_select)

        messages = self.store.list_messages(guild.id)[:SELECT_LIMIT]
        if messages:
            options: list[discord.SelectOption] = []
            for message_id, payload in messages:
                quoted = guild.get_member(int(payload.get("quoted_user_id", 0)))
                quoted_name = quoted.display_name if quoted is not None else "usuario"
                channel_obj = guild.get_channel(int(payload.get("channel_id", 0)))
                channel_name = f"#{channel_obj.name}" if channel_obj is not None else "canal"
                notified = payload.get("notified_users")
                count = len(notified) if isinstance(notified, list) else 0
                options.append(
                    discord.SelectOption(
                        label=f"Cita de {quoted_name}"[:100],
                        description=f"{channel_name} · {count} avisos"[:100],
                        value=str(message_id),
                    )
                )
            self.message_select = discord.ui.Select(
                placeholder="Desactivar un mensaje RRPP",
                min_values=1,
                max_values=1,
                options=options,
                row=3,
            )
            self.message_select.callback = self._on_message_disable
            self.add_item(self.message_select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Solo quien ha abierto el panel puede usarlo.",
                ephemeral=True,
            )
            return False
        return True

    async def refresh(self, interaction: discord.Interaction) -> None:
        view = RrppPanelView(self.bot, self.guild, self.author_id)
        view.message = self.message
        self.stop()
        embed = _build_panel_embed(self.guild, self.store)
        if interaction.response.is_done():
            await interaction.edit_original_response(embed=embed, view=view)
        else:
            await interaction.response.edit_message(embed=embed, view=view)

    async def _on_role(self, interaction: discord.Interaction) -> None:
        role = self.role_select.values[0]
        if role.is_default():
            await interaction.response.send_message(
                "❌ No puedes usar @everyone como rol RRPP.", ephemeral=True
            )
            return
        self.store.set_role(self.guild.id, role.id)
        await self.refresh(interaction)

    async def _on_channel(self, interaction: discord.Interaction) -> None:
        self.store.set_channel(self.guild.id, self.channel_select.values[0].id)
        await self.refresh(interaction)

    async def _on_members(self, interaction: discord.Interaction) -> None:
        role = self.role
        if role is None:
            await self.refresh(interaction)
            return

        problem = _role_problem(self.guild, role)
        if problem is not None:
            await interaction.response.send_message(f"❌ {problem}", ephemeral=True)
            return

        await interaction.response.defer()
        selected_ids = {user.id for user in self.member_select.values}
        current_ids = {member.id for member in role.members}
        to_add = selected_ids - current_ids
        to_remove = self.shown_member_ids - selected_ids

        failed: list[str] = []
        reason = f"Panel RRPP ({interaction.user})"
        for user_id in to_add:
            member = self.guild.get_member(user_id)
            if member is None or member.bot:
                failed.append(f"<@{user_id}>")
                continue
            try:
                await member.add_roles(role, reason=reason)
            except discord.HTTPException:
                failed.append(member.mention)

        for user_id in to_remove:
            member = self.guild.get_member(user_id)
            if member is None:
                continue
            try:
                await member.remove_roles(role, reason=reason)
            except discord.HTTPException:
                failed.append(member.mention)

        await self.refresh(interaction)
        if failed:
            await interaction.followup.send(
                f"⚠️ No he podido cambiar el rol de: {', '.join(failed)}",
                ephemeral=True,
            )

    async def _on_message_disable(self, interaction: discord.Interaction) -> None:
        message_id = int(self.message_select.values[0])
        record = self.store.get_message(self.guild.id, message_id)
        self.store.remove_message(self.guild.id, message_id)

        if record is not None:
            channel = self.guild.get_channel(int(record.get("channel_id", 0)))
            if isinstance(channel, discord.TextChannel):
                with suppress(discord.HTTPException):
                    message = await channel.fetch_message(message_id)
                    await message.remove_reaction(record.get("emoji"), self.guild.me)

        await self.refresh(interaction)

    @discord.ui.button(label="Emoji", style=discord.ButtonStyle.secondary, row=4)
    async def emoji(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        _ = button
        await interaction.response.send_modal(RrppEmojiModal(self))

    @discord.ui.button(label="Cerrar", style=discord.ButtonStyle.secondary, row=4)
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        _ = button
        self.stop()
        await interaction.response.edit_message(view=None)

    async def on_timeout(self) -> None:
        if self.message is not None:
            with suppress(discord.HTTPException):
                await self.message.edit(view=None)


def register_rrpp_commands(bot: commands.Bot, noah_group: commands.Group) -> None:
    @bot.listen("on_raw_reaction_add")
    async def _handle_rrpp_reaction(payload: discord.RawReactionActionEvent) -> None:
        if payload.guild_id is None or bot.user is None or payload.user_id == bot.user.id:
            return

        store = get_bot_context(bot).rrpp
        record = store.get_message(payload.guild_id, payload.message_id)
        if record is None or not _emoji_matches(str(record.get("emoji")), payload.emoji):
            return

        guild = bot.get_guild(payload.guild_id)
        if guild is None:
            return

        role_id = store.get_role_id(guild.id)
        channel_id = store.get_channel_id(guild.id)
        role = guild.get_role(role_id) if role_id is not None else None
        channel = guild.get_channel(channel_id) if channel_id is not None else None
        if role is None or not isinstance(channel, discord.TextChannel):
            return

        member = payload.member or guild.get_member(payload.user_id)
        if member is None:
            return

        if not store.mark_notified(guild.id, payload.message_id, member.id):
            return

        content, embeds = await _build_notice(bot, guild, member, role, record)
        try:
            await channel.send(
                content,
                embeds=embeds,
                allowed_mentions=discord.AllowedMentions(
                    everyone=False, users=False, roles=[role]
                ),
            )
        except discord.HTTPException:
            store.unmark_notified(guild.id, payload.message_id, member.id)

    @bot.listen("on_raw_message_delete")
    async def _forget_rrpp_message(payload: discord.RawMessageDeleteEvent) -> None:
        if payload.guild_id is not None:
            get_bot_context(bot).rrpp.remove_message(payload.guild_id, payload.message_id)

    async def _check_admin(ctx: commands.Context) -> bool:
        if ctx.guild is None:
            await ctx.send("❌ Este comando solo funciona dentro de un servidor.")
            return False

        if not _is_rrpp_admin(ctx.author):
            await ctx.send("❌ Solo los administradores pueden usar este comando.")
            return False

        return True

    @noah_group.group(invoke_without_command=True)
    async def rrpp(ctx: commands.Context) -> None:
        if not await _check_admin(ctx):
            return

        view = RrppPanelView(ctx.bot, ctx.guild, ctx.author.id)
        store = get_bot_context(ctx.bot).rrpp
        view.message = await ctx.send(embed=_build_panel_embed(ctx.guild, store), view=view)

    @rrpp.command()
    async def help(ctx: commands.Context) -> None:
        await ctx.send(embed=_build_help_embed())

    @rrpp.command()
    async def quote(
        ctx: commands.Context, user: discord.Member | discord.User | None = None
    ) -> None:
        if not await _check_admin(ctx):
            return

        store = get_bot_context(ctx.bot).rrpp
        role_id = store.get_role_id(ctx.guild.id)
        channel_id = store.get_channel_id(ctx.guild.id)
        if (
            role_id is None
            or ctx.guild.get_role(role_id) is None
            or channel_id is None
            or ctx.guild.get_channel(channel_id) is None
        ):
            await ctx.send("❌ Primero configura el rol y el canal RRPP con `.noah rrpp`.")
            return

        if user is None:
            await ctx.send("❌ Usa `.noah rrpp quote <user>` respondiendo a un mensaje.")
            return

        if not ctx.message.reference:
            await ctx.send("❌ Tienes que responder a un mensaje para citarlo.")
            return

        try:
            replied_msg = await ctx.channel.fetch_message(ctx.message.reference.message_id)
        except discord.NotFound:
            await ctx.send("❌ No encuentro el mensaje original.")
            return

        if not replied_msg.content.strip():
            await ctx.send("❌ El mensaje no tiene texto que citar.")
            return

        emoji = store.get_emoji(ctx.guild.id)
        sent_message = await ctx.send(embed=build_quote_embed(user, replied_msg))

        try:
            await sent_message.add_reaction(emoji)
        except discord.HTTPException:
            with suppress(discord.HTTPException):
                await sent_message.delete()
            await ctx.send(
                f"❌ No puedo reaccionar con {emoji}. Cambia el emoji en `.noah rrpp`."
            )
            return

        store.add_message(ctx.guild.id, ctx.channel.id, sent_message.id, user.id, emoji)

        with suppress(discord.Forbidden, discord.NotFound, discord.HTTPException):
            await ctx.message.delete()
        with suppress(discord.Forbidden, discord.NotFound, discord.HTTPException):
            await replied_msg.delete()

    @quote.error
    async def _rrpp_quote_error(ctx: commands.Context, error: commands.CommandError) -> None:
        if isinstance(error, (commands.MemberNotFound, commands.UserNotFound)):
            await ctx.send(f"❌ {error}")
