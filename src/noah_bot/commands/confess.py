import asyncio
from contextlib import suppress

import discord
from discord.ext import commands

from noah_bot.modules.bot_context import get_bot_context, is_guard_active
from noah_bot.modules.confessions import ConfessionsStore

CONFESS_COLOR = discord.Color.from_rgb(230, 126, 34)
PANEL_BUTTON_ID = "noah:confess:open"
CONFESSION_LIMIT = 2000
DESCRIPTION_LIMIT = 4096
PROMPT_TIMEOUT = 60
PANEL_STICKY_REPOST_DELAY = 2.0
PANEL_STICKY_NOTICE_DELETE_AFTER = 8
PANEL_STICKY_HISTORY_LOOKUP = 10

# Evita que dos publicaciones del panel a la vez dejen paneles duplicados.
_panel_locks: dict[int, asyncio.Lock] = {}


def _is_confess_admin(member: discord.abc.User) -> bool:
    return (
        isinstance(member, discord.Member)
        and member.guild_permissions.administrator
    )


def _format_confession(text: str) -> str:
    """Pone cada línea en cursiva, como en el ejemplo, sin romper con `*` sueltos."""
    lines = []
    for line in text.strip().splitlines():
        line = line.strip().replace("*", "\\*")
        lines.append(f"*{line}*" if line else "")

    formatted = "\n".join(lines)
    if len(formatted) > DESCRIPTION_LIMIT:
        return text.strip()[:DESCRIPTION_LIMIT]
    return formatted


def build_confession_embed(guild: discord.Guild, number: int, text: str) -> discord.Embed:
    embed = discord.Embed(
        title=f"Confesión anónima (#{number})",
        description=_format_confession(text),
        color=CONFESS_COLOR,
    )
    embed.set_footer(
        text=guild.name,
        icon_url=guild.icon.url if guild.icon is not None else None,
    )
    return embed


def _confessions_channel(
    guild: discord.Guild, store: ConfessionsStore
) -> discord.TextChannel | None:
    channel_id = store.get_channel_id(guild.id)
    channel = guild.get_channel(channel_id) if channel_id is not None else None
    return channel if isinstance(channel, discord.TextChannel) else None


def _channel_problem(channel: discord.TextChannel) -> str | None:
    permissions = channel.permissions_for(channel.guild.me)
    if not (permissions.send_messages and permissions.embed_links):
        return f"No puedo enviar mensajes con embeds en {channel.mention}."
    return None


def _jump_url(guild_id: int, channel_id: int, message_id: int) -> str:
    return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"


class ConfessionModal(discord.ui.Modal, title="Confesión anónima"):
    confession = discord.ui.TextInput(
        label="Tu confesión",
        style=discord.TextStyle.paragraph,
        placeholder="Nadie sabrá que la has escrito tú, ni siquiera los admins.",
        min_length=1,
        max_length=CONFESSION_LIMIT,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "❌ Las confesiones solo funcionan dentro de un servidor.", ephemeral=True
            )
            return

        if is_guard_active(interaction.client, guild.id):
            await interaction.response.send_message(
                "⛔ Las confesiones están pausadas por el protocolo de contingencia.",
                ephemeral=True,
            )
            return

        text = self.confession.value.strip()
        if not text:
            await interaction.response.send_message(
                "❌ La confesión está vacía.", ephemeral=True
            )
            return

        store = get_bot_context(interaction.client).confessions
        channel = _confessions_channel(guild, store)
        problem = (
            "El canal de confesiones no está configurado."
            if channel is None
            else _channel_problem(channel)
        )
        if problem is not None:
            await interaction.response.send_message(f"❌ {problem}", ephemeral=True)
            return

        number = store.next_number(guild.id)
        sent = await channel.send(
            embed=build_confession_embed(guild, number, text),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await interaction.response.send_message(
            f"✅ Tu confesión anónima [#{number}]({sent.jump_url}) se ha publicado en "
            f"{channel.mention}.",
            ephemeral=True,
        )

    async def on_error(
        self, interaction: discord.Interaction, error: Exception
    ) -> None:
        _ = error
        message = "❌ No he podido publicar la confesión. Inténtalo de nuevo más tarde."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


class ConfessPanelView(discord.ui.View):
    """Botón fijo del panel. Persistente: sigue funcionando tras reiniciar el bot."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Escribir confesión",
        emoji="📝",
        style=discord.ButtonStyle.secondary,
        custom_id=PANEL_BUTTON_ID,
    )
    async def open(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        _ = button
        await interaction.response.send_modal(ConfessionModal())


class ConfessPromptView(discord.ui.View):
    """Botón temporal de `.noah confess`: solo lo puede usar quien lo ha pedido."""

    def __init__(self, author_id: int) -> None:
        super().__init__(timeout=PROMPT_TIMEOUT)
        self.author_id = author_id
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Este botón no es para ti. Usa `.noah confess` o el panel de confesiones.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(
        label="Escribir confesión", emoji="📝", style=discord.ButtonStyle.secondary
    )
    async def open(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        _ = button
        await interaction.response.send_modal(ConfessionModal())
        self.stop()
        with suppress(discord.HTTPException):
            await interaction.message.delete()

    async def on_timeout(self) -> None:
        if self.message is not None:
            with suppress(discord.HTTPException):
                await self.message.delete()


def _build_panel_embed(confessions_channel: discord.TextChannel | None) -> discord.Embed:
    target = (
        confessions_channel.mention
        if confessions_channel is not None
        else "el canal de confesiones"
    )
    return discord.Embed(
        title="Confesiones anónimas",
        description=(
            "Pulsa el botón y escribe tu confesión en el formulario. "
            f"Se publicará en {target} de forma totalmente anónima: "
            "nadie ve que la has escrito y el bot no guarda quién la envía."
        ),
        color=CONFESS_COLOR,
    )


def _build_help_embed() -> discord.Embed:
    embed = discord.Embed(
        title="Confesiones",
        description=(
            "Confesiones totalmente anónimas: se escriben en un formulario privado y "
            "se publican sin ningún dato del autor. El bot no guarda quién las envía."
        ),
        color=CONFESS_COLOR,
    )
    embed.add_field(
        name="Panel con botón",
        value="La forma más discreta: pulsa **Escribir confesión** y no queda nada en el chat.",
        inline=False,
    )
    embed.add_field(
        name=".noah confess",
        value=(
            "Borra tu mensaje al momento y deja un botón temporal que solo tú puedes "
            f"usar (desaparece en {PROMPT_TIMEOUT}s)."
        ),
        inline=False,
    )
    embed.add_field(
        name=".noah confess config",
        value="Elige el canal de confesiones y dónde va el panel (solo admins).",
        inline=False,
    )
    embed.add_field(
        name=".noah confess sticky [on / off]",
        value=(
            "Mantiene el panel siempre como último mensaje de su canal: cada vez que "
            "alguien escribe, se borra y se vuelve a publicar abajo (solo admins)."
        ),
        inline=False,
    )
    return embed


def _build_config_embed(guild: discord.Guild, store: ConfessionsStore) -> discord.Embed:
    channel = _confessions_channel(guild, store)
    panel_channel_id, panel_message_id = store.get_panel(guild.id)

    embed = discord.Embed(
        title="Configuración de confesiones",
        description=(
            "Elige el canal donde se publican las confesiones y el canal donde poner "
            "el panel con el botón. Al elegir el canal del panel se publica ahí."
        ),
        color=CONFESS_COLOR,
    )
    embed.add_field(
        name="Canal de confesiones",
        value=channel.mention if channel is not None else "Sin configurar",
        inline=True,
    )
    embed.add_field(
        name="Panel",
        value=(
            f"[Ver panel]({_jump_url(guild.id, panel_channel_id, panel_message_id)})"
            if panel_channel_id is not None and panel_message_id is not None
            else "Sin publicar"
        ),
        inline=True,
    )
    embed.add_field(
        name="Mensaje fijo",
        value="Activado" if store.is_sticky(guild.id) else "Desactivado",
        inline=True,
    )
    embed.add_field(
        name="Confesiones enviadas", value=f"`{store.get_count(guild.id)}`", inline=True
    )

    if channel is not None:
        problem = _channel_problem(channel)
        if problem is not None:
            embed.add_field(name="⚠️ Problema", value=problem, inline=False)
    return embed


async def _publish_panel(
    guild: discord.Guild, store: ConfessionsStore, channel: discord.TextChannel
) -> str | None:
    """Publica el panel en `channel` y borra el anterior. Devuelve el error, si hay."""
    async with _panel_locks.setdefault(guild.id, asyncio.Lock()):
        return await _publish_panel_unlocked(guild, store, channel)


async def _publish_panel_unlocked(
    guild: discord.Guild, store: ConfessionsStore, channel: discord.TextChannel
) -> str | None:
    problem = _channel_problem(channel)
    if problem is not None:
        return problem

    try:
        message = await channel.send(
            embed=_build_panel_embed(_confessions_channel(guild, store)),
            view=ConfessPanelView(),
        )
    except discord.HTTPException:
        return f"No he podido publicar el panel en {channel.mention}."

    old_channel_id, old_message_id = store.get_panel(guild.id)
    store.set_panel(guild.id, channel.id, message.id)

    old_channel = guild.get_channel(old_channel_id) if old_channel_id is not None else None
    if isinstance(old_channel, discord.TextChannel) and old_message_id is not None:
        with suppress(discord.HTTPException):
            await old_channel.get_partial_message(old_message_id).delete()
    return None


class ConfessConfigView(discord.ui.View):
    def __init__(self, guild: discord.Guild, store: ConfessionsStore, author_id: int) -> None:
        super().__init__(timeout=300)
        self.guild = guild
        self.store = store
        self.author_id = author_id
        self.message: discord.Message | None = None

        channel = _confessions_channel(guild, store)
        self.channel_select = discord.ui.ChannelSelect(
            placeholder="Canal donde se publican las confesiones",
            channel_types=[discord.ChannelType.text],
            min_values=1,
            max_values=1,
            default_values=[channel] if channel is not None else [],
            row=0,
        )
        self.channel_select.callback = self._on_channel
        self.add_item(self.channel_select)

        panel_channel_id, _ = store.get_panel(guild.id)
        panel_channel = (
            guild.get_channel(panel_channel_id) if panel_channel_id is not None else None
        )
        self.panel_select = discord.ui.ChannelSelect(
            placeholder="Canal donde publicar el panel con el botón",
            channel_types=[discord.ChannelType.text],
            min_values=1,
            max_values=1,
            default_values=[panel_channel] if panel_channel is not None else [],
            row=1,
        )
        self.panel_select.callback = self._on_panel
        self.add_item(self.panel_select)

        self.toggle_sticky.label = (
            "Mensaje fijo: activado" if store.is_sticky(guild.id) else "Mensaje fijo: desactivado"
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Solo quien ha abierto la configuración puede usarla.",
                ephemeral=True,
            )
            return False
        return True

    async def refresh(self, interaction: discord.Interaction) -> None:
        view = ConfessConfigView(self.guild, self.store, self.author_id)
        view.message = self.message
        self.stop()
        embed = _build_config_embed(self.guild, self.store)
        if interaction.response.is_done():
            await interaction.edit_original_response(embed=embed, view=view)
        else:
            await interaction.response.edit_message(embed=embed, view=view)

    async def _republish(self, interaction: discord.Interaction, channel_id: int) -> None:
        channel = self.guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            await interaction.followup.send("❌ No encuentro ese canal.", ephemeral=True)
            return

        problem = await _publish_panel(self.guild, self.store, channel)
        if problem is not None:
            await interaction.followup.send(f"❌ {problem}", ephemeral=True)

    async def _on_channel(self, interaction: discord.Interaction) -> None:
        self.store.set_channel(self.guild.id, self.channel_select.values[0].id)

        # El panel menciona el canal de confesiones, así que se rehace con el nuevo.
        panel_channel_id, _ = self.store.get_panel(self.guild.id)
        if panel_channel_id is None:
            await self.refresh(interaction)
            return

        await interaction.response.defer()
        await self._republish(interaction, panel_channel_id)
        await self.refresh(interaction)

    async def _on_panel(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        await self._republish(interaction, self.panel_select.values[0].id)
        await self.refresh(interaction)

    @discord.ui.button(label="Republicar panel", style=discord.ButtonStyle.secondary, row=2)
    async def republish(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        _ = button
        panel_channel_id, _ = self.store.get_panel(self.guild.id)
        if panel_channel_id is None:
            await interaction.response.send_message(
                "❌ Primero elige el canal del panel.", ephemeral=True
            )
            return

        await interaction.response.defer()
        await self._republish(interaction, panel_channel_id)
        await self.refresh(interaction)

    @discord.ui.button(label="Mensaje fijo", style=discord.ButtonStyle.secondary, row=2)
    async def toggle_sticky(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        _ = button
        enabled = not self.store.is_sticky(self.guild.id)
        self.store.set_sticky(self.guild.id, enabled)

        panel_channel_id, _ = self.store.get_panel(self.guild.id)
        if not enabled or panel_channel_id is None:
            await self.refresh(interaction)
            return

        # Al activarlo, el panel solo se baja si hay mensajes por debajo.
        await interaction.response.defer()
        problem = await _repost_sticky_panel(interaction.client, self.guild)
        if problem is not None:
            await interaction.followup.send(f"❌ {problem}", ephemeral=True)
        await self.refresh(interaction)

    @discord.ui.button(label="Cerrar", style=discord.ButtonStyle.secondary, row=2)
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        _ = button
        self.stop()
        await interaction.response.edit_message(view=None)

    async def on_timeout(self) -> None:
        if self.message is not None:
            with suppress(discord.HTTPException):
                await self.message.edit(view=None)


def _is_panel_message(bot: commands.Bot, message: discord.Message) -> bool:
    if bot.user is None or message.author.id != bot.user.id:
        return False
    return any(
        getattr(child, "custom_id", None) == PANEL_BUTTON_ID
        for row in message.components
        for child in getattr(row, "children", [])
    )


async def _panel_is_last(
    channel: discord.TextChannel,
    panel_message_id: int | None,
    ignored_ids: set[int],
) -> bool:
    """Indica si el panel ya es el último mensaje real del canal.

    Los mensajes de `ignored_ids` (avisos temporales del bot que se borran solos)
    no cuentan: cuando desaparezcan el panel vuelve a quedar abajo sin reenviarlo.
    """
    if panel_message_id is None:
        return False

    try:
        async for message in channel.history(limit=PANEL_STICKY_HISTORY_LOOKUP):
            if message.id in ignored_ids:
                continue
            return message.id == panel_message_id
    except discord.HTTPException:
        return False
    return False


async def _repost_sticky_panel(
    bot: commands.Bot,
    guild: discord.Guild,
    ignored_ids: set[int] | None = None,
) -> str | None:
    """Baja el panel al final de su canal si el modo fijo está activo.

    Solo se reenvía cuando hay algún mensaje por debajo; si ya es el último no se toca.
    """
    store = get_bot_context(bot).confessions
    if not store.is_sticky(guild.id):
        return None

    panel_channel_id, panel_message_id = store.get_panel(guild.id)
    channel = guild.get_channel(panel_channel_id) if panel_channel_id is not None else None
    if not isinstance(channel, discord.TextChannel):
        return "Primero publica el panel con `.noah confess config`."

    if await _panel_is_last(channel, panel_message_id, ignored_ids or set()):
        return None
    return await _publish_panel(guild, store, channel)


def register_confess_commands(bot: commands.Bot, noah_group: commands.Group) -> None:
    panel_view_registered = False
    sticky_tasks: dict[int, asyncio.Task] = {}
    sticky_dirty: set[int] = set()
    # Mensajes que no deben empujar el panel: el comando (se borra) y sus avisos temporales.
    transient_ids: set[int] = set()

    def _mark_transient(message_id: int) -> None:
        transient_ids.add(message_id)
        asyncio.get_running_loop().call_later(
            PANEL_STICKY_NOTICE_DELETE_AFTER * 4, transient_ids.discard, message_id
        )

    async def _sticky_notice(ctx: commands.Context, text: str) -> None:
        with suppress(discord.HTTPException):
            notice = await ctx.send(text, delete_after=PANEL_STICKY_NOTICE_DELETE_AFTER)
            _mark_transient(notice.id)

    async def _sticky_repost_worker(guild: discord.Guild) -> None:
        try:
            while True:
                await asyncio.sleep(PANEL_STICKY_REPOST_DELAY)
                sticky_dirty.discard(guild.id)
                await _repost_sticky_panel(bot, guild, transient_ids)
                # Si llegó otro mensaje mientras se reenviaba, hay que repetir.
                if guild.id not in sticky_dirty:
                    return
        finally:
            if sticky_tasks.get(guild.id) is asyncio.current_task():
                sticky_tasks.pop(guild.id, None)

    def _schedule_sticky_repost(guild: discord.Guild) -> None:
        """Agrupa ráfagas de mensajes en un único reenvío del panel."""
        sticky_dirty.add(guild.id)
        pending = sticky_tasks.get(guild.id)
        if pending is not None and not pending.done():
            return
        sticky_tasks[guild.id] = asyncio.create_task(_sticky_repost_worker(guild))

    @bot.listen("on_ready")
    async def _register_confess_panel() -> None:
        nonlocal panel_view_registered
        if panel_view_registered:
            return
        bot.add_view(ConfessPanelView())
        panel_view_registered = True

        # Si alguien escribió mientras el bot estaba apagado, vuelve a dejarlo el último.
        store = get_bot_context(bot).confessions
        for guild_id in store.get_sticky_guild_ids():
            guild = bot.get_guild(guild_id)
            panel_channel_id, panel_message_id = store.get_panel(guild_id)
            channel = (
                guild.get_channel(panel_channel_id)
                if guild is not None and panel_channel_id is not None
                else None
            )
            if not isinstance(channel, discord.TextChannel):
                continue
            if channel.last_message_id == panel_message_id:
                continue
            await _repost_sticky_panel(bot, guild)

    @bot.listen("on_message")
    async def _keep_confess_panel_last(message: discord.Message) -> None:
        if message.guild is None or _is_panel_message(bot, message):
            return

        if message.id in transient_ids:
            return

        store = get_bot_context(bot).confessions
        if not store.is_sticky(message.guild.id):
            return

        panel_channel_id, _ = store.get_panel(message.guild.id)
        if panel_channel_id is None or message.channel.id != panel_channel_id:
            return

        if is_guard_active(bot, message.guild.id):
            return

        _schedule_sticky_repost(message.guild)

    @bot.listen("on_raw_message_delete")
    async def _forget_confess_panel(payload: discord.RawMessageDeleteEvent) -> None:
        if payload.guild_id is None:
            return

        store = get_bot_context(bot).confessions
        _, panel_message_id = store.get_panel(payload.guild_id)
        if panel_message_id == payload.message_id:
            store.set_panel(payload.guild_id, None, None)

    @noah_group.group(invoke_without_command=True)
    async def confess(ctx: commands.Context) -> None:
        # Lo primero es borrar el comando para que quede el menor rastro posible.
        with suppress(discord.HTTPException):
            await ctx.message.delete()

        if ctx.guild is None:
            await ctx.send("❌ Este comando solo funciona dentro de un servidor.")
            return

        store = get_bot_context(ctx.bot).confessions
        if _confessions_channel(ctx.guild, store) is None:
            await ctx.send(
                "❌ Las confesiones no están configuradas. Un admin debe usar "
                "`.noah confess config`.",
                delete_after=10,
            )
            return

        view = ConfessPromptView(ctx.author.id)
        view.message = await ctx.send(
            "📝 Pulsa el botón para escribir tu confesión en privado.",
            view=view,
            delete_after=PROMPT_TIMEOUT,
        )

    @confess.command()
    async def help(ctx: commands.Context) -> None:
        await ctx.send(embed=_build_help_embed())

    @confess.command()
    async def config(ctx: commands.Context) -> None:
        if ctx.guild is None:
            await ctx.send("❌ Este comando solo funciona dentro de un servidor.")
            return

        if not _is_confess_admin(ctx.author):
            await ctx.send("❌ Solo los administradores pueden usar este comando.")
            return

        store = get_bot_context(ctx.bot).confessions
        view = ConfessConfigView(ctx.guild, store, ctx.author.id)
        view.message = await ctx.send(
            embed=_build_config_embed(ctx.guild, store), view=view
        )

    @confess.command()
    async def sticky(ctx: commands.Context, mode: str | None = None) -> None:
        # El comando se borra para no dejar nada debajo del panel.
        _mark_transient(ctx.message.id)
        with suppress(discord.HTTPException):
            await ctx.message.delete()

        if ctx.guild is None:
            await _sticky_notice(ctx, "❌ Este comando solo funciona dentro de un servidor.")
            return

        if not _is_confess_admin(ctx.author):
            await _sticky_notice(ctx, "❌ Solo los administradores pueden usar este comando.")
            return

        store = get_bot_context(ctx.bot).confessions
        normalized = (mode or "on").strip().casefold()

        if normalized in {"on", "enable", "activar"}:
            panel_channel_id, _ = store.get_panel(ctx.guild.id)
            if panel_channel_id is None:
                await _sticky_notice(
                    ctx,
                    "❌ Primero publica el panel eligiendo su canal en `.noah confess config`.",
                )
                return

            store.set_sticky(ctx.guild.id, True)
            problem = await _repost_sticky_panel(ctx.bot, ctx.guild, transient_ids)
            if problem is not None:
                await _sticky_notice(ctx, f"⚠️ Mensaje fijo activado, pero: {problem}")
                return

            await _sticky_notice(
                ctx,
                f"✅ Panel de confesiones fijo en <#{panel_channel_id}>: "
                "se mantendrá siempre abajo del todo.",
            )
            return

        if normalized in {"off", "disable", "desactivar"}:
            store.set_sticky(ctx.guild.id, False)
            await _sticky_notice(
                ctx, "✅ Panel de confesiones fijo desactivado. El panel se queda donde está."
            )
            return

        await _sticky_notice(
            ctx, "❌ Usa `.noah confess sticky on` o `.noah confess sticky off`."
        )
