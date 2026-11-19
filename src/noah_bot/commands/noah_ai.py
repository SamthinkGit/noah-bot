"""Noah AI: chats privados con Noah que puede leer el servidor (solo lectura).

Flujo:
- Un panel fijo con «Abrir chat privado» y «Configurar mi Noah».
- Cada chat es un hilo privado (no invitable) en el canal de chats: solo lo ven su dueño
  y el staff con permiso de gestionar hilos.
- Mientras Noah trabaja, un contenedor enseña las herramientas («Leyendo #canal…») y el
  razonamiento desplegable; la respuesta final llega como mensaje normal.
- Primer intento de saltarse las reglas: aviso. Segundo: se cierra el chat y se avisa al
  staff en el canal de avisos.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import OrderedDict
from contextlib import suppress
from datetime import timedelta
from typing import Any

import discord
from discord.ext import commands

from noah_bot.modules import noah_ai_emojis
from noah_bot.modules.ai_settings import (
    DEFAULT_MODEL,
    LENGTHS,
    PERSONA_LIMIT,
    TOPICS,
    AiSettingsStore,
    local_now,
    mask_key,
)
from noah_bot.modules.bot_context import get_bot_context, is_guard_active
from noah_bot.modules.noah_ai_agent import AgentError, NoahAgent, build_instructions, check_api_key
from noah_bot.modules.noah_ai_tools import (
    TOOLS_BY_NAME,
    Step,
    ToolContext,
    execute_tool,
    tools_for,
)

_log = logging.getLogger(__name__)

AI_COLOR = discord.Color.from_rgb(155, 89, 208)
WARN_COLOR = discord.Color.from_rgb(240, 178, 50)
DANGER_COLOR = discord.Color.from_rgb(218, 55, 60)
OK_COLOR = discord.Color.from_rgb(35, 165, 90)

PANEL_OPEN_ID = "noah:ai:open"
PANEL_PREFS_ID = "noah:ai:prefs"
PANEL_TEXT = (
    "## Noah AI\n"
    "Pulsa aquí abajo para abrir un chat **PRIVADO** con Noah. Esta IA tiene acceso a los "
    "mismos canales y puedes usarlo para preguntar lo que quieras <3"
)
OPEN_COOLDOWN_SECONDS = 15
HISTORY_LIMIT = 24
QUESTION_LIMIT = 1500
ANSWER_CHUNK = 1900
EDIT_INTERVAL = 1.2
REASONING_LIMIT = 2500
CARD_CACHE = 300
CONFIG_TIMEOUT = 600
READABLE_CHANNEL_TYPES = [
    discord.ChannelType.text,
    discord.ChannelType.news,
    discord.ChannelType.voice,
    discord.ChannelType.stage_voice,
    discord.ChannelType.forum,
]

NONE = discord.AllowedMentions.none()
USER_MENTION_RE = re.compile(r"<@!?(\d+)>")
ROLE_MENTION_RE = re.compile(r"<@&(\d+)>")

_thread_locks: dict[int, asyncio.Lock] = {}
_cards: "OrderedDict[int, ProgressCard]" = OrderedDict()
_agent: NoahAgent | None = None


def _settings(bot: commands.Bot | discord.Client) -> AiSettingsStore:
    return get_bot_context(bot).ai_settings  # type: ignore[arg-type]


def _agent_for(bot: commands.Bot | discord.Client) -> NoahAgent:
    global _agent
    if _agent is None:
        _agent = NoahAgent(_settings(bot))
    return _agent


def _is_admin(user: discord.abc.User) -> bool:
    return isinstance(user, discord.Member) and user.guild_permissions.administrator


def _quota_text(settings: AiSettingsStore, guild_id: int, member: discord.Member) -> str:
    if _is_admin(member):
        return "Sin límite de preguntas (admin)"
    limit = settings.daily_limit(guild_id)
    remaining = max(0, limit - settings.used_today(guild_id, member.id))
    return f"Te quedan {remaining}/{limit} preguntas hoy"


def _channel_text(guild: discord.Guild, channel_id: int | None) -> str:
    if channel_id is None:
        return "sin configurar"
    channel = guild.get_channel(channel_id)
    return channel.mention if channel is not None else "canal borrado"


# -- Texto de las respuestas -------------------------------------------------------------


def _sanitize_answer(guild: discord.Guild, text: str) -> str:
    """Sin menciones: en un hilo privado una mención podría meter a esa persona."""

    def _user(match: re.Match[str]) -> str:
        member = guild.get_member(int(match.group(1)))
        name = member.display_name if member else "alguien"
        return f"**{discord.utils.escape_markdown(name)}**"

    def _role(match: re.Match[str]) -> str:
        role = guild.get_role(int(match.group(1)))
        return role.name if role else "un rol"

    text = USER_MENTION_RE.sub(_user, text)
    text = ROLE_MENTION_RE.sub(_role, text)
    return text.replace("@everyone", "@​everyone").replace("@here", "@​here")


def _split_message(text: str, limit: int = ANSWER_CHUNK) -> list[str]:
    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) > limit:
            chunks.append(current)
            current = ""
        current += line
    if current.strip():
        chunks.append(current)
    return [chunk.strip("\n") for chunk in chunks if chunk.strip()] or ["…"]


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


# -- Tarjeta de progreso ---------------------------------------------------------------------


class ThinkToggle(discord.ui.DynamicItem[discord.ui.Button], template=r"noah:ai:think:(?P<open>[01])"):
    """Botón «▸ Razonamiento». Funciona tras reiniciar (avisa si ya no hay datos)."""

    def __init__(self, expanded: bool) -> None:
        super().__init__(
            discord.ui.Button(
                label="Razonamiento",
                emoji="▾" if expanded else "▸",
                style=discord.ButtonStyle.secondary,
                custom_id=f"noah:ai:think:{int(expanded)}",
            )
        )

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str]
    ) -> "ThinkToggle":
        _ = interaction, item
        return cls(match["open"] == "1")

    async def callback(self, interaction: discord.Interaction) -> None:
        card = _cards.get(interaction.message.id) if interaction.message else None
        if card is None:
            await interaction.response.send_message(
                "Este razonamiento ya no está disponible (Noah se ha reiniciado desde entonces).",
                ephemeral=True,
            )
            return
        card.expanded = not card.expanded
        card.mark_edited()
        await interaction.response.edit_message(view=card.render(), allowed_mentions=NONE)


class ProgressCard:
    def __init__(self, channel: discord.abc.Messageable, show_reasoning: bool) -> None:
        self.channel = channel
        self.show_reasoning = show_reasoning
        self.steps: list[Step] = []
        self.thinking = True
        self.reasoning: list[str] = []
        self.expanded = False
        self.footer: str | None = None
        self.error: str | None = None
        self.message: discord.Message | None = None
        self.finished = False
        self._dirty = False
        self._last_edit = 0.0
        self._flush_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    def mark_edited(self) -> None:
        self._last_edit = time.monotonic()

    def render(self) -> discord.ui.LayoutView:
        view = discord.ui.LayoutView(timeout=None)
        container = discord.ui.Container(accent_color=DANGER_COLOR if self.error else AI_COLOR)

        lines = [f"{noah_ai_emojis.dot(step.status)} {step.label}" for step in self.steps]
        if self.thinking and not self.finished:
            lines.append(f"{noah_ai_emojis.dot('run')} Pensando…")
        if self.error:
            lines.append(f"{noah_ai_emojis.dot('error')} {self.error}")
        if not lines:
            lines.append(f"{noah_ai_emojis.dot('ok')} Respondido sin consultar el servidor")
        container.add_item(discord.ui.TextDisplay(_clip("\n".join(lines), 1200)))

        if self.reasoning and self.show_reasoning:
            row: discord.ui.ActionRow[Any] = discord.ui.ActionRow()
            row.add_item(ThinkToggle(self.expanded))
            container.add_item(row)
            if self.expanded:
                text = _clip("\n\n".join(self.reasoning), REASONING_LIMIT)
                quoted = "\n".join(f"> {line}" if line.strip() else ">" for line in text.splitlines())
                container.add_item(discord.ui.TextDisplay(quoted))

        if self.footer:
            container.add_item(discord.ui.Separator())
            container.add_item(discord.ui.TextDisplay(f"-# {self.footer}"))
        view.add_item(container)
        return view

    async def start(self) -> None:
        self.message = await self.channel.send(view=self.render(), allowed_mentions=NONE)
        self.mark_edited()
        _cards[self.message.id] = self
        while len(_cards) > CARD_CACHE:
            _cards.popitem(last=False)

    async def _flush(self) -> None:
        async with self._lock:
            if not self._dirty or self.message is None:
                return
            self._dirty = False
            self.mark_edited()
            with suppress(discord.HTTPException):
                await self.message.edit(view=self.render(), allowed_mentions=NONE)

    async def _delayed_flush(self, delay: float) -> None:
        await asyncio.sleep(delay)
        await self._flush()

    async def refresh(self, force: bool = False) -> None:
        self._dirty = True
        if force:
            if self._flush_task is not None and not self._flush_task.done():
                self._flush_task.cancel()
            await self._flush()
            return
        if self._flush_task is not None and not self._flush_task.done():
            return
        delay = max(0.0, EDIT_INTERVAL - (time.monotonic() - self._last_edit))
        self._flush_task = asyncio.create_task(self._delayed_flush(delay))

    async def _on_step(self, step: Step) -> None:
        await self.refresh()

    async def add_step(self, label: str) -> Step:
        step = Step(self._on_step, label)
        self.steps.append(step)
        await self.refresh()
        return step

    async def set_thinking(self, thinking: bool) -> None:
        if self.thinking != thinking:
            self.thinking = thinking
            await self.refresh()

    async def add_reasoning(self, text: str) -> None:
        self.reasoning.append(text)
        await self.refresh()

    async def finish(self, footer: str | None = None, error: str | None = None) -> None:
        self.finished = True
        self.thinking = False
        self.footer = footer
        self.error = error
        for step in self.steps:
            if step.status == "run":
                step.status = "error" if error else "ok"
        await self.refresh(force=True)


# -- Panel ----------------------------------------------------------------------------------


class AiPanelView(discord.ui.LayoutView):
    """Panel fijo. Persistente: los botones siguen funcionando tras reiniciar."""

    def __init__(self, avatar_url: str | None = None) -> None:
        super().__init__(timeout=None)
        container = discord.ui.Container(accent_color=AI_COLOR)
        if avatar_url:
            container.add_item(discord.ui.Section(PANEL_TEXT, accessory=discord.ui.Thumbnail(avatar_url)))
        else:
            container.add_item(discord.ui.TextDisplay(PANEL_TEXT))
        container.add_item(discord.ui.Separator())

        row: discord.ui.ActionRow[Any] = discord.ui.ActionRow()
        open_button = discord.ui.Button(
            label="Abrir chat privado", emoji="💬", style=discord.ButtonStyle.primary, custom_id=PANEL_OPEN_ID
        )
        open_button.callback = _open_chat
        prefs_button = discord.ui.Button(
            label="Configurar mi Noah", emoji="⚙️", style=discord.ButtonStyle.secondary, custom_id=PANEL_PREFS_ID
        )
        prefs_button.callback = _open_prefs
        row.add_item(open_button)
        row.add_item(prefs_button)
        container.add_item(row)
        self.add_item(container)


def _threads_problem(channel: discord.TextChannel) -> str | None:
    permissions = channel.permissions_for(channel.guild.me)
    missing = [
        name
        for name, ok in (
            ("Ver canal", permissions.view_channel),
            ("Crear hilos privados", permissions.create_private_threads),
            ("Enviar mensajes en hilos", permissions.send_messages_in_threads),
            ("Gestionar hilos", permissions.manage_threads),
        )
        if not ok
    ]
    return f"Me faltan permisos en {channel.mention}: {', '.join(missing)}." if missing else None


def _welcome_view(settings: AiSettingsStore, guild_id: int, member: discord.Member) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    container = discord.ui.Container(accent_color=WARN_COLOR)
    container.add_item(
        discord.ui.TextDisplay(
            "### 🔒 Chat anónimo y privado con Noah\n"
            "Este chat es solo entre tú y Noah. Puede ser revisado por el staff si hay motivos "
            "para ello, como romper las normas.\n"
            "**No compartas información confidencial.**\n"
            f"-# {_quota_text(settings, guild_id, member)}"
        )
    )
    view.add_item(container)
    return view


async def _open_chat(interaction: discord.Interaction) -> None:
    guild = interaction.guild
    member = interaction.user
    if guild is None or not isinstance(member, discord.Member):
        await interaction.response.send_message("❌ Esto solo funciona dentro del servidor.", ephemeral=True)
        return

    settings = _settings(interaction.client)
    admin = _is_admin(member)
    problem: str | None = None
    if not settings.is_enabled(guild.id) and not admin:
        problem = "Noah AI está desactivado ahora mismo."
    elif is_guard_active(interaction.client, guild.id) and not admin:  # type: ignore[arg-type]
        problem = "⛔ Noah AI está pausado por el protocolo de contingencia."
    elif not settings.api_key:
        problem = "Noah AI todavía no está configurado. Avisa a un admin."
    channel_id = settings.get_channel(guild.id, "threads")
    channel = guild.get_channel(channel_id) if channel_id is not None else None
    if problem is None and not isinstance(channel, discord.TextChannel):
        problem = "Todavía no hay canal de chats configurado. Avisa a un admin."
    if problem is None and isinstance(channel, discord.TextChannel):
        problem = _threads_problem(channel)
    last = settings.last_thread_created(guild.id, member.id)
    if problem is None and last is not None and local_now() - last < timedelta(seconds=OPEN_COOLDOWN_SECONDS):
        problem = "Acabas de abrir un chat, espera unos segundos 🖤"
    if problem is not None:
        await interaction.response.send_message(problem, ephemeral=True)
        return

    assert isinstance(channel, discord.TextChannel)
    await interaction.response.defer(ephemeral=True, thinking=True)
    count = sum(1 for _, data in settings.list_threads(guild.id) if data.get("owner_id") == member.id)
    name = f"Noah · {member.display_name}" + (f" #{count + 1}" if count else "")
    try:
        thread = await channel.create_thread(
            name=name[:100],
            type=discord.ChannelType.private_thread,
            invitable=False,
            auto_archive_duration=1440,
            reason=f"Noah AI: chat privado de {member}",
        )
        await thread.add_user(member)
    except discord.HTTPException:
        await interaction.followup.send("❌ No he podido abrir el chat. Avisa a un admin.", ephemeral=True)
        return

    settings.register_thread(guild.id, thread.id, member.id)
    with suppress(discord.HTTPException):
        await thread.send(view=_welcome_view(settings, guild.id, member), allowed_mentions=NONE)
    link = discord.ui.View()
    link.add_item(discord.ui.Button(label="Ir al chat", emoji="💬", url=thread.jump_url))
    await interaction.followup.send(f"Tu chat privado con Noah está listo: {thread.mention} 🖤", view=link, ephemeral=True)


class PrefsModal(discord.ui.Modal, title="Configura tu Noah"):
    def __init__(self, prefs: dict[str, Any]) -> None:
        super().__init__(timeout=600)
        descriptions = {"short": "1-3 frases", "normal": "Un párrafo", "long": "Con detalle cuando ayude"}
        self.length: discord.ui.Select[Any] = discord.ui.Select(
            options=[
                discord.SelectOption(label=label, value=key, description=descriptions[key], default=key == prefs["length"])
                for key, label in LENGTHS.items()
            ],
            min_values=1,
            max_values=1,
        )
        self.persona = discord.ui.TextInput(
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=PERSONA_LIMIT,
            default=prefs["persona"] or None,
            placeholder="Ej: Responde como si fueras un pirata, sé breve y llámame capitana.",
        )
        self.reasoning: discord.ui.Select[Any] = discord.ui.Select(
            options=[
                discord.SelectOption(label="Sí", value="1", default=prefs["show_reasoning"]),
                discord.SelectOption(label="No", value="0", default=not prefs["show_reasoning"]),
            ],
            min_values=1,
            max_values=1,
        )
        self.add_item(discord.ui.Label(text="Longitud de respuesta", component=self.length))
        self.add_item(
            discord.ui.Label(
                text="¿Cómo quieres que te responda?",
                description="Se guarda en tu cuenta. No puede saltarse las normas del server.",
                component=self.persona,
            )
        )
        self.add_item(discord.ui.Label(text="Mostrar razonamiento", component=self.reasoning))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        _settings(interaction.client).set_user_prefs(
            interaction.user.id,
            length=self.length.values[0] if self.length.values else "normal",
            persona=self.persona.value or "",
            show_reasoning=(self.reasoning.values[0] if self.reasoning.values else "1") == "1",
        )
        await interaction.response.send_message("✅ Guardado. Noah te responderá así a partir de ahora 🖤", ephemeral=True)


async def _open_prefs(interaction: discord.Interaction) -> None:
    prefs = _settings(interaction.client).user_prefs(interaction.user.id)
    await interaction.response.send_modal(PrefsModal(prefs))


async def publish_panel(bot: commands.Bot, guild: discord.Guild) -> str | None:
    """Publica el panel en su canal y borra el anterior. Devuelve el error, si hay."""
    settings = _settings(bot)
    channel_id = settings.get_channel(guild.id, "panel")
    channel = guild.get_channel(channel_id) if channel_id is not None else None
    if not isinstance(channel, discord.TextChannel):
        return "Primero elige el canal del panel en **Canales**."
    permissions = channel.permissions_for(guild.me)
    if not (permissions.view_channel and permissions.send_messages):
        return f"No puedo escribir en {channel.mention}."
    avatar = bot.user.display_avatar.url if bot.user else None
    try:
        message = await channel.send(view=AiPanelView(avatar), allowed_mentions=NONE)
    except discord.HTTPException:
        return f"No he podido publicar el panel en {channel.mention}."

    old = settings.get_panel_message(guild.id)
    settings.set_panel_message(guild.id, channel.id, message.id)
    if old is not None:
        old_channel = guild.get_channel(old[0])
        if isinstance(old_channel, discord.TextChannel):
            with suppress(discord.HTTPException):
                await old_channel.get_partial_message(old[1]).delete()
    return None


# -- Preguntas en los chats -------------------------------------------------------------------


async def _history(
    thread: discord.Thread, current: discord.Message, owner: discord.Member, bot_user: discord.ClientUser | None
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    with suppress(discord.HTTPException):
        async for message in thread.history(limit=HISTORY_LIMIT, before=current):
            if message.author.id == owner.id and message.content:
                items.append({"role": "user", "content": _clip(message.clean_content, QUESTION_LIMIT)})
            elif (
                bot_user is not None
                and message.author.id == bot_user.id
                and message.content
                and not message.components
                and not message.content.startswith("-# ")
            ):
                items.append({"role": "assistant", "content": _clip(message.content, 2000)})
    items.reverse()
    items.append({"role": "user", "content": _clip(current.clean_content, QUESTION_LIMIT)})
    return items


def _closed_view() -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    container = discord.ui.Container(accent_color=DANGER_COLOR)
    container.add_item(
        discord.ui.TextDisplay(
            "### 🔒 Chat cerrado\n"
            "Has vuelto a intentar acceder a información restringida o cambiar cómo funciona "
            "Noah, así que este chat se ha cerrado.\n"
            "-# El staff puede haber recibido una notificación."
        )
    )
    view.add_item(container)
    return view


async def _alert_staff(
    bot: commands.Bot, guild: discord.Guild, thread: discord.Thread, member: discord.Member, last_message: str
) -> None:
    settings = _settings(bot)
    channel_id = settings.get_channel(guild.id, "staff")
    channel = guild.get_channel(channel_id) if channel_id is not None else None
    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
        return
    data = settings.get_thread(guild.id, thread.id) or {}
    reasons = data.get("strike_reasons") or []
    reasons_text = "\n".join(f"{index}. {discord.utils.escape_markdown(str(reason))}" for index, reason in enumerate(reasons, start=1))
    quoted = "\n".join(f"> {line}" for line in _clip(discord.utils.escape_markdown(last_message), 600).splitlines())

    view = discord.ui.LayoutView(timeout=None)
    container = discord.ui.Container(accent_color=DANGER_COLOR)
    container.add_item(
        discord.ui.TextDisplay(
            "### ⛔ Noah AI · chat cerrado\n"
            f"**Usuario:** {member.mention} ({discord.utils.escape_markdown(member.display_name)})\n"
            f"**Chat:** {thread.mention}\n"
            f"**Avisos:**\n{reasons_text or '-'}\n"
            f"**Último mensaje:**\n{quoted or '> -'}"
        )
    )
    row: discord.ui.ActionRow[Any] = discord.ui.ActionRow()
    row.add_item(discord.ui.Button(label="Abrir chat", emoji="💬", url=thread.jump_url))
    container.add_item(row)
    view.add_item(container)
    with suppress(discord.HTTPException):
        await channel.send(view=view, allowed_mentions=NONE)


async def _close_thread(bot: commands.Bot, thread: discord.Thread, member: discord.Member, last_message: str) -> None:
    settings = _settings(bot)
    settings.close_thread(thread.guild.id, thread.id)
    with suppress(discord.HTTPException):
        await thread.send(view=_closed_view(), allowed_mentions=NONE)
    await _alert_staff(bot, thread.guild, thread, member, last_message)
    with suppress(discord.HTTPException):
        await thread.edit(locked=True, archived=True, reason="Noah AI: segundo intento de saltarse las reglas")


async def _handle_question(bot: commands.Bot, message: discord.Message) -> None:
    thread = message.channel
    guild = message.guild
    member = message.author
    assert isinstance(thread, discord.Thread) and guild is not None and isinstance(member, discord.Member)
    settings = _settings(bot)
    admin = _is_admin(member)

    async def _notice(text: str) -> None:
        with suppress(discord.HTTPException):
            await message.reply(text, mention_author=False, allowed_mentions=NONE)

    if not admin and not settings.is_enabled(guild.id):
        await _notice("Noah AI está desactivado ahora mismo por el staff 🖤")
        return
    if not admin and is_guard_active(bot, guild.id):
        await _notice("⛔ Noah AI está pausado por el protocolo de contingencia.")
        return
    limit = settings.daily_limit(guild.id)
    if not admin and settings.used_today(guild.id, member.id) >= limit:
        await _notice(f"Has llegado al límite de **{limit}** preguntas por hoy 🖤 Se reinicia a las 00:00.")
        return
    if not message.content.strip():
        await _notice("-# Solo puedo leer texto 🖤")
        return

    prefs = settings.user_prefs(member.id)
    topics = settings.enabled_topics(guild.id)
    card = ProgressCard(thread, show_reasoning=prefs["show_reasoning"])
    try:
        await card.start()
    except discord.HTTPException:
        return

    close_after = False

    async def _report(reason: str) -> str:
        nonlocal close_after
        strikes = settings.add_strike(guild.id, thread.id, reason)
        if strikes >= 2:
            close_after = True
            return (
                "SEGUNDO aviso: el chat se cerrará automáticamente al terminar tu respuesta. No hagas "
                "lo que pedía. Despídete en una o dos frases diciendo que cierras el chat y que el "
                "staff puede haber recibido una notificación."
            )
        return (
            "PRIMER aviso registrado. No hagas lo que pedía. Dile claramente que eso no lo puedes "
            "hacer y que si lo vuelve a intentar se cerrará el chat."
        )

    ctx = ToolContext(
        bot=bot,
        guild=guild,
        asker=member,
        is_admin=admin,
        settings=settings,
        thread_id=thread.id,
        topics=topics,
        report_infraction=_report,
    )

    async def _run_tool(name: str, arguments: str) -> str:
        spec = TOOLS_BY_NAME.get(name)
        step = await card.add_step(spec.label if spec else "Trabajando…")
        outcome = await execute_tool(ctx, name, arguments, step)
        await step.finish(outcome.done, outcome.status)
        return outcome.text

    started = time.monotonic()
    try:
        history = await _history(thread, message, member, bot.user)
        instructions = build_instructions(
            guild_name=guild.name,
            user_name=member.display_name,
            is_admin=admin,
            prefs=prefs,
            enabled_topics=topics,
        )
        result = await _agent_for(bot).run(
            instructions=instructions,
            history=history,
            tools=tools_for(admin, topics),
            run_tool=_run_tool,
            on_reasoning=card.add_reasoning,
            on_thinking=card.set_thinking,
        )
    except AgentError as error:
        await card.finish(error=str(error))
        return
    except Exception:  # noqa: BLE001 - que un fallo no deje la tarjeta colgada
        _log.exception("Noah AI ha fallado respondiendo en %s", thread.id)
        await card.finish(error="Algo ha fallado por dentro. Prueba otra vez en un rato.")
        return

    used = settings.add_usage(guild.id, member.id)
    settings.add_thread_question(guild.id, thread.id)
    seconds = max(1, round(time.monotonic() - started))
    quota = "sin límite (admin)" if admin else f"{max(0, limit - used)}/{limit} preguntas restantes hoy"
    await card.finish(footer=f"{quota} · {settings.model} · {seconds} s")

    for chunk in _split_message(_sanitize_answer(guild, result.answer)):
        with suppress(discord.HTTPException):
            await thread.send(chunk, allowed_mentions=NONE)

    if close_after:
        await _close_thread(bot, thread, member, message.content)


# -- Configuración (admins) -----------------------------------------------------------------


class ModelModal(discord.ui.Modal, title="Modelo de OpenAI"):
    def __init__(self, panel: "AiConfigView") -> None:
        super().__init__(timeout=300)
        self.panel = panel
        self.model = discord.ui.TextInput(
            default=panel.settings.model, placeholder=DEFAULT_MODEL, max_length=80, required=False
        )
        self.add_item(
            discord.ui.Label(
                text="Modelo",
                description=f"Se usa en Noah AI y en .noah ask/summary/behonest. Vacío = {DEFAULT_MODEL}",
                component=self.model,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        settings = self.panel.settings
        settings.set_model(self.model.value or DEFAULT_MODEL)
        await interaction.response.defer()
        if settings.api_key:
            settings.set_key_status(await check_api_key(settings.api_key, settings.model))
        await self.panel.refresh(interaction)


class ApiKeyModal(discord.ui.Modal, title="API key de OpenAI"):
    def __init__(self, panel: "AiConfigView") -> None:
        super().__init__(timeout=300)
        self.panel = panel
        self.key = discord.ui.TextInput(placeholder="sk-…", max_length=300, required=False)
        self.add_item(
            discord.ui.Label(
                text="Nueva API key",
                description="No se muestra nunca. Vacío = usar OPENAI_API_KEY del .env",
                component=self.key,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        settings = self.panel.settings
        settings.set_api_key(self.key.value.strip() or None)
        await interaction.response.defer()
        status = await check_api_key(settings.api_key, settings.model) if settings.api_key else None
        settings.set_key_status(status)
        await self.panel.refresh(interaction)
        notes = {
            "ok": "✅ API key guardada y comprobada.",
            "invalid": "❌ OpenAI dice que esa API key no es válida.",
            "no_model": f"⚠️ La key funciona, pero no tiene acceso al modelo `{settings.model}`.",
            "error": "⚠️ Guardada, pero no he podido comprobarla ahora mismo.",
            None: "API key borrada. Se usará la del .env si existe.",
        }
        await interaction.followup.send(notes.get(status, "Guardada."), ephemeral=True)


class ChannelsModal(discord.ui.Modal, title="Canales de Noah AI"):
    def __init__(self, panel: "AiConfigView") -> None:
        super().__init__(timeout=300)
        self.panel = panel
        guild = panel.guild
        self.selects: dict[str, discord.ui.ChannelSelect[Any]] = {}
        labels = {
            "panel": ("Canal del panel", "Donde va el botón para abrir chats"),
            "threads": ("Canal de chats privados", "Aquí se crean los hilos privados"),
            "staff": ("Canal de avisos al staff", "Aviso cuando se cierra un chat por saltarse las reglas"),
        }
        for key, (text, description) in labels.items():
            current_id = panel.settings.get_channel(guild.id, key)
            current = guild.get_channel(current_id) if current_id is not None else None
            select: discord.ui.ChannelSelect[Any] = discord.ui.ChannelSelect(
                channel_types=[discord.ChannelType.text],
                min_values=0,
                max_values=1,
                required=False,
                default_values=[current] if current is not None else [],
            )
            self.selects[key] = select
            self.add_item(discord.ui.Label(text=text, description=description, component=select))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        for key, select in self.selects.items():
            self.panel.settings.set_channel(self.panel.guild.id, key, select.values[0].id if select.values else None)
        await self.panel.refresh(interaction)


class LimitModal(discord.ui.Modal, title="Límite diario"):
    def __init__(self, panel: "AiConfigView") -> None:
        super().__init__(timeout=300)
        self.panel = panel
        self.limit = discord.ui.TextInput(
            default=str(panel.settings.daily_limit(panel.guild.id)), max_length=4, required=True
        )
        self.add_item(
            discord.ui.Label(
                text="Preguntas al día por usuario",
                description="Los admins no tienen límite. 0 = nadie salvo admins.",
                component=self.limit,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            value = int(self.limit.value.strip())
        except ValueError:
            await interaction.response.send_message("❌ Tiene que ser un número.", ephemeral=True)
            return
        self.panel.settings.set_daily_limit(self.panel.guild.id, max(0, min(value, 1000)))
        await self.panel.refresh(interaction)


def _threads_list_text(guild: discord.Guild, settings: AiSettingsStore, limit: int = 15) -> str:
    lines = []
    for thread_id, data in settings.list_threads(guild.id)[:limit]:
        owner = guild.get_member(int(data.get("owner_id") or 0))
        owner_name = discord.utils.escape_markdown(owner.display_name) if owner else "ex-miembro"
        state = " · ⛔ cerrado" if data.get("closed") else ""
        strikes = int(data.get("strikes", 0))
        warn = f" · ⚠️ {strikes} aviso{'s' if strikes != 1 else ''}" if strikes and not data.get("closed") else ""
        lines.append(f"<#{thread_id}> · {owner_name} · {data.get('questions', 0)} preguntas{warn}{state}")
    return "\n".join(lines) if lines else "Todavía no hay chats."


class AiConfigView(discord.ui.LayoutView):
    def __init__(self, bot: commands.Bot, guild: discord.Guild, author_id: int) -> None:
        super().__init__(timeout=CONFIG_TIMEOUT)
        self.bot = bot
        self.guild = guild
        self.author_id = author_id
        self.settings = _settings(bot)
        self._build()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Solo quien ha abierto la configuración puede usarla.", ephemeral=True)
            return False
        return True

    def _problems(self) -> list[str]:
        problems: list[str] = []
        guild_id = self.guild.id
        if not self.settings.api_key:
            problems.append("No hay API key: Noah AI y `.noah ask` no funcionarán.")
        elif self.settings.key_status() == "invalid":
            problems.append("La API key no es válida.")
        elif self.settings.key_status() == "no_model":
            problems.append(f"La API key no tiene acceso al modelo `{self.settings.model}`.")
        threads_id = self.settings.get_channel(guild_id, "threads")
        threads = self.guild.get_channel(threads_id) if threads_id is not None else None
        if isinstance(threads, discord.TextChannel):
            problem = _threads_problem(threads)
            if problem:
                problems.append(problem)
        else:
            problems.append("Falta el canal de chats privados.")
        staff_id = self.settings.get_channel(guild_id, "staff")
        staff = self.guild.get_channel(staff_id) if staff_id is not None else None
        if isinstance(staff, discord.TextChannel) and not staff.permissions_for(self.guild.me).send_messages:
            problems.append(f"No puedo escribir en {staff.mention}.")
        if not self.settings.readable_channels(guild_id):
            problems.append("No hay canales legibles: los usuarios no podrán preguntar por ningún canal.")
        if not self.guild.me.guild_permissions.view_audit_log:
            problems.append("Sin permiso de ver el registro de auditoría (los admins no podrán consultarlo).")
        return problems

    def _section(self, text: str, label: str, callback: Any) -> discord.ui.Section[Any]:
        button = discord.ui.Button(label=label, style=discord.ButtonStyle.secondary)
        button.callback = callback
        return discord.ui.Section(text, accessory=button)

    def _build(self) -> None:
        self.clear_items()
        settings = self.settings
        guild = self.guild
        enabled = settings.is_enabled(guild.id)
        open_threads = sum(1 for _, data in settings.list_threads(guild.id) if not data.get("closed"))
        container = discord.ui.Container(accent_color=OK_COLOR if enabled else DANGER_COLOR)
        container.add_item(
            discord.ui.TextDisplay(
                "## ⚙️ Noah AI\n"
                f"-# {'🟢 Activo' if enabled else '🔴 Desactivado'} · {settings.total_today(guild.id)} preguntas hoy · "
                f"{open_threads} chats abiertos"
            )
        )
        container.add_item(discord.ui.Separator())

        key_status = {
            "ok": "válida",
            "invalid": "no válida",
            "no_model": "sin acceso al modelo",
            "error": "sin comprobar",
        }.get(settings.key_status() or "", "sin comprobar")
        source = " (desde .env)" if settings.api_key_source == "env" else ""
        container.add_item(self._section(f"**Modelo**\n-# {settings.model}", "Cambiar", self._edit_model))
        container.add_item(
            self._section(
                f"**API key de OpenAI**\n-# {mask_key(settings.api_key)}{source}"
                + (f" · {key_status}" if settings.api_key else ""),
                "Cambiar",
                self._edit_key,
            )
        )
        container.add_item(
            self._section(
                "**Canales**\n"
                f"-# Panel {_channel_text(guild, settings.get_channel(guild.id, 'panel'))} · "
                f"Chats {_channel_text(guild, settings.get_channel(guild.id, 'threads'))} · "
                f"Avisos al staff {_channel_text(guild, settings.get_channel(guild.id, 'staff'))}",
                "Editar",
                self._edit_channels,
            )
        )
        container.add_item(
            self._section(
                f"**Límite diario**\n-# {settings.daily_limit(guild.id)} preguntas por usuario · admins sin límite",
                "Editar",
                self._edit_limit,
            )
        )
        container.add_item(discord.ui.Separator())

        readable = [guild.get_channel(channel_id) for channel_id in sorted(settings.readable_channels(guild.id))]
        readable = [channel for channel in readable if channel is not None]
        container.add_item(
            discord.ui.TextDisplay(
                f"**Canales que Noah puede leer** ({len(readable)})\n"
                "-# Para los usuarios, y solo si ellos también pueden verlos. Los admins pueden leerlo todo."
            )
        )
        channel_select: discord.ui.ChannelSelect[Any] = discord.ui.ChannelSelect(
            placeholder="Elige los canales públicos…",
            channel_types=READABLE_CHANNEL_TYPES,
            min_values=0,
            max_values=25,
            default_values=readable[:25],
        )
        channel_select.callback = self._on_readable
        self._channel_select = channel_select
        container.add_item(discord.ui.ActionRow(channel_select))

        enabled_topics = settings.enabled_topics(guild.id)
        container.add_item(
            discord.ui.TextDisplay(
                f"**Temas que Noah puede comentar** ({len(enabled_topics)}/{len(TOPICS)})\n"
                "-# Para los usuarios. Los niveles de voz no se comentan nunca."
            )
        )
        topic_select: discord.ui.Select[Any] = discord.ui.Select(
            placeholder="Ningún tema permitido",
            min_values=0,
            max_values=len(TOPICS),
            options=[
                discord.SelectOption(label=label, value=key, description=description, default=key in enabled_topics)
                for key, (label, description) in TOPICS.items()
            ],
        )
        topic_select.callback = self._on_topics
        self._topic_select = topic_select
        container.add_item(discord.ui.ActionRow(topic_select))

        problems = self._problems()
        if problems:
            container.add_item(discord.ui.TextDisplay("\n".join(f"⚠️ {problem}" for problem in problems)))
        container.add_item(discord.ui.Separator())

        row: discord.ui.ActionRow[Any] = discord.ui.ActionRow()
        publish = discord.ui.Button(label="Publicar panel", emoji="📌", style=discord.ButtonStyle.primary)
        publish.callback = self._publish
        toggle = discord.ui.Button(
            label="Desactivar" if enabled else "Activar",
            style=discord.ButtonStyle.danger if enabled else discord.ButtonStyle.success,
        )
        toggle.callback = self._toggle
        chats = discord.ui.Button(label="Ver chats", emoji="💬", style=discord.ButtonStyle.secondary)
        chats.callback = self._show_chats
        row.add_item(publish)
        row.add_item(toggle)
        row.add_item(chats)
        container.add_item(row)
        self.add_item(container)

    async def refresh(self, interaction: discord.Interaction) -> None:
        self._build()
        if interaction.response.is_done():
            await interaction.edit_original_response(view=self, allowed_mentions=NONE)
        else:
            await interaction.response.edit_message(view=self, allowed_mentions=NONE)

    async def _edit_model(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(ModelModal(self))

    async def _edit_key(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(ApiKeyModal(self))

    async def _edit_channels(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(ChannelsModal(self))

    async def _edit_limit(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(LimitModal(self))

    async def _on_readable(self, interaction: discord.Interaction) -> None:
        self.settings.set_readable_channels(self.guild.id, [channel.id for channel in self._channel_select.values])
        await self.refresh(interaction)

    async def _on_topics(self, interaction: discord.Interaction) -> None:
        self.settings.set_enabled_topics(self.guild.id, list(self._topic_select.values))
        await self.refresh(interaction)

    async def _toggle(self, interaction: discord.Interaction) -> None:
        self.settings.set_enabled(self.guild.id, not self.settings.is_enabled(self.guild.id))
        await self.refresh(interaction)

    async def _publish(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        problem = await publish_panel(self.bot, self.guild)
        await self.refresh(interaction)
        await interaction.followup.send(f"❌ {problem}" if problem else "✅ Panel publicado.", ephemeral=True)

    async def _show_chats(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            f"**Últimos chats de Noah AI**\n{_threads_list_text(self.guild, self.settings)}",
            ephemeral=True,
            allowed_mentions=NONE,
        )


def _help_view(is_admin: bool) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    container = discord.ui.Container(accent_color=AI_COLOR)
    text = (
        "## Noah AI\n"
        "Chats **privados** con Noah. Puede leer los canales que el staff permite (solo leer, "
        "nunca escribir ni borrar) y responder cosas como: quién se ha presentado, cuándo se "
        "conectó alguien a VC, con quién habla o coincide más, o un resumen del día.\n\n"
        "**Cómo se usa**\n"
        "- Pulsa **💬 Abrir chat privado** en el panel. Puedes abrir varios chats.\n"
        "- **⚙️ Configurar mi Noah**: longitud, estilo propio y si ver el razonamiento.\n"
        "- Límite de preguntas al día (los admins no tienen).\n"
        "- Si intentas sacarle información restringida te avisa; si insistes, cierra el chat y avisa al staff."
    )
    if is_admin:
        text += (
            "\n\n**Admins**\n"
            "- `.noah ai config`: modelo, API key, canales, límite, canales legibles, temas y panel.\n"
            "- `.noah ai chats`: últimos chats abiertos.\n"
            "- Los admins pueden pedir a Noah que lea cualquier canal, los chats, la auditoría y los eventos.\n"
            "-# El canal de chats debe dejar a todos **Ver canal** y **Enviar mensajes en hilos**, pero no "
            "**Enviar mensajes** ni **Crear hilos**. El bot necesita crear hilos privados y gestionar hilos."
        )
    container.add_item(discord.ui.TextDisplay(text))
    view.add_item(container)
    return view


def register_ai_commands(bot: commands.Bot, noah_group: commands.Group) -> None:
    @noah_group.group(name="ai", invoke_without_command=True)
    async def ai(ctx: commands.Context) -> None:
        await ctx.send(view=_help_view(_is_admin(ctx.author)), allowed_mentions=NONE)

    @ai.command(name="help")
    async def ai_help(ctx: commands.Context) -> None:
        await ctx.send(view=_help_view(_is_admin(ctx.author)), allowed_mentions=NONE)

    @ai.command(name="config")
    async def ai_config(ctx: commands.Context) -> None:
        if ctx.guild is None:
            await ctx.send("❌ Este comando solo funciona dentro de un servidor.")
            return
        if not _is_admin(ctx.author):
            await ctx.send("❌ Solo los admins pueden configurar Noah AI.")
            return
        await ctx.send(view=AiConfigView(bot, ctx.guild, ctx.author.id), allowed_mentions=NONE)

    @ai.command(name="chats", aliases=["hilos"])
    async def ai_chats(ctx: commands.Context) -> None:
        if ctx.guild is None or not _is_admin(ctx.author):
            await ctx.send("❌ Solo los admins pueden ver los chats de Noah AI.")
            return
        await ctx.send(
            f"**Últimos chats de Noah AI**\n{_threads_list_text(ctx.guild, _settings(bot))}",
            allowed_mentions=NONE,
        )

    @bot.listen("on_ready")
    async def _ai_ready() -> None:
        bot.add_view(AiPanelView())
        bot.add_dynamic_items(ThinkToggle)
        await noah_ai_emojis.load(bot)

    @bot.listen("on_message")
    async def _ai_on_message(message: discord.Message) -> None:
        if message.author.bot or message.guild is None or not isinstance(message.channel, discord.Thread):
            return
        data = _settings(bot).get_thread(message.guild.id, message.channel.id)
        if data is None or data.get("closed") or data.get("owner_id") != message.author.id:
            return
        if not isinstance(message.author, discord.Member):
            return

        lock = _thread_locks.setdefault(message.channel.id, asyncio.Lock())
        if lock.locked():
            with suppress(discord.HTTPException):
                await message.reply(
                    "-# Espera a que termine de contestar lo anterior 🖤", mention_author=False, allowed_mentions=NONE
                )
            return
        async with lock:
            await _handle_question(bot, message)
