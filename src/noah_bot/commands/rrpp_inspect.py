import asyncio
import io
import logging
import re
import time
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import discord
from discord.ext import commands
from discord.http import Route

from noah_bot.commands.presentations import find_presentation
from noah_bot.commands.rrpp import RRPP_COLOR, is_rrpp_admin
from noah_bot.modules.bot_context import get_bot_context
from noah_bot.modules.inspect_card import Badge, CardData, Stat, render_card
from noah_bot.modules.rrpp import CLAIM_ACCEPTED, RrppStore

_log = logging.getLogger(__name__)

INSPECT_TIMEOUT = 900
NEW_ACCOUNT_DAYS = 30
TEXT_LIMIT = 1800
PRESENTATION_LIMIT = 1500
SNIPPET_LIMIT = 160
CARD_NAME = "perfil.png"
AVATAR_CACHE_SIZE = 64
# Primeros mensajes: se piden de 5 en 5 a la búsqueda de Discord.
PAGE_SIZE = 5
SEARCH_MAX_OFFSET = 9975
SEARCH_ATTEMPTS = 3
# Respaldo si la búsqueda no está disponible: se lee el historial de cada canal.
HISTORY_SCAN_PER_CHANNEL = 500
HISTORY_SCAN_CONCURRENCY = 4
HISTORY_SCAN_TIMEOUT = 40
SCAN_CACHE_SECONDS = 600
CLOSE_DETAIL = "close"
VERIFIED_ROLE_PATTERN = re.compile(r"^\W*(verificad[oa]s?|verified)\W*$", re.IGNORECASE)
# Detalle: (emoji, nombre, descripción del desplegable).
DETAILS: dict[str, tuple[str, str, str]] = {
    "presentation": ("📝", "Presentación", "Texto completo e imagen"),
    "first": ("💬", "Primeros mensajes", "Desde el más antiguo, de 5 en 5"),
    "voice": ("🎧", "Voz", "Tiempo, estado y nivel de .noah vc"),
    "rrpp": ("🤝", "RRPP", "Quién le saludó, presentó y llevó al VC"),
    "server": ("🏷️", "Roles y servidor", "Verificación, roles, apodo y avisos"),
}
CLAIM_STEPS = (
    ("greeting", "👋", "Saludo"),
    ("presentation", "📝", "Presentación"),
    ("vc", "🎧", "VC"),
)

_avatar_cache: dict[str, bytes] = {}
_scan_cache: dict[tuple[int, int], tuple[float, list[dict[str, Any]], bool]] = {}


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


def _date(moment: datetime) -> str:
    return moment.astimezone().strftime("%d/%m/%Y")


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


def _snippet(content: str, attachments: int, stickers: int) -> str:
    text = " ".join(content.split())
    if not text:
        text = "📎 adjunto" if attachments else "🏷️ sticker" if stickers else "…"
    return text if len(text) <= SNIPPET_LIMIT else text[: SNIPPET_LIMIT - 1] + "…"


# -- Datos del usuario -------------------------------------------------------


@dataclass
class Verification:
    badge: str | None
    tone: str
    sentence: str


@dataclass
class Profile:
    guild: discord.Guild
    user: discord.Member | discord.User
    member: discord.Member | None
    voice: dict[str, Any] | None
    presented: bool | None
    presentation_ref: tuple[int, int] | None
    is_rrpp: bool
    claims: list[dict[str, Any]] = field(default_factory=list)
    help_at: float | None = None

    @property
    def joined_at(self) -> float | None:
        if self.member is None or self.member.joined_at is None:
            return None
        return self.member.joined_at.timestamp()


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
        voice=context.voice_manager.get_user_stats(user.id),
        presented=presented,
        presentation_ref=presentations.get_presentation_message(guild.id, user.id),
        is_rrpp=member is not None and _is_rrpp(member, context.rrpp),
        claims=context.rrpp.list_claims_about(guild.id, user.id),
        help_at=context.rrpp.help_requested_at(guild.id, user.id),
    )


def _verified_role(guild: discord.Guild) -> discord.Role | None:
    return next((role for role in guild.roles if VERIFIED_ROLE_PATTERN.match(role.name.strip())), None)


def _verification(profile: Profile) -> Verification:
    """Usa el rol «Verificado» del servidor si existe; si no, si ha aceptado las normas."""
    member = profile.member
    if member is None:
        return Verification(None, "", "🚪 Ya no está en el servidor.")
    if member.pending:
        return Verification(
            "Normas pendientes", "warn", "⏳ Todavía no ha aceptado las normas del servidor."
        )
    role = _verified_role(profile.guild)
    if role is not None:
        if role in member.roles:
            return Verification("Verificado", "ok", f"✅ Tiene el rol {role.mention}.")
        return Verification("Sin verificar", "warn", f"❌ No tiene el rol {role.mention}.")
    return Verification("Verificado", "ok", "✅ Ha aceptado las normas del servidor.")


# -- Tarjeta -----------------------------------------------------------------


async def _avatar_bytes(user: discord.abc.User) -> bytes | None:
    asset = user.display_avatar.with_static_format("png").with_size(256)
    cached = _avatar_cache.get(asset.url)
    if cached is not None:
        return cached
    try:
        data = await asset.read()
    except (discord.HTTPException, discord.NotFound, ValueError):
        return None
    if len(_avatar_cache) >= AVATAR_CACHE_SIZE:
        _avatar_cache.pop(next(iter(_avatar_cache)))
    _avatar_cache[asset.url] = data
    return data


def _card_data(profile: Profile, avatar: bytes | None) -> CardData:
    user = profile.user
    member = profile.member
    now = discord.utils.utcnow()

    badges: list[Badge] = []
    verification = _verification(profile)
    if verification.badge is not None:
        badges.append(Badge(verification.badge, verification.tone))
    if member is None:
        badges.append(Badge("Fuera del servidor", "bad"))
    else:
        if member.is_timed_out():
            badges.append(Badge("Aislado", "bad"))
        if profile.is_rrpp:
            badges.append(Badge("RRPP", "info"))
        if profile.voice and profile.voice.get("is_connected"):
            badges.append(Badge("En VC", "info"))
        if member.premium_since is not None:
            badges.append(Badge("Booster", "info"))
        if member.flags.did_rejoin:
            badges.append(Badge("Reingreso", "info"))
    if user.bot:
        badges.append(Badge("Bot", "info"))
    if (now - user.created_at).days < NEW_ACCOUNT_DAYS:
        badges.append(Badge("Cuenta nueva", "warn"))

    stats = [Stat("Cuenta", _span((now - user.created_at).total_seconds()), _date(user.created_at))]
    if member is not None and member.joined_at is not None:
        stats.append(
            Stat(
                "En el servidor",
                _span((now - member.joined_at).total_seconds()),
                f"desde {_date(member.joined_at)}",
            )
        )
    else:
        stats.append(Stat("En el servidor", "—", "ya no está"))

    if profile.presented is None:
        stats.append(Stat("Presentación", "—", "sin configurar"))
    elif not profile.presented:
        stats.append(Stat("Presentación", "No", "todavía no"))
    else:
        sub = ""
        joined = profile.joined_at
        if profile.presentation_ref is not None and joined is not None:
            presented_at = discord.utils.snowflake_time(profile.presentation_ref[1]).timestamp()
            if presented_at >= joined:
                sub = f"{_minutes(int(presented_at - joined) // 60)} tras entrar"
        stats.append(Stat("Presentación", "Sí", sub))

    voice = profile.voice
    if voice is None:
        stats.append(Stat("Voz", "0 min", "nunca ha entrado"))
    else:
        sessions = int(voice.get("sessions", 0))
        stats.append(
            Stat(
                "Voz",
                _minutes(int(voice.get("effective_total_minutes", 0))),
                _plural(sessions, "sesión", "sesiones"),
            )
        )

    return CardData(
        name=user.display_name,
        username=user.name,
        user_id=user.id,
        avatar=avatar,
        badges=badges,
        stats=stats,
    )


async def _card_file(profile: Profile) -> discord.File:
    data = _card_data(profile, await _avatar_bytes(profile.user))
    png = await asyncio.to_thread(render_card, data)
    return discord.File(io.BytesIO(png), filename=CARD_NAME)


def _card_gallery(name: str) -> discord.ui.MediaGallery[Any]:
    return discord.ui.MediaGallery(
        discord.MediaGalleryItem(f"attachment://{CARD_NAME}", description=f"Perfil de {name}")
    )


# -- Primeros mensajes -------------------------------------------------------


@dataclass
class FirstPage:
    entries: list[dict[str, Any]]
    offset: int
    total: int | None
    has_next: bool
    from_history: bool = False


def _raw_entry(raw: dict[str, Any]) -> dict[str, Any] | None:
    at = _iso_ts(raw.get("timestamp"))
    if at is None or raw.get("id") is None:
        return None
    return {
        "channel_id": str(raw.get("channel_id")),
        "message_id": str(raw.get("id")),
        "at": at,
        "text": _snippet(
            str(raw.get("content") or ""),
            len(raw.get("attachments") or []),
            len(raw.get("sticker_items") or []),
        ),
    }


def _message_entry(message: discord.Message) -> dict[str, Any]:
    return {
        "channel_id": str(message.channel.id),
        "message_id": str(message.id),
        "at": message.created_at.timestamp(),
        "text": _snippet(message.content, len(message.attachments), len(message.stickers)),
    }


async def _search_page(
    bot: commands.Bot, guild_id: int, user_id: int, offset: int
) -> FirstPage | None:
    """Pide a la búsqueda de Discord los mensajes del usuario, del más antiguo al más nuevo."""
    route = Route("GET", "/guilds/{guild_id}/messages/search", guild_id=guild_id)
    params = {
        "author_id": str(user_id),
        "sort_by": "timestamp",
        "sort_order": "asc",
        "limit": PAGE_SIZE,
        "offset": offset,
        "include_nsfw": "true",
    }
    for _ in range(SEARCH_ATTEMPTS):
        try:
            data = await bot.http.request(route, params=params)
        except discord.HTTPException as error:
            _log.info("La búsqueda de mensajes de Discord no está disponible: %s", error)
            return None
        if not isinstance(data, dict):
            return None

        groups = data.get("messages")
        if isinstance(groups, list):
            entries: list[dict[str, Any]] = []
            for group in groups:
                raw = group[0] if isinstance(group, list) and group else group
                entry = _raw_entry(raw) if isinstance(raw, dict) else None
                if entry is not None:
                    entries.append(entry)
            total = int(data.get("total_results") or len(entries))
            has_next = offset + PAGE_SIZE < min(total, SEARCH_MAX_OFFSET + PAGE_SIZE)
            return FirstPage(entries, offset, total, has_next)

        # 202: Discord todavía está indexando el servidor; avisa cuándo reintentar.
        with suppress(TypeError, ValueError):
            await asyncio.sleep(min(max(float(data.get("retry_after", 2)), 1), 5))
    return None


async def _scan_history(
    guild: discord.Guild, user_id: int, after: datetime, need: int
) -> list[dict[str, Any]]:
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
                        if len(found) >= need:
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
    return [_message_entry(message) for message in messages[:need]]


async def _history_page(
    guild: discord.Guild, user_id: int, after: datetime, offset: int
) -> FirstPage:
    """Respaldo: lee el historial de cada canal y guarda el resultado un rato."""
    need = offset + PAGE_SIZE + 1
    key = (guild.id, user_id)
    now = time.monotonic()
    for stale in [cache_key for cache_key, value in _scan_cache.items() if value[0] <= now]:
        del _scan_cache[stale]

    cached = _scan_cache.get(key)
    if cached is not None and (len(cached[1]) >= need or cached[2]):
        entries, complete = cached[1], cached[2]
    else:
        entries = await _scan_history(guild, user_id, after, need)
        complete = len(entries) < need
        _scan_cache[key] = (now + SCAN_CACHE_SECONDS, entries, complete)

    return FirstPage(
        entries=entries[offset : offset + PAGE_SIZE],
        offset=offset,
        total=len(entries) if complete else None,
        has_next=len(entries) > offset + PAGE_SIZE,
        from_history=True,
    )


async def _first_page(bot: commands.Bot, profile: Profile, offset: int) -> FirstPage:
    page = await _search_page(bot, profile.guild.id, profile.user.id, offset)
    if page is not None:
        return page
    joined = profile.member.joined_at if profile.member is not None else None
    after = (joined or profile.user.created_at) - timedelta(minutes=1)
    return await _history_page(profile.guild, profile.user.id, after, offset)


def _first_text(profile: Profile, page: FirstPage) -> str:
    title = "### 💬 Primeros mensajes"
    if not page.entries:
        if page.offset:
            return f"{title}\nNo hay más mensajes."
        return f"{title}\nNo he encontrado mensajes suyos en el servidor."

    blocks = [title]
    for number, entry in enumerate(page.entries, start=page.offset + 1):
        url = _jump(profile.guild.id, entry.get("channel_id"), entry.get("message_id"))
        blocks.append(
            f"**{number}.** {_ts(float(entry.get('at', 0)))} en <#{entry.get('channel_id')}>\n"
            f"{_quote(str(entry.get('text', '…')))}\n"
            f"[↗ Ir al mensaje]({url})"
        )
    if page.from_history:
        blocks.append("-# Buscados en el historial reciente de cada canal; puede faltar alguno.")
    return _clip("\n\n".join(blocks))


# -- Otros detalles ----------------------------------------------------------


async def _presentation_items(bot: commands.Bot, profile: Profile) -> list[discord.ui.Item[Any]]:
    title = "### 📝 Presentación"
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


def _voice_text(profile: Profile) -> str:
    """Los mismos datos que `.noah vc stats`, separados en bloques."""
    title = "### 🎧 Voz"
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
    lines = ["### 🤝 RRPP"]
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
    lines = ["### 🏷️ Roles y servidor", f"**Verificación:** {_verification(profile).sentence}"]
    lines.append(f"**Cuenta creada:** {_ts(user.created_at.timestamp(), 'F')}")
    if member is None:
        return "\n".join(lines)

    if member.joined_at is not None:
        lines.append(f"**Entró:** {_ts(member.joined_at.timestamp(), 'F')}")
    roles = [role for role in reversed(member.roles) if not role.is_default()]
    lines.append("")
    lines.append(f"**Roles ({len(roles)}):** " + (" ".join(role.mention for role in roles) if roles else "ninguno"))
    lines.append("")
    lines.append(f"**Usuario:** `{user.name}`")
    if member.nick:
        lines.append(f"**Apodo:** {discord.utils.escape_markdown(member.nick)}")
    if member.premium_since is not None:
        lines.append(f"**Booster** desde el {_ts(member.premium_since.timestamp(), 'D')}")
    if member.is_timed_out() and member.timed_out_until is not None:
        lines.append(f"**Aislado** hasta {_ts(member.timed_out_until.timestamp(), 'F')}")
    return _clip("\n".join(lines))


# -- Vista -------------------------------------------------------------------


def _loading_view(text: str, card_name: str | None = None) -> discord.ui.LayoutView:
    """Aviso de carga; si la persona no cambia se deja su tarjeta visible."""
    container = discord.ui.Container(accent_color=RRPP_COLOR)
    if card_name is not None:
        container.add_item(_card_gallery(card_name))
    container.add_item(discord.ui.TextDisplay(f"⏳ {text}"))
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(container)
    return view


def _loading_text(
    user: discord.abc.User | None, detail: str | None, offset: int
) -> str:
    if user is None:
        return "Cargando…"
    name = discord.utils.escape_markdown(user.display_name)
    if detail == "first" and offset:
        return f"Cargando los mensajes {offset + 1}–{offset + PAGE_SIZE} de **{name}**…"
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
        offset: int = 0,
    ) -> None:
        super().__init__(timeout=INSPECT_TIMEOUT)
        self.bot = bot
        self.guild = guild
        self.author_id = author_id
        self.user = user
        self.detail = detail if user is not None else None
        self.offset = offset if self.detail == "first" else 0
        self.files: list[discord.File] = []
        self.message: discord.Message | None = None

    @classmethod
    async def create(
        cls,
        bot: commands.Bot,
        guild: discord.Guild,
        author_id: int,
        user: discord.Member | discord.User | None = None,
        detail: str | None = None,
        offset: int = 0,
    ) -> "InspectView":
        view = cls(bot, guild, author_id, user, detail, offset)
        await view._render()
        return view

    async def _render(self) -> None:
        container = discord.ui.Container(accent_color=RRPP_COLOR)
        if self.user is None:
            container.add_item(
                discord.ui.TextDisplay(
                    "## 🔎 Inspector RRPP\nPulsa **👤 Buscar** para elegir a quién inspeccionar."
                )
            )
            container.add_item(self._button_row(None))
            self.add_item(container)
            return

        profile = _load_profile(self.bot, self.guild, self.user)
        self.files = [await _card_file(profile)]
        container.add_item(_card_gallery(profile.user.display_name))
        container.add_item(discord.ui.TextDisplay(f"-# Perfil: {profile.user.mention}"))

        if self.detail in DETAILS:
            container.add_item(discord.ui.Separator())
            for item in await self._detail_items(profile):
                container.add_item(item)

        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.ActionRow(self._detail_select()))
        container.add_item(self._button_row(profile))
        self.add_item(container)

    async def _detail_items(self, profile: Profile) -> list[discord.ui.Item[Any]]:
        if self.detail == "presentation":
            return await _presentation_items(self.bot, profile)
        if self.detail == "first":
            page = await _first_page(self.bot, profile, self.offset)
            items: list[discord.ui.Item[Any]] = [discord.ui.TextDisplay(_first_text(profile, page))]
            if page.offset or page.has_next:
                items.append(self._pager_row(page))
            return items
        if self.detail == "voice":
            return [discord.ui.TextDisplay(_voice_text(profile))]
        if self.detail == "rrpp":
            return [discord.ui.TextDisplay(_rrpp_text(profile))]
        return [discord.ui.TextDisplay(_server_text(profile))]

    def _pager_row(self, page: FirstPage) -> discord.ui.ActionRow[Any]:
        row: discord.ui.ActionRow[Any] = discord.ui.ActionRow()

        def _go(offset: int) -> Any:
            async def _callback(interaction: discord.Interaction) -> None:
                await self.show(interaction, self.user, "first", offset)

            return _callback

        previous = discord.ui.Button(emoji="◀️", style=discord.ButtonStyle.secondary)
        previous.disabled = page.offset == 0
        previous.callback = _go(max(0, page.offset - PAGE_SIZE))
        row.add_item(previous)

        shown = f"{page.offset + 1}–{page.offset + len(page.entries)}"
        label = f"{shown} de {page.total}" if page.total is not None else shown
        row.add_item(discord.ui.Button(label=label, style=discord.ButtonStyle.secondary, disabled=True))

        following = discord.ui.Button(emoji="▶️", style=discord.ButtonStyle.secondary)
        following.disabled = not page.has_next
        following.callback = _go(page.offset + PAGE_SIZE)
        row.add_item(following)
        return row

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
        offset: int = 0,
    ) -> None:
        self.stop()
        none = discord.AllowedMentions.none()
        same_user = self.user is not None and user is not None and user.id == self.user.id
        loading = _loading_view(
            _loading_text(user, detail, offset), self.user.display_name if same_user and self.user else None
        )
        if same_user:
            await interaction.response.edit_message(view=loading, allowed_mentions=none)
        else:
            await interaction.response.edit_message(view=loading, attachments=[], allowed_mentions=none)

        try:
            view = await InspectView.create(self.bot, self.guild, self.author_id, user, detail, offset)
        except Exception:
            # Que la ficha no se quede en "Cargando…": se vuelve a la ficha sin detalle.
            _log.exception("No se ha podido cargar el inspector RRPP")
            view = await InspectView.create(self.bot, self.guild, self.author_id, user)
            await interaction.followup.send(
                "❌ No he podido cargar ese detalle. Inténtalo otra vez.", ephemeral=True
            )
        view.message = self.message or interaction.message
        await interaction.edit_original_response(view=view, attachments=view.files, allowed_mentions=none)

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
        message = await ctx.send(view=_loading_view(_loading_text(user, None, 0)), allowed_mentions=none)
        view = await InspectView.create(ctx.bot, ctx.guild, ctx.author.id, user)
        view.message = message
        await message.edit(view=view, attachments=view.files, allowed_mentions=none)

    @inspect.error
    async def _inspect_error(ctx: commands.Context, error: commands.CommandError) -> None:
        if isinstance(error, (commands.MemberNotFound, commands.UserNotFound, commands.BadUnionArgument)):
            await ctx.send("❌ No encuentro a ese usuario. Usa `.noah rrpp inspect` y pulsa 👤 Buscar.")
