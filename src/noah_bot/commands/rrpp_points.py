from contextlib import suppress
from datetime import datetime, timezone
from typing import Any

import discord
from discord.ext import commands

from noah_bot.commands.presentations import build_presentation_embed, find_presentation
from noah_bot.commands.rrpp import RRPP_COLOR, clip_lines, is_rrpp_admin
from noah_bot.modules.bot_context import get_bot_context
from noah_bot.modules.rrpp import (
    CLAIM_ACCEPTED,
    CLAIM_POINTS,
    CLAIM_REJECTED,
    HELP_BONUS_POINTS,
    POINTS_PER_LEVEL,
    RrppStore,
    level_for_points,
)


FORM_TIMEOUT = 900
NOTE_LIMIT = 300
PREVIEW_LIMIT = 200
TOP_LIMIT = 20
ERROR_DELETE_AFTER = 8
# Tipo de claim: (nombre, emoji). Los puntos están en CLAIM_POINTS.
CLAIM_TYPES: dict[str, tuple[str, str]] = {
    "greeting": ("Saludo", "👋"),
    "presentation": ("Presentación", "📝"),
    "vc": ("Voice chat", "🎙️"),
}
MEDALS = ("🥇", "🥈", "🥉")


def _claim_label(claim_type: str, points: int | None = None) -> str:
    name, emoji = CLAIM_TYPES.get(claim_type, (claim_type, "❔"))
    if points is None:
        points = CLAIM_POINTS.get(claim_type, 0)
    return f"{emoji} {name} (+{points}p)"


def _rrpp_role(guild: discord.Guild, store: RrppStore) -> discord.Role | None:
    role_id = store.get_role_id(guild.id)
    return guild.get_role(role_id) if role_id is not None else None


def _is_rrpp(member: discord.abc.User, store: RrppStore) -> bool:
    if not isinstance(member, discord.Member):
        return False
    role = _rrpp_role(member.guild, store)
    return role is not None and role in member.roles


def _named_channel(
    guild: discord.Guild, store: RrppStore, name: str
) -> discord.TextChannel | None:
    channel_id = store.get_named_channel_id(guild.id, name)
    channel = guild.get_channel(channel_id) if channel_id is not None else None
    return channel if isinstance(channel, discord.TextChannel) else None


def _progress(points: int) -> str:
    filled = points % POINTS_PER_LEVEL
    bar = "▰" * filled + "▱" * (POINTS_PER_LEVEL - filled)
    return f"{bar} `{filled}/{POINTS_PER_LEVEL}` · faltan `{POINTS_PER_LEVEL - filled}p`"


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + "…"


def _jump_url(guild_id: int, channel_id: Any, message_id: Any) -> str:
    return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"


def _build_claim_embed(
    guild: discord.Guild, claim: dict[str, Any], points: int | None = None
) -> discord.Embed:
    rejected = claim.get("status") == CLAIM_REJECTED
    embed = discord.Embed(
        title=_claim_label(str(claim.get("type")), int(claim.get("points", 0))),
        color=discord.Color.red() if rejected else discord.Color.green(),
    )
    embed.add_field(name="RRPP", value=f"<@{claim.get('rrpp_id')}>", inline=True)
    embed.add_field(
        name="Usuario",
        value=f"<@{claim.get('target_id')}>\n`{claim.get('target_id')}`",
        inline=True,
    )
    status = (
        f"❌ Rechazado por <@{claim.get('reviewed_by')}>" if rejected else "✅ Aceptado"
    )
    embed.add_field(name="Estado", value=status, inline=True)

    if claim.get("source_message_id"):
        url = _jump_url(guild.id, claim.get("source_channel_id"), claim.get("source_message_id"))
        preview = claim.get("source_preview") or "*Sin texto.*"
        embed.add_field(name="Mensaje", value=f"[Ir al mensaje]({url})\n> {preview}", inline=False)
    if claim.get("vc_channel_id"):
        embed.add_field(
            name="En VC", value=f"<#{claim.get('vc_channel_id')}> al reclamar", inline=False
        )
    if claim.get("type") == "presentation":
        url = claim.get("presentation_url")
        embed.add_field(
            name="Presentación",
            value=f"[Ir a la presentación]({url})" if url else "No encuentro su mensaje.",
            inline=False,
        )
    if claim.get("help_bonus"):
        embed.add_field(
            name="🆘 Pidió ayuda",
            value=f"Reaccionó a una cita RRPP: +{HELP_BONUS_POINTS}p extra.",
            inline=False,
        )
    if claim.get("note"):
        embed.add_field(name="Nota", value=str(claim.get("note")), inline=False)

    footer = f"Claim #{claim.get('id')}"
    if points is not None:
        footer += f" · Total: {points}p · Nivel {level_for_points(points)}"
    embed.set_footer(text=footer)
    with suppress(TypeError, ValueError, OverflowError, OSError):
        embed.timestamp = datetime.fromtimestamp(float(claim.get("created_at", 0)), tz=timezone.utc)
    return embed


class RrppReviewButton(
    discord.ui.DynamicItem[discord.ui.Button], template=r"rrpp:claim:(?P<claim_id>\d+)"
):
    """Botón de los logs para rechazar o restaurar un claim. Sobrevive a reinicios."""

    def __init__(self, claim_id: int, rejected: bool) -> None:
        super().__init__(
            discord.ui.Button(
                label="Restaurar" if rejected else "Rechazar",
                emoji="↩️" if rejected else "✖️",
                style=discord.ButtonStyle.success if rejected else discord.ButtonStyle.danger,
                custom_id=f"rrpp:claim:{claim_id}",
            )
        )
        self.claim_id = claim_id

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: Any,
    ) -> "RrppReviewButton":
        _ = item
        claim_id = int(match["claim_id"])
        claim = None
        if interaction.guild_id is not None:
            claim = get_bot_context(interaction.client).rrpp.get_claim(
                interaction.guild_id, claim_id
            )
        return cls(claim_id, claim is not None and claim.get("status") == CLAIM_REJECTED)

    async def callback(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None or not is_rrpp_admin(interaction.user):
            await interaction.response.send_message(
                "❌ Solo los administradores pueden revisar claims.", ephemeral=True
            )
            return

        bot = interaction.client
        store = get_bot_context(bot).rrpp
        claim = store.get_claim(guild.id, self.claim_id)
        if claim is None:
            await interaction.response.send_message(
                "❌ Este claim ya no existe.", ephemeral=True
            )
            return

        rejected = claim.get("status") != CLAIM_REJECTED
        store.update_claim(
            guild.id,
            self.claim_id,
            status=CLAIM_REJECTED if rejected else CLAIM_ACCEPTED,
            reviewed_by=str(interaction.user.id),
        )

        rrpp_id = int(claim.get("rrpp_id", 0))
        points = store.get_points(guild.id, rrpp_id)
        embeds = [_build_claim_embed(guild, claim, points)]
        if interaction.message is not None:
            embeds.extend(interaction.message.embeds[1:])
        await interaction.response.edit_message(
            embeds=embeds, view=_build_review_view(self.claim_id, rejected)
        )

        await _refresh_form_message(guild, claim, points)
        if not rejected:
            await _announce_level(guild, store, rrpp_id)


def _build_review_view(claim_id: int, rejected: bool) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(RrppReviewButton(claim_id, rejected))
    return view


async def _refresh_form_message(
    guild: discord.Guild, claim: dict[str, Any], points: int
) -> None:
    """Actualiza la confirmación del canal de claims para que el RRPP vea el estado."""
    channel = guild.get_channel(int(claim.get("form_channel_id") or 0))
    if not isinstance(channel, discord.TextChannel) or not claim.get("form_message_id"):
        return
    with suppress(discord.HTTPException):
        message = await channel.fetch_message(int(claim["form_message_id"]))
        await message.edit(embed=_build_claim_embed(guild, claim, points))


async def _announce_level(guild: discord.Guild, store: RrppStore, rrpp_id: int) -> None:
    points = store.get_points(guild.id, rrpp_id)
    level = level_for_points(points)
    if level <= 0 or not store.raise_announced_level(guild.id, rrpp_id, level):
        return

    channel = _named_channel(guild, store, "levels")
    if channel is None:
        return

    member = guild.get_member(rrpp_id)
    embed = discord.Embed(
        title=f"🎉 ¡Nivel {level}!",
        description=f"<@{rrpp_id}> sube a **nivel {level}** con `{points}` puntos RRPP.",
        color=RRPP_COLOR,
    )
    if member is not None:
        embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="Siguiente nivel", value=_progress(points), inline=False)
    with suppress(discord.HTTPException):
        await channel.send(
            f"<@{rrpp_id}>",
            embed=embed,
            allowed_mentions=discord.AllowedMentions(
                everyone=False, roles=False, users=[discord.Object(rrpp_id)]
            ),
        )


async def _send_claim_log(
    guild: discord.Guild,
    store: RrppStore,
    claim: dict[str, Any],
    target: discord.Member,
    presentation: discord.Message | None,
    points: int,
) -> None:
    channel = _named_channel(guild, store, "logs")
    if channel is None:
        return

    embeds = [_build_claim_embed(guild, claim, points)]
    if presentation is not None:
        embeds.append(build_presentation_embed(target, presentation))
    with suppress(discord.HTTPException):
        message = await channel.send(
            embeds=embeds,
            view=_build_review_view(int(claim["id"]), rejected=False),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        store.update_claim(
            guild.id,
            int(claim["id"]),
            log_channel_id=str(channel.id),
            log_message_id=str(message.id),
        )


class RrppNoteModal(discord.ui.Modal, title="Enviar claim RRPP"):
    def __init__(self, form: "RrppClaimView") -> None:
        super().__init__()
        self.form = form
        self.note_input = discord.ui.TextInput(
            label="Nota para el admin (opcional)",
            style=discord.TextStyle.paragraph,
            placeholder="Contexto que ayude a revisar el claim.",
            required=False,
            max_length=NOTE_LIMIT,
        )
        self.add_item(self.note_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        error = await self.form.submit(self.note_input.value.strip())
        if error is not None:
            await interaction.followup.send(error, ephemeral=True)


class RrppClaimView(discord.ui.View):
    """Formulario de `.nrc` en el canal de claims. Solo lo puede usar su RRPP."""

    def __init__(
        self,
        bot: commands.Bot,
        rrpp: discord.Member,
        source: discord.Message | None,
    ) -> None:
        super().__init__(timeout=FORM_TIMEOUT)
        self.bot = bot
        self.guild = rrpp.guild
        self.rrpp = rrpp
        self.source = source
        self.store = get_bot_context(bot).rrpp
        self.message: discord.Message | None = None

        options = [
            discord.SelectOption(
                label=f"{name} (+{CLAIM_POINTS[key]}p)",
                emoji=emoji,
                value=key,
            )
            for key, (name, emoji) in CLAIM_TYPES.items()
            # Un saludo siempre tiene que venir de responder a tu propio mensaje.
            if key != "greeting" or source is not None
        ]
        self.type_select = discord.ui.Select(
            placeholder="Tipo de claim", min_values=1, max_values=1, options=options, row=0
        )
        self.type_select.callback = self._acknowledge
        self.add_item(self.type_select)

        self.user_select = discord.ui.UserSelect(
            placeholder="Usuario", min_values=1, max_values=1, row=1
        )
        self.user_select.callback = self._acknowledge
        self.add_item(self.user_select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.rrpp.id:
            await interaction.response.send_message(
                "Este formulario no es tuyo. Usa `.nrc` para reclamar tus puntos.",
                ephemeral=True,
            )
            return False
        return True

    async def _acknowledge(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()

    @discord.ui.button(label="Enviar", emoji="📨", style=discord.ButtonStyle.success, row=2)
    async def send(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        _ = button
        if not self.type_select.values or not self.user_select.values:
            await interaction.response.send_message(
                "❌ Elige el tipo de claim y el usuario antes de enviar.", ephemeral=True
            )
            return
        await interaction.response.send_modal(RrppNoteModal(self))

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.secondary, row=2)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        _ = button
        self.stop()
        await interaction.response.edit_message(
            content=None,
            embed=discord.Embed(description="Claim cancelado.", color=RRPP_COLOR),
            view=None,
        )

    async def _validate(
        self, claim_type: str, target: discord.Member
    ) -> tuple[str | None, dict[str, Any], discord.Message | None]:
        """Devuelve (error, detalles del claim, presentación encontrada)."""
        details: dict[str, Any] = {}
        presentation: discord.Message | None = None

        if claim_type == "greeting":
            if self.source is None:
                return "❌ Para un saludo usa `.nrc` respondiendo a tu propio mensaje.", details, None
            if self.store.is_source_used(self.guild.id, self.source.id):
                return "❌ Ese mensaje ya se ha usado en otro claim.", details, None

        elif claim_type == "presentation":
            presentations = get_bot_context(self.bot).presentations
            if presentations.get_channel_id(self.guild.id) is None:
                return "❌ Las presentaciones no están configuradas.", details, None
            if not presentations.has_completed(self.guild.id, target.id):
                return f"❌ {target.mention} todavía no se ha presentado.", details, None
            presentation = await find_presentation(presentations, self.guild, target.id)
            if presentation is not None:
                details["presentation_url"] = presentation.jump_url

        elif claim_type == "vc":
            if target.voice is None or target.voice.channel is None:
                return (
                    f"❌ {target.mention} no está ahora mismo en un canal de voz. "
                    "Reclama mientras siga dentro."
                ), details, None
            details["vc_channel_id"] = str(target.voice.channel.id)

        return None, details, presentation

    async def submit(self, note: str) -> str | None:
        """Crea el claim. Devuelve un mensaje de error o None si ha ido bien."""
        claim_type = self.type_select.values[0]
        target = self.guild.get_member(self.user_select.values[0].id)
        if target is None:
            return "❌ Ese usuario no está en el servidor."
        if target.bot:
            return "❌ No puedes reclamar puntos por un bot."
        if target.id == self.rrpp.id:
            return "❌ No puedes reclamar puntos por ti mismo."

        error, details, presentation = await self._validate(claim_type, target)
        if error is not None:
            return error

        # Se comprueba al final porque la búsqueda de la presentación puede tardar.
        previous = self.store.find_claim(self.guild.id, self.rrpp.id, target.id, claim_type)
        if previous is not None:
            return (
                f"❌ Ya reclamaste {_claim_label(claim_type)} por {target.mention} "
                f"(claim #{previous.get('id')})."
            )

        if self.source is not None:
            details["source_channel_id"] = str(self.source.channel.id)
            details["source_message_id"] = str(self.source.id)
            details["source_preview"] = _clip(self.source.content, PREVIEW_LIMIT)
        if note:
            details["note"] = note
        if self.message is not None:
            details["form_channel_id"] = str(self.message.channel.id)
            details["form_message_id"] = str(self.message.id)

        claim = self.store.add_claim(
            self.guild.id,
            self.rrpp.id,
            target.id,
            claim_type,
            help_bonus=self.store.can_get_help_bonus(self.guild.id, self.rrpp.id, target.id),
            **details,
        )
        self.stop()

        points = self.store.get_points(self.guild.id, self.rrpp.id)
        if self.message is not None:
            with suppress(discord.HTTPException):
                await self.message.edit(
                    content=None,
                    embed=_build_claim_embed(self.guild, claim, points),
                    view=None,
                )

        await _send_claim_log(self.guild, self.store, claim, target, presentation, points)
        await _announce_level(self.guild, self.store, self.rrpp.id)
        return None

    async def on_timeout(self) -> None:
        if self.message is not None:
            with suppress(discord.HTTPException):
                await self.message.edit(
                    content=None,
                    embed=discord.Embed(
                        description="Formulario caducado. Usa `.nrc` de nuevo.",
                        color=RRPP_COLOR,
                    ),
                    view=None,
                )


def _build_form_embed(rrpp: discord.Member, source: discord.Message | None) -> discord.Embed:
    embed = discord.Embed(
        title="Nuevo claim RRPP",
        description=(
            "Elige el tipo y el usuario y pulsa **Enviar**. "
            "Podrás añadir una nota para el admin."
        ),
        color=RRPP_COLOR,
    )
    embed.set_author(name=rrpp.display_name, icon_url=rrpp.display_avatar.url)
    if source is not None:
        preview = _clip(source.content, PREVIEW_LIMIT) or "*Sin texto.*"
        embed.add_field(
            name="Tu mensaje",
            value=f"[Ir al mensaje]({source.jump_url})\n> {preview}",
            inline=False,
        )
    else:
        embed.add_field(
            name="Sin mensaje",
            value="Solo puedes reclamar presentación o VC. Para un saludo responde a tu mensaje.",
            inline=False,
        )
    embed.add_field(
        name="Requisitos",
        value=(
            f"{_claim_label('greeting')}: responde con `.nrc` a tu propio saludo.\n"
            f"{_claim_label('presentation')}: el usuario ya está presentado.\n"
            f"{_claim_label('vc')}: el usuario está ahora mismo en VC.\n"
            f"🆘 +{HELP_BONUS_POINTS}p extra (una vez por usuario) si pidió ayuda en una cita RRPP."
        ),
        inline=False,
    )
    embed.set_footer(text=f"El formulario caduca en {FORM_TIMEOUT // 60} minutos.")
    return embed


def _ranking(guild: discord.Guild, store: RrppStore) -> list[tuple[int, int]]:
    """RRPP actuales y antiguos con puntos, ordenados por puntos."""
    points = store.get_points_by_rrpp(guild.id)
    role = _rrpp_role(guild, store)
    if role is not None:
        for member in role.members:
            points.setdefault(member.id, 0)

    def _name(user_id: int) -> str:
        member = guild.get_member(user_id)
        return member.display_name.casefold() if member is not None else ""

    return sorted(points.items(), key=lambda item: (-item[1], _name(item[0])))


def _build_summary_embed(
    guild: discord.Guild, store: RrppStore, member: discord.Member
) -> discord.Embed:
    points = store.get_points(guild.id, member.id)
    claims = store.list_claims(guild.id, member.id)
    ranking = _ranking(guild, store)
    position = next(
        (index for index, (user_id, _) in enumerate(ranking, start=1) if user_id == member.id),
        None,
    )

    embed = discord.Embed(title="Resumen RRPP", color=RRPP_COLOR)
    embed.set_author(name=member.display_name, icon_url=member.display_avatar.url)
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="Nivel", value=f"`{level_for_points(points)}`", inline=True)
    embed.add_field(name="Puntos", value=f"`{points}`", inline=True)
    embed.add_field(
        name="Posición",
        value=f"`#{position}` de {len(ranking)}" if position is not None else "-",
        inline=True,
    )
    embed.add_field(name="Siguiente nivel", value=_progress(points), inline=False)

    breakdown: list[str] = []
    for claim_type in CLAIM_TYPES:
        accepted = [
            claim
            for claim in claims
            if claim.get("type") == claim_type and claim.get("status") == CLAIM_ACCEPTED
        ]
        gained = sum(int(claim.get("points", 0)) for claim in accepted)
        breakdown.append(f"{_claim_label(claim_type)}: `{len(accepted)}` · `{gained}p`")
    rejected = sum(1 for claim in claims if claim.get("status") == CLAIM_REJECTED)
    if rejected:
        breakdown.append(f"❌ Rechazados: `{rejected}`")
    embed.add_field(name="Claims", value="\n".join(breakdown), inline=False)

    recent = [
        f"`#{claim.get('id')}` {CLAIM_TYPES.get(str(claim.get('type')), ('', '❔'))[1]} "
        f"<@{claim.get('target_id')}> · "
        f"{'❌' if claim.get('status') == CLAIM_REJECTED else '✅'} · "
        f"<t:{int(claim.get('created_at', 0))}:R>"
        for claim in claims[:5]
    ]
    embed.add_field(
        name="Últimos claims",
        value=clip_lines(recent, "Ninguno todavía. Usa `.nrc`."),
        inline=False,
    )
    return embed


def _build_top_embed(guild: discord.Guild, store: RrppStore) -> discord.Embed:
    lines: list[str] = []
    for index, (user_id, points) in enumerate(_ranking(guild, store)[:TOP_LIMIT], start=1):
        prefix = MEDALS[index - 1] if index <= len(MEDALS) else f"`#{index}`"
        lines.append(
            f"{prefix} <@{user_id}> · Nivel `{level_for_points(points)}` · `{points}p`"
        )
    return discord.Embed(
        title="Top RRPP",
        description=clip_lines(lines, "Todavía no hay RRPPs."),
        color=RRPP_COLOR,
    )


def _build_guide_embed() -> discord.Embed:
    embed = discord.Embed(
        title="Guía RRPP",
        description=(
            f"Ganas puntos ayudando a que la gente se integre. Cada `{POINTS_PER_LEVEL}` "
            "puntos subes un nivel. Los niveles RRPP son independientes de los niveles de VC."
        ),
        color=RRPP_COLOR,
    )
    embed.add_field(
        name="Qué puntúa",
        value=(
            f"{_claim_label('greeting')}: saludas a alguien por primera vez.\n"
            f"{_claim_label('presentation')}: alguien se presenta gracias a ti.\n"
            f"{_claim_label('vc')}: alguien se mete a VC gracias a ti.\n"
            f"🆘 **+{HELP_BONUS_POINTS}p extra**, una sola vez por usuario, si ese usuario "
            "había pedido ayuda reaccionando a una cita RRPP."
        ),
        inline=False,
    )
    embed.add_field(
        name="Cómo reclamar",
        value=(
            "1. Escribe `.nrc` (o `.noah rrpp claim`). Tu comando se borra al instante.\n"
            "2. Te llega un formulario al canal de claims RRPP.\n"
            "3. Elige el tipo y el usuario, pulsa **Enviar** y añade una nota si quieres."
        ),
        inline=False,
    )
    embed.add_field(
        name="Reglas",
        value=(
            "• **Nunca etiquetes al usuario.** Para un saludo, usa `.nrc` respondiendo a "
            "**tu propio** mensaje de bienvenida.\n"
            "• Presentación: el usuario tiene que estar ya presentado.\n"
            "• VC: reclama mientras el usuario sigue en el canal de voz.\n"
            "• Solo un claim de cada tipo por usuario. Otros RRPP también pueden reclamar "
            "por el mismo usuario.\n"
            "• Los claims se aceptan al momento, pero un admin los revisa y puede "
            "rechazarlos."
        ),
        inline=False,
    )
    embed.add_field(
        name="Comandos",
        value=(
            "`.noah rrpp summary` · tu nivel y tus claims\n"
            "`.noah rrpp top` · ranking de RRPP\n"
            "`.noah rrpp guide` · esta guía"
        ),
        inline=False,
    )
    return embed


def register_rrpp_points_commands(bot: commands.Bot, rrpp_group: commands.Group) -> None:
    bot.add_dynamic_items(RrppReviewButton)

    async def _check_rrpp(ctx: commands.Context) -> bool:
        if ctx.guild is None:
            await ctx.send("❌ Este comando solo funciona dentro de un servidor.")
            return False

        store = get_bot_context(ctx.bot).rrpp
        if not (_is_rrpp(ctx.author, store) or is_rrpp_admin(ctx.author)):
            await ctx.send("❌ Solo los RRPP pueden usar este comando.")
            return False
        return True

    async def _start_claim(ctx: commands.Context) -> None:
        # Se borra el comando al instante para no dejar rastro en el chat.
        reference = ctx.message.reference
        with suppress(discord.HTTPException):
            await ctx.message.delete()

        if ctx.guild is None or not isinstance(ctx.author, discord.Member):
            return

        store = get_bot_context(ctx.bot).rrpp
        if not _is_rrpp(ctx.author, store):
            await ctx.send(
                "❌ Solo los RRPP pueden reclamar puntos.", delete_after=ERROR_DELETE_AFTER
            )
            return

        spam = _named_channel(ctx.guild, store, "spam")
        if spam is None:
            await ctx.send(
                "❌ Falta el canal de claims RRPP. Un admin debe configurarlo con "
                "`.noah rrpp config`.",
                delete_after=ERROR_DELETE_AFTER,
            )
            return

        mention = discord.AllowedMentions(everyone=False, roles=False, users=[ctx.author])
        source: discord.Message | None = None
        if reference is not None and reference.message_id is not None:
            if isinstance(reference.resolved, discord.Message):
                source = reference.resolved
            else:
                with suppress(discord.HTTPException):
                    source = await ctx.channel.fetch_message(reference.message_id)
            if source is None:
                await spam.send(
                    f"{ctx.author.mention} ❌ No encuentro el mensaje al que respondiste.",
                    allowed_mentions=mention,
                )
                return
            if source.author.id != ctx.author.id:
                await spam.send(
                    f"{ctx.author.mention} ❌ Usa `.nrc` respondiendo a **tu propio** mensaje, "
                    "nunca al del usuario.",
                    allowed_mentions=mention,
                )
                return

        view = RrppClaimView(ctx.bot, ctx.author, source)
        view.message = await spam.send(
            ctx.author.mention,
            embed=_build_form_embed(ctx.author, source),
            view=view,
            allowed_mentions=mention,
        )

    @rrpp_group.command(name="claim")
    async def rrpp_claim(ctx: commands.Context) -> None:
        await _start_claim(ctx)

    @bot.command(name="nrc")
    async def nrc(ctx: commands.Context) -> None:
        await _start_claim(ctx)

    @rrpp_group.command()
    async def summary(ctx: commands.Context, member: discord.Member | None = None) -> None:
        if not await _check_rrpp(ctx):
            return
        store = get_bot_context(ctx.bot).rrpp
        target = member or ctx.author
        await ctx.send(embed=_build_summary_embed(ctx.guild, store, target))

    @summary.error
    async def _summary_error(ctx: commands.Context, error: commands.CommandError) -> None:
        if isinstance(error, commands.MemberNotFound):
            await ctx.send(f"❌ {error}")

    @rrpp_group.command()
    async def top(ctx: commands.Context) -> None:
        if not await _check_rrpp(ctx):
            return
        store = get_bot_context(ctx.bot).rrpp
        await ctx.send(embed=_build_top_embed(ctx.guild, store))

    @rrpp_group.command()
    async def guide(ctx: commands.Context) -> None:
        if not await _check_rrpp(ctx):
            return
        await ctx.send(embed=_build_guide_embed())
