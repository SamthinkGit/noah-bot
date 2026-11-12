import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

import discord
from discord.ext import commands

from noah_bot.commands.presentations import find_presentation
from noah_bot.commands.rrpp import RRPP_COLOR, is_rrpp_admin
from noah_bot.modules.bot_context import get_bot_context
from noah_bot.modules.rrpp import CLAIM_ACCEPTED, RrppStore
from noah_bot.modules.user_activity import FIRST_MESSAGES_LIMIT, local_day, message_entry


_log = logging.getLogger(__name__)

INSPECT_TIMEOUT = 900
NEW_ACCOUNT_DAYS = 30
TEXT_LIMIT = 1800
PRESENTATION_LIMIT = 1500
HISTORY_SCAN_PER_CHANNEL = 300
HISTORY_SCAN_CONCURRENCY = 4
HISTORY_SCAN_TIMEOUT = 40
TOP_CHANNELS_SHOWN = 5
WEEKDAYS = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")
MONTHS = ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic")
CLOSE_DETAIL = "close"
# Detalle: (emoji, nombre, descripción del desplegable).
DETAILS: dict[str, tuple[str, str, str]] = {
    "presentation": ("📝", "Presentación", "Texto completo e imagen"),
    "first": ("💬", "Primeros mensajes", "Sus 5 primeros mensajes, con enlace"),
    "activity": ("📈", "Actividad", "Mensajes de la semana y por canal"),
    "voice": ("🎧", "Voz", "Tiempo, estado y nivel de .noah vc"),
    "rrpp": ("🤝", "RRPP", "Quién le saludó, presentó y llevó al VC"),
    "server": ("🏷️", "Roles y servidor", "Roles, apodo, boost y avisos"),
}
# Tipo de claim: (emoji, nombre) en el detalle RRPP.
CLAIM_STEPS = (
    ("greeting", "👋", "Saludo"),
    ("presentation", "📝", "Presentación"),
    ("vc", "🎧", "VC"),
)


# -- Formato -----------------------------------------------------------------


def _plural(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


def _minutes(total_minutes: int) -> str:
    """Mismo formato que `.noah vc`: `6h 40m` o `52 min`."""
    hours, minutes = divmod(max(int(total_minutes), 0), 60)
    return f"{minutes} min" if hours == 0 else f"{hours}h {minutes}m"


def _hours(value: Any) -> str:
    if value is None:
        return "-"
    number = float(value)
    return f"{int(number)}h" if number.is_integer() else f"{number:g}h"


def _ts(moment: float, style: str = "f") -> str:
    return f"<t:{int(moment)}:{style}>"


def _jump(guild_id: int, channel_id: Any, message_id: Any) -> str:
    return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"


def _quote(text: str) -> str:
    text = discord.utils.escape_mentions(discord.utils.escape_markdown(text))
    return "> " + text.replace("\n", "\n> ")


def _clip(text: str, limit: int = TEXT_LIMIT) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _iso_ts(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


def _day_name(day: date) -> str:
    return f"{WEEKDAYS[day.weekday()]} {day.day} {MONTHS[day.month - 1]}"


# -- Datos del usuario -------------------------------------------------------


@dataclass
class Profile:
    guild: discord.Guild
    user: discord.Member | discord.User
    member: discord.Member | None
    activity: dict[str, Any]
    tracking_since: float | None
    voice: dict[str, Any] | None
    presented: bool | None
    presentation_ref: tuple[int, int] | None
    claims: list[dict[str, Any]] = field(default_factory=list)
    help_at: float | None = None

    @property
    def joined_at(self) -> float | None:
        if self.member is None or self.member.joined_at is None:
            return None
        return self.member.joined_at.timestamp()

    def counts_from_join(self) -> bool:
        """True si Noah ya contaba mensajes cuando el usuario entró."""
        joined = self.joined_at
        return self.tracking_since is not None and joined is not None and joined >= self.tracking_since


def _is_rrpp(member: discord.abc.User, store: RrppStore) -> bool:
    if not isinstance(member, discord.Member):
        return False
    role_id = store.get_role_id(member.guild.id)
    return role_id is not None and any(role.id == role_id for role in member.roles)


def _load_profile(
    bot: commands.Bot, guild: discord.Guild, user: discord.Member | discord.User
) -> Profile:
    context = get_bot_context(bot)
    member = user if isinstance(user, discord.Member) else guild.get_member(user.id)
    presentations = context.presentations
    presented = (
        None
        if presentations.get_channel_id(guild.id) is None
        else presentations.has_completed(guild.id, user.id)
    )
    return Profile(
        guild=guild,
        user=member or user,
        member=member,
        activity=context.user_activity.get_user(guild.id, user.id),
        tracking_since=context.user_activity.tracking_since(guild.id),
        voice=context.voice_manager.get_user_stats(user.id),
        presented=presented,
        presentation_ref=presentations.get_presentation_message(guild.id, user.id),
        claims=context.rrpp.list_claims_about(guild.id, user.id),
        help_at=context.rrpp.help_requested_at(guild.id, user.id),
    )


# -- Ficha -------------------------------------------------------------------


def _header_text(profile: Profile) -> str:
    user = profile.user
    lines = [f"## {discord.utils.escape_markdown(user.display_name)}", f"{user.mention} · `{user.id}`"]

    flags: list[str] = []
    account_days = (discord.utils.utcnow() - user.created_at).days
    if user.bot:
        flags.append("🤖 Bot")
    if account_days < NEW_ACCOUNT_DAYS:
        flags.append(f"🆕 Cuenta nueva ({_plural(account_days, 'día', 'días')})")
    member = profile.member
    if member is None:
        flags.append("🚪 Ya no está en el servidor")
    else:
        if member.is_timed_out() and member.timed_out_until is not None:
            flags.append(f"⏳ Aislado hasta {_ts(member.timed_out_until.timestamp(), 'R')}")
        if member.pending:
            flags.append("⚠️ No ha aceptado las normas")
    if flags:
        lines.append("-# " + " · ".join(flags))
    return "\n".join(lines)


def _summary_text(profile: Profile) -> str:
    created = profile.user.created_at.timestamp()
    lines = [f"🪪 **Cuenta:** creada el {_ts(created, 'D')} ({_ts(created, 'R')})"]

    joined = profile.joined_at
    if joined is not None:
        lines.append(f"🚪 **En el servidor:** desde el {_ts(joined, 'D')} ({_ts(joined, 'R')})")

    if profile.presented is None:
        presentation = "sin configurar"
    elif not profile.presented:
        presentation = "❌ todavía no"
    else:
        presentation = "✅ hecha"
        if profile.presentation_ref is not None and joined is not None:
            presented_at = discord.utils.snowflake_time(profile.presentation_ref[1]).timestamp()
            if presented_at >= joined:
                presentation += f", {_minutes(int(presented_at - joined) // 60)} después de entrar"
    lines.append(f"📝 **Presentación:** {presentation}")

    messages = f"💬 **Mensajes:** {int(profile.activity.get('messages', 0))}"
    last = profile.activity.get("last")
    if isinstance(last, dict):
        messages += f" · el último {_ts(float(last.get('at', 0)), 'R')}"
    lines.append(messages)

    voice = profile.voice
    if voice is None:
        lines.append("🎧 **Voz:** nunca ha entrado")
    else:
        status = " · 🟢 en VC ahora" if voice.get("is_connected") else ""
        lines.append(f"🎧 **Voz:** {_minutes(int(voice.get('effective_total_minutes', 0)))}{status}")
    return "\n".join(lines)


# -- Detalles ----------------------------------------------------------------


async def _scan_first_messages(
    guild: discord.Guild, user_id: int, after: datetime
) -> list[dict[str, Any]]:
    """Busca los primeros mensajes del usuario en el historial de cada canal."""
    me = guild.me
    channels = [
        channel
        for channel in guild.text_channels
        if channel.permissions_for(me).read_message_history
        and channel.permissions_for(me).view_channel
    ]
    semaphore = asyncio.Semaphore(HISTORY_SCAN_CONCURRENCY)

    async def _scan(channel: discord.TextChannel) -> list[discord.Message]:
        found: list[discord.Message] = []
        async with semaphore:
            with suppress(discord.HTTPException):
                async for message in channel.history(
                    limit=HISTORY_SCAN_PER_CHANNEL, after=after, oldest_first=True
                ):
                    if message.author.id == user_id:
                        found.append(message)
                        if len(found) >= FIRST_MESSAGES_LIMIT:
                            break
        return found

    tasks = [asyncio.create_task(_scan(channel)) for channel in channels]
    if not tasks:
        return []
    done, pending = await asyncio.wait(tasks, timeout=HISTORY_SCAN_TIMEOUT)
    for task in pending:
        task.cancel()
    messages = [message for task in done if not task.exception() for message in task.result()]
    messages.sort(key=lambda message: message.created_at)
    return [message_entry(message) for message in messages[:FIRST_MESSAGES_LIMIT]]


def _title(key: str) -> str:
    emoji, name, _ = DETAILS[key]
    return f"### {emoji} {name}"


async def _presentation_items(bot: commands.Bot, profile: Profile) -> list[discord.ui.Item[Any]]:
    title = _title("presentation")
    if profile.presented is None:
        return [discord.ui.TextDisplay(f"{title}\nLas presentaciones no están configuradas.")]
    if not profile.presented:
        return [discord.ui.TextDisplay(f"{title}\n❌ Todavía no se ha presentado.")]

    store = get_bot_context(bot).presentations
    message = await find_presentation(store, profile.guild, profile.user.id)
    if message is None:
        return [
            discord.ui.TextDisplay(
                f"{title}\n✅ Está marcado como presentado, pero no encuentro su mensaje."
            )
        ]

    meta = f"-# {_ts(message.created_at.timestamp())} en {message.channel.mention}"
    content = message.content.strip() or "*Sin texto.*"
    items: list[discord.ui.Item[Any]] = [
        discord.ui.TextDisplay(
            f"{title}\n{meta}\n\n{_clip(content, PRESENTATION_LIMIT)}\n\n"
            f"[↗ Ir al mensaje]({message.jump_url})"
        )
    ]
    images = [
        attachment.url
        for attachment in message.attachments
        if attachment.content_type and attachment.content_type.startswith("image/")
    ]
    if images:
        items.append(discord.ui.MediaGallery(*(discord.MediaGalleryItem(url) for url in images[:4])))
    return items


async def _first_messages_text(bot: commands.Bot, profile: Profile) -> str:
    title = _title("first")
    activity = profile.activity
    entries: list[dict[str, Any]] = list(activity.get("first_messages", []))
    scanned = bool(activity.get("history_scanned"))
    member = profile.member

    if (
        member is not None
        and member.joined_at is not None
        and not scanned
        and not profile.counts_from_join()
    ):
        found = await _scan_first_messages(
            profile.guild, member.id, member.joined_at - timedelta(minutes=1)
        )
        entries = get_bot_context(bot).user_activity.merge_first_messages(
            profile.guild.id, member.id, found
        )
        scanned = True

    if not entries:
        return f"{title}\nNo he encontrado mensajes suyos."

    blocks = [title]
    for number, entry in enumerate(entries, start=1):
        url = _jump(profile.guild.id, entry.get("channel_id"), entry.get("message_id"))
        blocks.append(
            f"**{number}.** {_ts(float(entry.get('at', 0)))} en <#{entry.get('channel_id')}>\n"
            f"{_quote(str(entry.get('text', '…')))}\n"
            f"[↗ Ir al mensaje]({url})"
        )

    since = profile.tracking_since
    if (
        since is not None
        and not profile.counts_from_join()
        and all(float(entry.get("at", 0)) >= since for entry in entries)
    ):
        blocks.append(
            f"-# No los encuentro en el historial; son los primeros desde que Noah cuenta "
            f"mensajes ({_ts(since, 'D')})."
        )
    elif scanned:
        blocks.append("-# Buscados en el historial de los canales.")
    return _clip("\n\n".join(blocks))


def _activity_text(profile: Profile) -> str:
    activity = profile.activity
    days: dict[str, int] = activity.get("days", {})
    today = local_day()
    week = [today - timedelta(days=offset) for offset in range(7)]
    counts = {day: int(days.get(day.isoformat(), 0)) for day in week}

    lines = [
        _title("activity"),
        f"**Hoy:** {counts[today]}",
        f"**Últimos 7 días:** {sum(counts.values())}",
    ]
    busiest = max(week, key=lambda day: counts[day])
    if counts[busiest]:
        lines.append(f"**Día más activo:** {_day_name(busiest)} ({counts[busiest]})")

    channels: dict[str, int] = activity.get("channels", {})
    total = sum(channels.values())
    ranked = sorted(channels.items(), key=lambda item: item[1], reverse=True)[:TOP_CHANNELS_SHOWN]
    if ranked:
        lines.append("")
        lines.append("**Dónde escribe**")
        lines.extend(f"<#{channel_id}> · {count} ({count * 100 // total} %)" for channel_id, count in ranked)

    last = activity.get("last")
    if isinstance(last, dict):
        url = _jump(profile.guild.id, last.get("channel_id"), last.get("message_id"))
        lines.append("")
        lines.append(
            f"**Último mensaje:** {_ts(float(last.get('at', 0)), 'R')} en <#{last.get('channel_id')}> · "
            f"[↗ Ir al mensaje]({url})"
        )
    if not profile.counts_from_join() and profile.tracking_since is not None:
        lines.append(f"-# Noah cuenta mensajes desde el {_ts(profile.tracking_since, 'D')}.")
    return _clip("\n".join(lines))


def _voice_text(profile: Profile) -> str:
    """Los mismos datos que `.noah vc stats`, con aire entre bloques."""
    title = _title("voice")
    voice = profile.voice
    if voice is None:
        return f"{title}\n🔇 Todavía no tiene stats de voz."

    lines = [
        title,
        f"**Tiempo total:** {_minutes(int(voice.get('effective_total_minutes', 0)))}",
        f"**Sesiones:** {int(voice.get('sessions', 0))}",
        "",
    ]

    active = voice.get("active_session") or {}
    if voice.get("is_connected"):
        lines.append(f"**Estado:** 🟢 Conectado en {active.get('channel_name') or 'un canal'}")
        lines.append(f"**Sesión actual:** {_minutes(int(voice.get('current_session_minutes', 0)))}")
    else:
        lines.append("**Estado:** ⚫ Desconectado")
        lines.append(f"**Último canal:** {voice.get('last_channel_name') or '-'}")

    joined = _iso_ts(voice.get("last_joined_at"))
    left = _iso_ts(voice.get("last_left_at"))
    lines.append(f"**Última entrada:** {_ts(joined, 'R') if joined is not None else '-'}")
    lines.append(f"**Última salida:** {_ts(left, 'R') if left is not None else '-'}")

    level = voice.get("level_data")
    lines.append("")
    if isinstance(level, dict):
        lines.append(f"**Nivel:** {level['level']}")
        lines.append(
            f"**Progreso:** {_minutes(int(level['progress_minutes']))} / "
            f"{_minutes(int(level['minutes_per_level']))} ({int(level['progress_percent'])} %)"
        )
        lines.append(f"**Siguiente nivel en:** {_minutes(int(level['remaining_minutes']))}")
        lines.append(f"-# Cada nivel pide {_hours(level.get('hours_per_level'))} en voz.")
    else:
        lines.append("**Nivel:** sistema desactivado")
    return _clip("\n".join(lines))


def _rrpp_text(profile: Profile) -> str:
    lines = [_title("rrpp")]
    for claim_type, emoji, name in CLAIM_STEPS:
        rrpps: list[str] = []
        for claim in reversed(profile.claims):
            if claim.get("type") != claim_type or claim.get("status") != CLAIM_ACCEPTED:
                continue
            mention = f"<@{claim.get('rrpp_id')}>"
            if mention not in rrpps:
                rrpps.append(mention)
        lines.append(f"{emoji} **{name}:** {', '.join(rrpps) if rrpps else 'nadie todavía'}")

    if profile.help_at is not None:
        lines.append(f"📩 **Pidió ayuda:** sí, {_ts(profile.help_at, 'R')}")
    else:
        lines.append("📩 **Pidió ayuda:** no")
    return "\n".join(lines)


def _server_text(profile: Profile) -> str:
    member = profile.member
    user = profile.user
    lines = [_title("server")]
    if member is None:
        lines.append("🚪 Ya no está en el servidor.")
        return "\n".join(lines)

    roles = [role for role in reversed(member.roles) if not role.is_default()]
    lines.append(f"**Roles ({len(roles)}):** " + (" ".join(role.mention for role in roles) if roles else "ninguno"))
    lines.append("")
    lines.append(f"**Usuario:** `{user.name}`")
    if member.nick:
        lines.append(f"**Apodo:** {discord.utils.escape_markdown(member.nick)}")
    if member.premium_since is not None:
        lines.append(f"💎 **Booster** desde el {_ts(member.premium_since.timestamp(), 'D')}")
    if member.guild_avatar is not None:
        lines.append("🖼️ Usa un avatar propio en este servidor")
    if member.is_timed_out() and member.timed_out_until is not None:
        lines.append(f"⏳ **Aislado** hasta {_ts(member.timed_out_until.timestamp(), 'F')}")
    if member.pending:
        lines.append("⚠️ Todavía no ha aceptado las normas del servidor")
    return _clip("\n".join(lines))


async def _detail_items(
    bot: commands.Bot, profile: Profile, detail: str
) -> list[discord.ui.Item[Any]]:
    if detail == "presentation":
        return await _presentation_items(bot, profile)
    if detail == "first":
        text = await _first_messages_text(bot, profile)
    elif detail == "activity":
        text = _activity_text(profile)
    elif detail == "voice":
        text = _voice_text(profile)
    elif detail == "rrpp":
        text = _rrpp_text(profile)
    else:
        text = _server_text(profile)
    return [discord.ui.TextDisplay(text)]


# -- Vista -------------------------------------------------------------------


def _loading_view(text: str) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(discord.ui.Container(discord.ui.TextDisplay(f"⏳ {text}"), accent_color=RRPP_COLOR))
    return view


def _loading_text(user: discord.abc.User | None, detail: str | None) -> str:
    if user is None:
        return "Cargando…"
    name = discord.utils.escape_markdown(user.display_name)
    if detail in DETAILS:
        return f"Cargando **{DETAILS[detail][1]}** de **{name}**…"
    return f"Cargando la ficha de **{name}**…"


class InspectSearchModal(discord.ui.Modal, title="Buscar usuario"):
    def __init__(self, panel: "InspectView") -> None:
        super().__init__(timeout=300)
        self.panel = panel
        self.user_select = discord.ui.UserSelect(
            placeholder="Escribe un nombre…", min_values=1, max_values=1
        )
        self.add_item(discord.ui.Label(text="¿A quién quieres inspeccionar?", component=self.user_select))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.panel.show(interaction, self.user_select.values[0], self.panel.detail)


class InspectView(discord.ui.LayoutView):
    def __init__(
        self,
        bot: commands.Bot,
        guild: discord.Guild,
        author_id: int,
        user: discord.Member | discord.User | None = None,
        detail: str | None = None,
    ) -> None:
        super().__init__(timeout=INSPECT_TIMEOUT)
        self.bot = bot
        self.guild = guild
        self.author_id = author_id
        self.user = user
        self.detail = detail if user is not None else None
        self.message: discord.Message | None = None

    @classmethod
    async def create(
        cls,
        bot: commands.Bot,
        guild: discord.Guild,
        author_id: int,
        user: discord.Member | discord.User | None = None,
        detail: str | None = None,
    ) -> "InspectView":
        view = cls(bot, guild, author_id, user, detail)
        await view._render()
        return view

    async def _render(self) -> None:
        container = discord.ui.Container(accent_color=RRPP_COLOR)
        profile = _load_profile(self.bot, self.guild, self.user) if self.user is not None else None

        if profile is None:
            container.add_item(
                discord.ui.TextDisplay(
                    "## 🔎 Inspector RRPP\nPulsa **👤 Buscar** para elegir a quién inspeccionar."
                )
            )
        else:
            container.add_item(
                discord.ui.Section(
                    discord.ui.TextDisplay(_header_text(profile)),
                    accessory=discord.ui.Thumbnail(profile.user.display_avatar.url),
                )
            )
            container.add_item(discord.ui.Separator())
            container.add_item(discord.ui.TextDisplay(_summary_text(profile)))
            if self.detail in DETAILS:
                container.add_item(discord.ui.Separator())
                for item in await _detail_items(self.bot, profile, self.detail):
                    container.add_item(item)
            container.add_item(discord.ui.Separator())
            container.add_item(discord.ui.ActionRow(self._detail_select()))

        container.add_item(self._button_row(profile))
        self.add_item(container)

    def _detail_select(self) -> discord.ui.Select[Any]:
        options = [
            discord.SelectOption(
                label=name, value=key, description=description, emoji=emoji, default=key == self.detail
            )
            for key, (emoji, name, description) in DETAILS.items()
        ]
        if self.detail is not None:
            options.append(
                discord.SelectOption(
                    label="Cerrar detalle", value=CLOSE_DETAIL, description="Deja solo la ficha", emoji="✖️"
                )
            )
        select: discord.ui.Select[Any] = discord.ui.Select(
            placeholder="🔎 Ver detalle…", min_values=1, max_values=1, options=options
        )

        async def _on_select(interaction: discord.Interaction) -> None:
            choice = select.values[0]
            await self.show(interaction, self.user, None if choice == CLOSE_DETAIL else choice)

        select.callback = _on_select
        return select

    def _button_row(self, profile: Profile | None) -> discord.ui.ActionRow[Any]:
        row: discord.ui.ActionRow[Any] = discord.ui.ActionRow()
        search = discord.ui.Button(label="Buscar", emoji="👤", style=discord.ButtonStyle.primary)

        async def _search(interaction: discord.Interaction) -> None:
            await interaction.response.send_modal(InspectSearchModal(self))

        search.callback = _search
        row.add_item(search)

        if profile is not None and profile.presentation_ref is not None:
            channel_id, message_id = profile.presentation_ref
            row.add_item(
                discord.ui.Button(
                    label="Presentación",
                    emoji="📝",
                    url=_jump(self.guild.id, channel_id, message_id),
                )
            )
        return row

    async def show(
        self,
        interaction: discord.Interaction,
        user: discord.Member | discord.User | None,
        detail: str | None,
    ) -> None:
        self.stop()
        await interaction.response.edit_message(
            view=_loading_view(_loading_text(user, detail)),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        try:
            view = await InspectView.create(self.bot, self.guild, self.author_id, user, detail)
        except Exception:
            # Que la ficha no se quede en "Cargando…": se vuelve a la ficha sin detalle.
            _log.exception("No se ha podido cargar el inspector RRPP")
            view = await InspectView.create(self.bot, self.guild, self.author_id, user)
            await interaction.followup.send(
                "❌ No he podido cargar ese detalle. Inténtalo otra vez.", ephemeral=True
            )
        view.message = self.message or interaction.message
        await interaction.edit_original_response(
            view=view, allowed_mentions=discord.AllowedMentions.none()
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Este inspector es de otra persona. Abre el tuyo con `.noah rrpp inspect`.",
                ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self) -> None:
        if self.message is None:
            return
        for item in self.walk_children():
            if isinstance(item, discord.ui.Select) or (
                isinstance(item, discord.ui.Button) and item.url is None
            ):
                item.disabled = True
        with suppress(discord.HTTPException):
            await self.message.edit(view=self, allowed_mentions=discord.AllowedMentions.none())


def register_rrpp_inspect_commands(bot: commands.Bot, rrpp_group: commands.Group) -> None:
    @bot.listen("on_message")
    async def _track_user_activity(message: discord.Message) -> None:
        if message.author.bot or message.guild is None:
            return
        get_bot_context(bot).user_activity.record_message(message)

    @rrpp_group.command(name="inspect")
    async def inspect(
        ctx: commands.Context, user: discord.Member | discord.User | None = None
    ) -> None:
        if ctx.guild is None:
            await ctx.send("❌ Este comando solo funciona dentro de un servidor.")
            return

        store = get_bot_context(ctx.bot).rrpp
        if not (_is_rrpp(ctx.author, store) or is_rrpp_admin(ctx.author)):
            await ctx.send("❌ Solo los RRPP y los administradores pueden usar este comando.")
            return

        none = discord.AllowedMentions.none()
        message = await ctx.send(view=_loading_view(_loading_text(user, None)), allowed_mentions=none)
        view = await InspectView.create(ctx.bot, ctx.guild, ctx.author.id, user)
        view.message = message
        await message.edit(view=view, allowed_mentions=none)

    @inspect.error
    async def _inspect_error(ctx: commands.Context, error: commands.CommandError) -> None:
        if isinstance(error, (commands.MemberNotFound, commands.UserNotFound, commands.BadUnionArgument)):
            await ctx.send("❌ No encuentro a ese usuario. Usa `.noah rrpp inspect` y pulsa 👤 Buscar.")
