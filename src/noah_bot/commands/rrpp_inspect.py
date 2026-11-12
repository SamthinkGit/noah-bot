import asyncio
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import discord
from discord.ext import commands

from noah_bot.commands.presentations import find_presentation
from noah_bot.commands.rrpp import RRPP_COLOR, is_rrpp_admin
from noah_bot.modules.bot_context import get_bot_context
from noah_bot.modules.rrpp import CLAIM_ACCEPTED, CLAIM_REJECTED, RrppStore, level_for_points
from noah_bot.modules.user_activity import FIRST_MESSAGES_LIMIT, local_day, message_entry


INSPECT_TIMEOUT = 900
NEWCOMER_DAYS = 14
NEW_ACCOUNT_DAYS = 30
TEXT_LIMIT = 1800
PRESENTATION_LIMIT = 1500
HISTORY_SCAN_PER_CHANNEL = 300
HISTORY_SCAN_CONCURRENCY = 4
HISTORY_SCAN_TIMEOUT = 40
RECENT_SESSIONS_SHOWN = 5
TOP_CHANNELS_SHOWN = 5
SPARK_BLOCKS = "▁▂▃▄▅▆▇█"
WEEKDAYS = "LMXJVSD"
CLOSE_DETAIL = "close"
# Detalle: (emoji, nombre, descripción del desplegable).
DETAILS: dict[str, tuple[str, str, str]] = {
    "presentation": ("📝", "Presentación", "Texto completo e imagen"),
    "first": ("💬", "Primeros mensajes", "Sus 5 primeros mensajes, con enlace"),
    "activity": ("📈", "Actividad", "Mensajes por día y por canal"),
    "voice": ("🎧", "Sesiones de voz", "Nivel, puesto y últimas sesiones"),
    "rrpp": ("🤝", "RRPP", "Quién le saludó, presentó y llevó al VC"),
    "server": ("🏷️", "Roles y servidor", "Roles, apodo, boost y avisos"),
}
CLAIM_NAMES = {"greeting": ("👋", "Saludo"), "presentation": ("📝", "Presentación"), "vc": ("🎧", "VC")}


# -- Formato -----------------------------------------------------------------


def _plural(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


def _span(seconds: float) -> str:
    """Tiempo aproximado en la unidad más grande: `5 años`, `3 meses`, `12 días`…"""
    seconds = max(0, int(seconds))
    if seconds < 3600:
        return f"{seconds // 60} min"
    if seconds < 86400:
        return f"{seconds // 3600} h"
    days = seconds // 86400
    if days < 60:
        return _plural(days, "día", "días")
    if days < 730:
        return f"{days // 30} meses"
    return f"{days // 365} años"


def _ago(moment: float) -> str:
    seconds = discord.utils.utcnow().timestamp() - moment
    return "ahora" if seconds < 60 else f"hace {_span(seconds)}"


def _duration(seconds: float) -> str:
    minutes = max(0, int(seconds)) // 60
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours} h {minutes:02d} min" if minutes else f"{hours} h"
    return f"{minutes} min"


def _ts(moment: float, style: str = "f") -> str:
    return f"<t:{int(moment)}:{style}>"


def _jump(guild_id: int, channel_id: Any, message_id: Any) -> str:
    return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"


def _link_label(text: str) -> str:
    text = discord.utils.escape_markdown(text).replace("[", "(").replace("]", ")")
    return discord.utils.escape_mentions(text)


def _clip(text: str, limit: int = TEXT_LIMIT) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _iso_ts(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


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
    rrpp_points: int | None = None

    @property
    def joined_at(self) -> float | None:
        if self.member is None or self.member.joined_at is None:
            return None
        return self.member.joined_at.timestamp()

    @property
    def voice_seconds(self) -> int:
        return int(self.voice.get("effective_total_seconds", 0)) if self.voice else 0

    def has_claim(self, claim_type: str) -> bool:
        return any(
            claim.get("type") == claim_type and claim.get("status") == CLAIM_ACCEPTED
            for claim in self.claims
        )

    def steps(self) -> list[tuple[str, bool]]:
        return [
            ("👋 Saludo", self.has_claim("greeting")),
            ("📝 Presentación", bool(self.presented)),
            ("💬 Habla", int(self.activity.get("messages", 0)) > 0),
            ("🎧 VC", self.voice_seconds > 0 or self.has_claim("vc")),
        ]

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
    rrpp = context.rrpp
    points = rrpp.get_points(guild.id, user.id)
    return Profile(
        guild=guild,
        user=member or user,
        member=member,
        activity=context.user_activity.get_user(guild.id, user.id),
        tracking_since=context.user_activity.tracking_since(guild.id),
        voice=context.voice_manager.get_user_stats(user.id),
        presented=presented,
        presentation_ref=presentations.get_presentation_message(guild.id, user.id),
        claims=rrpp.list_claims_about(guild.id, user.id),
        help_at=rrpp.help_requested_at(guild.id, user.id),
        rrpp_points=points if (points or _is_rrpp(member or user, rrpp)) else None,
    )


def _join_position(guild: discord.Guild, member: discord.Member) -> int | None:
    if member.joined_at is None:
        return None
    return 1 + sum(
        1
        for other in guild.members
        if other.joined_at is not None and other.joined_at < member.joined_at
    )


def _newcomers(guild: discord.Guild) -> list[discord.Member]:
    """Miembros que han entrado en los últimos días, el más nuevo primero."""
    cutoff = discord.utils.utcnow() - timedelta(days=NEWCOMER_DAYS)
    members = [
        member
        for member in guild.members
        if not member.bot and member.joined_at is not None and member.joined_at >= cutoff
    ]
    members.sort(key=lambda member: member.joined_at, reverse=True)
    return members


# -- Ficha -------------------------------------------------------------------


def _header_text(profile: Profile) -> str:
    user = profile.user
    lines = [f"## {discord.utils.escape_markdown(user.display_name)}", f"{user.mention} · `{user.id}`"]

    flags: list[str] = []
    now = discord.utils.utcnow()
    account_days = (now - user.created_at).days
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


def _sheet_text(profile: Profile) -> str:
    rows: list[tuple[str, str, str]] = []
    now = discord.utils.utcnow()
    user = profile.user

    rows.append(("Cuenta", _span((now - user.created_at).total_seconds()), f"creada {user.created_at:%d/%m/%Y}"))

    if profile.member is not None and profile.member.joined_at is not None:
        position = _join_position(profile.guild, profile.member)
        rows.append(
            (
                "Servidor",
                _span((now - profile.member.joined_at).total_seconds()),
                f"llegada nº {position:,}".replace(",", ".") if position else "",
            )
        )
    else:
        rows.append(("Servidor", "fuera", ""))

    if profile.presented is None:
        rows.append(("Presentada", "-", "sin configurar"))
    elif profile.presented:
        extra = ""
        joined = profile.joined_at
        if profile.presentation_ref is not None and joined is not None:
            presented_at = discord.utils.snowflake_time(profile.presentation_ref[1]).timestamp()
            if presented_at >= joined:
                extra = f"+{_duration(presented_at - joined)} tras entrar"
        rows.append(("Presentada", "sí", extra))
    else:
        rows.append(("Presentada", "no", ""))

    activity = profile.activity
    today = int(activity.get("days", {}).get(local_day().isoformat(), 0))
    extra = f"hoy {today}"
    last = activity.get("last")
    if isinstance(last, dict):
        extra += f" · último {_ago(float(last.get('at', 0)))}"
    rows.append(("Mensajes", str(int(activity.get("messages", 0))), extra))

    voice = profile.voice
    if voice is None:
        rows.append(("Voz", "0 min", "nunca ha entrado"))
    else:
        if voice.get("is_connected"):
            extra = "en VC ahora"
        else:
            extra = _plural(int(voice.get("sessions", 0)), "sesión", "ses.")
            left = _iso_ts(voice.get("last_left_at"))
            if left is not None:
                extra += f" · última {_ago(left)}"
        rows.append(("Voz", _duration(profile.voice_seconds), extra))

    steps = profile.steps()
    done = sum(1 for _, ok in steps if ok)
    rows.append(("RRPP", f"{done}/{len(steps)} pasos", "pidió ayuda" if profile.help_at else ""))

    if profile.rrpp_points is not None:
        rows.append(
            ("Es RRPP", f"nivel {level_for_points(profile.rrpp_points)}", f"{profile.rrpp_points} puntos")
        )

    body = "\n".join(f"{label:<11}{value:<13}{extra}".rstrip() for label, value, extra in rows)
    return f"```\n{body}\n```"


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
    joined = profile.joined_at
    if joined is not None and message.created_at.timestamp() >= joined:
        meta += f" · {_duration(message.created_at.timestamp() - joined)} tras entrar"
    content = message.content.strip() or "*Sin texto.*"
    items: list[discord.ui.Item[Any]] = [
        discord.ui.TextDisplay(f"{title}\n{meta}\n{_clip(content, PRESENTATION_LIMIT)}")
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

    lines = [
        f"{_ts(float(entry.get('at', 0)))} <#{entry.get('channel_id')}> "
        f"[{_link_label(str(entry.get('text', '…')))}]"
        f"({_jump(profile.guild.id, entry.get('channel_id'), entry.get('message_id'))})"
        for entry in entries
    ]
    since = profile.tracking_since
    if (
        since is not None
        and not profile.counts_from_join()
        and all(float(entry.get("at", 0)) >= since for entry in entries)
    ):
        lines.append(
            f"-# No los encuentro en el historial; son los primeros desde que Noah cuenta "
            f"mensajes ({_ts(since, 'D')})."
        )
    elif scanned:
        lines.append("-# Buscados en el historial de los canales.")
    return _clip(f"{title}\n" + "\n".join(lines))


def _activity_text(profile: Profile) -> str:
    activity = profile.activity
    days: dict[str, int] = activity.get("days", {})
    today = local_day()
    week = [today - timedelta(days=offset) for offset in range(6, -1, -1)]
    counts = [int(days.get(day.isoformat(), 0)) for day in week]
    top = max(counts) or 1

    def _block(count: int) -> str:
        if count == 0:
            return " "
        return SPARK_BLOCKS[max(0, -(-count * len(SPARK_BLOCKS) // top) - 1)]

    chart = "\n".join(
        (
            "".join(f"{WEEKDAYS[day.weekday()]:>4}" for day in week),
            "".join(f"{_block(count) * 2:>4}" for count in counts),
            "".join(f"{count:>4}" for count in counts),
        )
    )
    lines = [_title("activity"), f"**{sum(counts)}** mensajes en los últimos 7 días", f"```\n{chart}\n```"]

    channels: dict[str, int] = activity.get("channels", {})
    total = sum(channels.values())
    ranked = sorted(channels.items(), key=lambda item: item[1], reverse=True)[:TOP_CHANNELS_SHOWN]
    if ranked:
        lines.append("**Dónde escribe**")
        lines.extend(
            f"<#{channel_id}> · **{count}** ({count * 100 // total} %)" for channel_id, count in ranked
        )

    last = activity.get("last")
    if isinstance(last, dict):
        lines.append(
            f"Último mensaje {_ts(float(last.get('at', 0)), 'R')} en <#{last.get('channel_id')}> · "
            f"[ir al mensaje]({_jump(profile.guild.id, last.get('channel_id'), last.get('message_id'))})"
        )
    if not profile.counts_from_join() and profile.tracking_since is not None:
        lines.append(f"-# Noah cuenta mensajes desde el {_ts(profile.tracking_since, 'D')}.")
    return _clip("\n".join(lines))


def _voice_text(bot: commands.Bot, profile: Profile) -> str:
    title = _title("voice")
    voice = profile.voice
    if voice is None:
        return f"{title}\nNunca ha entrado a un canal de voz."

    sessions = int(voice.get("sessions", 0))
    total = profile.voice_seconds
    summary = f"**{_duration(total)}** en {_plural(sessions, 'sesión', 'sesiones')}"
    if sessions:
        summary += f" · media {_duration(int(voice.get('total_seconds', 0)) / sessions)}"
    lines = [title, summary]

    manager = get_bot_context(bot).voice_manager
    member_ids = {member.id for member in profile.guild.members}
    ranking = manager.get_top_users(limit=len(member_ids) or 1, guild_member_ids=member_ids)
    position = next(
        (index for index, record in enumerate(ranking, start=1) if int(record["user_id"]) == profile.user.id),
        None,
    )
    level = voice.get("level_data")
    rank_parts: list[str] = []
    if isinstance(level, dict):
        rank_parts.append(
            f"Nivel VC **{level['level']}** · {level['progress_percent']} % hacia el {level['next_level']}"
        )
    if position is not None:
        rank_parts.append(f"puesto **#{position}** de {len(ranking)}")
    if rank_parts:
        lines.append(" · ".join(rank_parts))

    active = voice.get("active_session")
    if isinstance(active, dict):
        started = _iso_ts(active.get("started_at"))
        since = f" desde {_ts(started, 'R')}" if started is not None else ""
        lines.append(f"🟢 Ahora en <#{active.get('channel_id')}>{since}")

    recent = voice.get("recent_sessions")
    if isinstance(recent, list) and recent:
        lines.append("**Últimas sesiones**")
        for session in recent[:RECENT_SESSIONS_SHOWN]:
            started = _iso_ts(session.get("started_at"))
            when = _ts(started) if started is not None else "?"
            lines.append(f"{when} · {session.get('channel_name') or 'canal'} · **{_duration(session.get('seconds', 0))}**")
    else:
        last_joined = _iso_ts(voice.get("last_joined_at"))
        if last_joined is not None:
            lines.append(f"Última vez: {_ts(last_joined)} en {voice.get('last_channel_name') or 'un canal'}")

    first = _iso_ts(voice.get("first_session_at"))
    if first is not None:
        lines.append(f"-# Primera sesión registrada: {_ts(first, 'D')}")
    return _clip("\n".join(lines))


def _rrpp_text(profile: Profile) -> str:
    lines = [_title("rrpp"), " · ".join(f"{name} {'✅' if ok else '❌'}" for name, ok in profile.steps())]

    if profile.claims:
        lines.append("**Claims sobre esta persona**")
        for claim in profile.claims[:10]:
            emoji, name = CLAIM_NAMES.get(str(claim.get("type")), ("❔", str(claim.get("type"))))
            rejected = claim.get("status") == CLAIM_REJECTED
            points = "rechazado" if rejected else f"{int(claim.get('points', 0))} p"
            lines.append(
                f"`#{claim.get('id')}` {emoji} {name} · <@{claim.get('rrpp_id')}> · {points} · "
                f"{_ts(float(claim.get('created_at', 0)), 'd')}"
            )
    else:
        lines.append("Ningún RRPP ha hecho claims sobre esta persona.")

    if profile.help_at is not None:
        lines.append(f"📩 Pidió ayuda reaccionando a una cita RRPP {_ts(profile.help_at)}")
    else:
        lines.append("-# No ha reaccionado a ninguna cita RRPP.")
    return _clip("\n".join(lines))


def _server_text(profile: Profile) -> str:
    title = _title("server")
    member = profile.member
    user = profile.user
    lines = [title, f"Cuenta creada {_ts(user.created_at.timestamp(), 'F')}"]
    if member is None:
        lines.append("🚪 Ya no está en el servidor.")
        return "\n".join(lines)

    if member.joined_at is not None:
        lines.append(f"Entró {_ts(member.joined_at.timestamp(), 'F')}")
    roles = [role for role in reversed(member.roles) if not role.is_default()]
    lines.append(
        f"**Roles ({len(roles)})** " + (" ".join(role.mention for role in roles) if roles else "ninguno")
    )
    if member.nick:
        lines.append(f"Apodo: **{discord.utils.escape_markdown(member.nick)}** · usuario `{user.name}`")
    else:
        lines.append(f"Usuario `{user.name}`")
    if member.premium_since is not None:
        lines.append(f"💎 Booster desde {_ts(member.premium_since.timestamp(), 'D')}")
    if member.guild_avatar is not None:
        lines.append("🖼️ Usa un avatar propio en este servidor")
    if member.is_timed_out() and member.timed_out_until is not None:
        lines.append(f"⏳ Aislado hasta {_ts(member.timed_out_until.timestamp(), 'F')}")
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
        text = _voice_text(bot, profile)
    elif detail == "rrpp":
        text = _rrpp_text(profile)
    else:
        text = _server_text(profile)
    return [discord.ui.TextDisplay(text)]


# -- Vista -------------------------------------------------------------------


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
        newcomers = _newcomers(self.guild)
        profile = _load_profile(self.bot, self.guild, self.user) if self.user is not None else None

        if profile is None:
            container.add_item(
                discord.ui.TextDisplay(
                    "## 🔎 Inspector RRPP\n"
                    "Pulsa **👤 Buscar** para elegir a alguien, o recorre a los recién "
                    "llegados con ◀ ▶."
                )
            )
            if newcomers:
                newest = newcomers[0]
                assert newest.joined_at is not None
                summary = (
                    f"{_plural(len(newcomers), 'persona nueva', 'personas nuevas')} · la última, "
                    f"{newest.mention}, entró {_ts(newest.joined_at.timestamp(), 'R')}"
                )
            else:
                summary = "Nadie nuevo por ahora."
            container.add_item(discord.ui.TextDisplay(summary))
        else:
            container.add_item(
                discord.ui.Section(
                    discord.ui.TextDisplay(_header_text(profile)),
                    accessory=discord.ui.Thumbnail(profile.user.display_avatar.url),
                )
            )
            container.add_item(discord.ui.Separator())
            container.add_item(discord.ui.TextDisplay(_sheet_text(profile)))
            if self.detail in DETAILS:
                container.add_item(discord.ui.Separator())
                for item in await _detail_items(self.bot, profile, self.detail):
                    container.add_item(item)
            container.add_item(discord.ui.ActionRow(self._detail_select()))

        container.add_item(self._nav_row(newcomers, profile))
        container.add_item(
            discord.ui.TextDisplay(
                f"-# ◀ ▶ recorren a quienes han entrado en los últimos {NEWCOMER_DAYS} días, "
                f"del más nuevo al más antiguo · actualizado {_ts(discord.utils.utcnow().timestamp(), 'R')}"
            )
        )
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

    def _nav_row(
        self, newcomers: list[discord.Member], profile: Profile | None
    ) -> discord.ui.ActionRow[Any]:
        ids = [member.id for member in newcomers]
        index = ids.index(self.user.id) if self.user is not None and self.user.id in ids else None
        row: discord.ui.ActionRow[Any] = discord.ui.ActionRow()

        def _go(target: int) -> Any:
            async def _callback(interaction: discord.Interaction) -> None:
                # Se recalcula por si alguien ha entrado o salido mientras tanto.
                fresh = _newcomers(self.guild)
                if not fresh:
                    await self.show(interaction, self.user, self.detail)
                    return
                await self.show(interaction, fresh[max(0, min(target, len(fresh) - 1))], self.detail)

            return _callback

        previous = discord.ui.Button(emoji="◀️", style=discord.ButtonStyle.secondary)
        previous.disabled = index is None or index == 0
        previous.callback = _go((index or 0) - 1)
        row.add_item(previous)

        position = f"{index + 1}" if index is not None else "–"
        counter = discord.ui.Button(
            label=f"{position} / {len(newcomers)} nuevos",
            style=discord.ButtonStyle.secondary,
            disabled=True,
        )
        row.add_item(counter)

        following = discord.ui.Button(emoji="▶️", style=discord.ButtonStyle.secondary)
        following.disabled = not newcomers or (index is not None and index >= len(newcomers) - 1)
        following.callback = _go(0 if index is None else index + 1)
        row.add_item(following)

        if profile is not None and profile.presentation_ref is not None:
            channel_id, message_id = profile.presentation_ref
            row.add_item(
                discord.ui.Button(
                    label="Presentación",
                    emoji="📝",
                    url=_jump(self.guild.id, channel_id, message_id),
                )
            )

        search = discord.ui.Button(label="Buscar", emoji="👤", style=discord.ButtonStyle.primary)

        async def _search(interaction: discord.Interaction) -> None:
            await interaction.response.send_modal(InspectSearchModal(self))

        search.callback = _search
        row.add_item(search)
        return row

    async def show(
        self,
        interaction: discord.Interaction,
        user: discord.Member | discord.User | None,
        detail: str | None,
    ) -> None:
        await interaction.response.defer()
        view = await InspectView.create(self.bot, self.guild, self.author_id, user, detail)
        view.message = self.message or interaction.message
        self.stop()
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

        async with ctx.typing():
            view = await InspectView.create(ctx.bot, ctx.guild, ctx.author.id, user)
        view.message = await ctx.send(view=view, allowed_mentions=discord.AllowedMentions.none())

    @inspect.error
    async def _inspect_error(ctx: commands.Context, error: commands.CommandError) -> None:
        if isinstance(error, (commands.MemberNotFound, commands.UserNotFound, commands.BadUnionArgument)):
            await ctx.send("❌ No encuentro a ese usuario. Usa `.noah rrpp inspect` y pulsa 👤 Buscar.")
