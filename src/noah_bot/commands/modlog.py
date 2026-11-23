"""Moderación: marcar, borrar y guardar mensajes, y expediente de cada usuario.

Clic derecho en un mensaje (Aplicaciones):
- **Marcar mensaje**: lo guarda como infracción y lo deja donde está.
- **Eliminar y marcar**: guarda una copia (texto y adjuntos) y lo borra.
- **Guardar evidencia**: lo guarda sin contar como infracción.

Clic derecho en un usuario (Aplicaciones):
- **Ver expediente**: todo lo que hay guardado de esa persona.
- **Añadir nota**: texto, capturas y un enlace o momento de referencia.

Todo es solo para administradores. Cada caso se publica en el canal de logs con los
adjuntos resubidos, así que se conservan aunque el mensaje se borre. Al usuario no se
le avisa de nada.

`.noah mod ...` sirve para consultar y gestionar el expediente.
"""

import time
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from noah_bot.modules.bot_context import get_bot_context
from noah_bot.modules.help_ui import (
    HelpEntry,
    HelpModule,
    HelpSection,
    register_help,
    send_help,
)
from noah_bot.modules.modlog import (
    INFRACTION_KINDS,
    KIND_DELETE,
    KIND_EVIDENCE,
    KIND_FLAG,
    KIND_NOTE,
    KINDS,
    SEVERITIES,
    ModlogStore,
    kind_label,
    severity_label,
)

PAGE_SIZE = 6
NO_MENTIONS = discord.AllowedMentions.none()

KIND_COLORS = {
    KIND_FLAG: discord.Color.from_rgb(250, 119, 124),
    KIND_DELETE: discord.Color.from_rgb(237, 66, 69),
    KIND_EVIDENCE: discord.Color.from_rgb(88, 101, 242),
    KIND_NOTE: discord.Color.from_rgb(150, 155, 165),
}
SEVERITY_COLORS = {
    "leve": discord.Color.from_rgb(250, 196, 82),
    "media": discord.Color.from_rgb(240, 140, 60),
    "grave": discord.Color.from_rgb(237, 66, 69),
}
MODAL_TITLES = {
    KIND_FLAG: "Marcar mensaje",
    KIND_DELETE: "Eliminar y marcar",
    KIND_EVIDENCE: "Guardar evidencia",
}
CONFIG_HINT = "Configúralo con `.noah mod config #canal`."


HELP = register_help(
    HelpModule(
        key="mod",
        emoji="🛡️",
        name="Moderación",
        summary="Marcar y borrar mensajes, notas y expediente de cada usuario",
        description=(
            "Guarda infracciones, evidencias y notas de cada usuario. Las copias de los "
            "mensajes y adjuntos se publican en el canal de logs, así que se conservan aunque "
            "el mensaje se borre. Al usuario no se le avisa."
        ),
        staff=True,
        sections=(
            HelpSection(
                "Clic derecho en un mensaje → Aplicaciones",
                text=(
                    "**Marcar mensaje**: infracción, el mensaje se queda.\n"
                    "**Eliminar y marcar**: guarda copia y lo borra.\n"
                    "**Guardar evidencia**: lo guarda sin contar como infracción.\n"
                    "-# Abre un formulario con motivo y gravedad (opcionales)."
                ),
            ),
            HelpSection(
                "Clic derecho en un usuario → Aplicaciones",
                text=(
                    "**Ver expediente**: todo lo guardado de esa persona.\n"
                    "**Añadir nota**: texto, capturas y un enlace o momento."
                ),
            ),
            HelpSection(
                "Consultar",
                (
                    HelpEntry(".noah mod flags", "Todas las infracciones del servidor."),
                    HelpEntry(".noah mod flags @user [@user…]", "Infracciones de uno o varios usuarios."),
                    HelpEntry(".noah mod user @user", "Expediente completo: resumen, infracciones, evidencias y notas."),
                    HelpEntry(".noah mod notes @user", "Solo notas y evidencias de un usuario."),
                    HelpEntry(".noah mod case <nº>", "Detalle de un caso con su texto completo."),
                ),
            ),
            HelpSection(
                "Gestionar",
                (
                    HelpEntry(
                        ".noah mod note @user <texto>",
                        "Nota con capturas adjuntas. Si respondes a un mensaje, queda enlazado.",
                    ),
                    HelpEntry(".noah mod remove <nº>", "Retira un caso puesto por error."),
                    HelpEntry(".noah mod config [#canal]", "Canal de logs donde se guardan las copias."),
                ),
            ),
        ),
        note="Solo administradores",
    )
)


# ---------------------------------------------------------------------------
# Utilidades


def _is_admin(user: discord.abc.User | None) -> bool:
    return isinstance(user, discord.Member) and (
        user.guild_permissions.administrator or user.id == user.guild.owner_id
    )


def _store(bot: commands.Bot) -> ModlogStore:
    return get_bot_context(bot).modlog


def _clip(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _one_line(text: str, limit: int) -> str:
    flat = " ".join((text or "").split())
    return discord.utils.escape_markdown(_clip(flat, limit))


def _log_channel(guild: discord.Guild, store: ModlogStore) -> discord.TextChannel | discord.Thread | None:
    channel_id = store.get_log_channel(guild.id)
    if channel_id is None:
        return None
    channel = guild.get_channel_or_thread(channel_id)
    return channel if isinstance(channel, (discord.TextChannel, discord.Thread)) else None


def _log_channel_problem(guild: discord.Guild, store: ModlogStore) -> str | None:
    """Texto de error si el canal de logs no sirve; None si está bien."""
    channel = _log_channel(guild, store)
    if channel is None:
        return f"❌ No hay canal de logs de moderación. {CONFIG_HINT}"
    perms = channel.permissions_for(guild.me)
    missing = [
        name
        for name, ok in (
            ("Ver canal", perms.view_channel),
            ("Enviar mensajes", perms.send_messages_in_threads if isinstance(channel, discord.Thread) else perms.send_messages),
            ("Insertar enlaces", perms.embed_links),
            ("Adjuntar archivos", perms.attach_files),
        )
        if not ok
    ]
    if missing:
        return f"❌ Me faltan permisos en {channel.mention}: {', '.join(missing)}."
    return None


def _snapshot_message(message: discord.Message) -> dict[str, Any]:
    channel = message.channel
    reference = message.reference
    return {
        "id": message.id,
        "channel_id": channel.id,
        "channel_name": getattr(channel, "name", None),
        "content": message.content or "",
        "created_at": int(message.created_at.timestamp()),
        "edited_at": int(message.edited_at.timestamp()) if message.edited_at else None,
        "jump_url": message.jump_url,
        "reply_to": reference.message_id if reference and reference.message_id else None,
        "attachments": [
            {
                "filename": attachment.filename,
                "url": attachment.url,
                "size": attachment.size,
                "content_type": attachment.content_type,
            }
            for attachment in message.attachments
        ],
        "embeds": len(message.embeds),
        "stickers": [sticker.name for sticker in message.stickers],
    }


async def _copy_attachments(
    attachments: list[discord.Attachment], limit_bytes: int
) -> tuple[list[discord.File], list[str]]:
    """Descarga los adjuntos para resubirlos. Devuelve (archivos, nombres que no caben)."""
    files: list[discord.File] = []
    skipped: list[str] = []
    budget = max(0, limit_bytes - 256 * 1024)  # margen para el embed
    used = 0
    for attachment in attachments[:10]:
        if attachment.size > limit_bytes or used + attachment.size > budget:
            skipped.append(attachment.filename)
            continue
        try:
            files.append(await attachment.to_file(use_cached=True))
            used += attachment.size
        except (discord.HTTPException, discord.NotFound):
            try:
                files.append(await attachment.to_file())
                used += attachment.size
            except (discord.HTTPException, discord.NotFound):
                skipped.append(attachment.filename)
    skipped.extend(attachment.filename for attachment in attachments[10:])
    return files, skipped


def _user_line(user_id: int, name: str | None) -> str:
    return f"<@{user_id}> · `{name or '?'}` · `{user_id}`"


def build_case_embed(
    record: dict[str, Any], avatar_url: str | None = None, *, full: bool = False
) -> discord.Embed:
    kind = record.get("kind", "")
    severity = record.get("severity")
    color = SEVERITY_COLORS.get(severity) if kind in INFRACTION_KINDS and severity else None
    color = color or KIND_COLORS.get(kind, discord.Color.dark_grey())
    message = record.get("message") or {}

    title = f"{kind_label(kind)} · caso #{record.get('id')}"
    if record.get("removed"):
        title = f"~~{title}~~ · retirado"

    if kind == KIND_NOTE:
        body = record.get("note") or "*(sin texto)*"
    else:
        body = message.get("content") or "*(sin texto)*"
    body = _clip(body, 4000 if full else 3500)

    embed = discord.Embed(title=title, description=body, color=color)
    embed.set_author(
        name=f"{record.get('user_name') or '?'} ({record.get('user_id')})",
        icon_url=avatar_url or None,
    )
    embed.add_field(name="Usuario", value=f"<@{record.get('user_id')}>", inline=True)

    if message:
        embed.add_field(
            name="Canal",
            value=f"<#{message.get('channel_id')}>" if message.get("channel_id") else "—",
            inline=True,
        )
        sent = message.get("created_at")
        if sent:
            embed.add_field(name="Enviado", value=f"<t:{sent}:f>", inline=True)

    severity_text = severity_label(severity)
    if severity_text:
        embed.add_field(name="Gravedad", value=severity_text, inline=True)

    if kind != KIND_NOTE and record.get("reason"):
        embed.add_field(name="Motivo" if kind in INFRACTION_KINDS else "Nota", value=_clip(record["reason"], 1024), inline=False)
    if record.get("reference"):
        embed.add_field(name="Referencia", value=_clip(record["reference"], 1024), inline=False)

    if message:
        if kind == KIND_DELETE and record.get("deleted"):
            status = f"🗑️ Borrado · [enlace original]({message.get('jump_url')})"
        elif kind == KIND_DELETE:
            status = f"⚠️ No se pudo borrar · [ir al mensaje]({message.get('jump_url')})"
        else:
            status = f"[Ir al mensaje]({message.get('jump_url')})"
        extras = []
        if message.get("attachments"):
            extras.append(f"📷 {len(message['attachments'])} adjunto(s)")
        if message.get("embeds"):
            extras.append(f"{message['embeds']} embed(s)")
        if message.get("stickers"):
            extras.append("sticker: " + ", ".join(message["stickers"]))
        if message.get("edited_at"):
            extras.append("editado")
        if extras:
            status += " · " + " · ".join(extras)
        embed.add_field(name="Mensaje", value=status, inline=False)

    if record.get("skipped_attachments"):
        embed.add_field(
            name="Adjuntos sin copia (demasiado grandes)",
            value=_clip(", ".join(record["skipped_attachments"]), 1024),
            inline=False,
        )
    if full and record.get("log_url"):
        embed.add_field(name="Copia en logs", value=f"[Abrir]({record['log_url']})", inline=False)
    if record.get("removed"):
        embed.add_field(
            name="Retirado",
            value=f"por <@{record.get('removed_by')}> · <t:{record.get('removed_at', 0)}:f>",
            inline=False,
        )

    embed.add_field(
        name="Moderador",
        value=f"<@{record.get('moderator_id')}> · <t:{record.get('created_at', 0)}:f>",
        inline=False,
    )
    embed.set_footer(text=f"Caso #{record.get('id')} · {KINDS.get(kind, ('', kind))[1]}")
    return embed


async def _publish_case(
    bot: commands.Bot,
    guild: discord.Guild,
    record: dict[str, Any],
    avatar_url: str | None,
    attachments: list[discord.Attachment],
) -> tuple[dict[str, Any] | None, str | None]:
    """Copia el caso al canal de logs. Devuelve (caso actualizado, error)."""
    store = _store(bot)
    channel = _log_channel(guild, store)
    if channel is None:
        return None, f"❌ No hay canal de logs. {CONFIG_HINT}"

    files, skipped = await _copy_attachments(attachments, guild.filesize_limit)
    record = dict(record, skipped_attachments=skipped)
    try:
        log_message = await channel.send(
            embed=build_case_embed(record, avatar_url), files=files, allowed_mentions=NO_MENTIONS
        )
    except discord.HTTPException:
        # Puede fallar por tamaño total: lo intento sin archivos para no perder el caso.
        skipped = skipped + [file.filename for file in files]
        record["skipped_attachments"] = skipped
        try:
            log_message = await channel.send(
                embed=build_case_embed(record, avatar_url), allowed_mentions=NO_MENTIONS
            )
        except discord.HTTPException as exc:
            return None, f"❌ No pude publicar en {channel.mention}: {exc.text or exc}"

    updated = store.update_record(
        guild.id,
        int(record["id"]),
        skipped_attachments=skipped,
        log_channel_id=channel.id,
        log_message_id=log_message.id,
        log_url=log_message.jump_url,
        saved_attachments=[
            {"filename": attachment.filename, "url": attachment.url} for attachment in log_message.attachments
        ],
    )
    return updated, None


async def _refresh_log_card(bot: commands.Bot, guild: discord.Guild, record: dict[str, Any]) -> None:
    """Rehace la tarjeta del canal de logs (p. ej. tras borrar o retirar)."""
    channel = guild.get_channel_or_thread(int(record.get("log_channel_id") or 0))
    if not isinstance(channel, (discord.TextChannel, discord.Thread)) or not record.get("log_message_id"):
        return
    try:
        log_message = await channel.fetch_message(int(record["log_message_id"]))
        avatar = log_message.embeds[0].author.icon_url if log_message.embeds else None
        await log_message.edit(embed=build_case_embed(record, avatar))
    except discord.HTTPException:
        pass


def _record_line(record: dict[str, Any], *, show_user: bool) -> str:
    kind = record.get("kind", "")
    emoji = KINDS.get(kind, ("•", ""))[0]
    head = [f"{emoji} **#{record.get('id')}**", f"<t:{record.get('created_at', 0)}:d>"]
    severity = severity_label(record.get("severity"))
    if severity:
        head.append(severity)
    if show_user:
        head.append(f"<@{record.get('user_id')}>")

    message = record.get("message") or {}
    text = record.get("note") if kind == KIND_NOTE else message.get("content")
    snippet = _one_line(text or "", 110) or "*(sin texto)*"
    files = len(record.get("saved_attachments") or []) + len(record.get("skipped_attachments") or [])
    if files:
        snippet += f" · 📷 {files}"

    meta: list[str] = []
    if kind != KIND_NOTE and record.get("reason"):
        meta.append(_one_line(record["reason"], 80))
    if record.get("reference"):
        meta.append("ref: " + _one_line(record["reference"], 60))
    meta.append(f"por <@{record.get('moderator_id')}>")
    if record.get("log_url"):
        meta.append(f"[logs]({record['log_url']})")
    elif message.get("jump_url"):
        meta.append(f"[mensaje]({message['jump_url']})")

    return f"{' · '.join(head)}\n> {snippet}\n-# {' · '.join(meta)}"


def _summary_text(store: ModlogStore, guild_id: int, user_id: int) -> str:
    summary = store.summary(guild_id, user_id)
    counts = summary["counts"]
    severities = summary["severities"]
    lines = [
        (
            f"🚩 **{summary['infractions']}** infracciones "
            f"({counts.get(KIND_DELETE, 0)} borradas) · "
            f"📎 **{counts.get(KIND_EVIDENCE, 0)}** evidencias · "
            f"📝 **{counts.get(KIND_NOTE, 0)}** notas"
        )
    ]
    if any(severities.values()):
        lines.append(
            "Gravedad: "
            + " · ".join(f"{SEVERITIES[key][0]} {value}" for key, value in severities.items())
        )
    if summary["last_infraction_at"]:
        lines.append(f"Última infracción: <t:{summary['last_infraction_at']}:R>")
    return "\n".join(lines)


class CasePager(discord.ui.View):
    """Lista de casos con páginas ◀ ▶ (solo la mueve quien la pidió)."""

    def __init__(
        self,
        owner_id: int,
        title: str,
        header: str,
        records: list[dict[str, Any]],
        *,
        show_user: bool,
        color: discord.Color,
        thumbnail: str | None = None,
    ) -> None:
        super().__init__(timeout=900)
        self.owner_id = owner_id
        self.title = title
        self.header = header
        self.records = records
        self.show_user = show_user
        self.color = color
        self.thumbnail = thumbnail
        self.page = 0
        self.pages = max(1, -(-len(records) // PAGE_SIZE))
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        self.previous.disabled = self.page <= 0
        self.next.disabled = self.page >= self.pages - 1
        self.counter.label = f"{self.page + 1}/{self.pages}"
        if self.pages <= 1:
            self.clear_items()

    def build_embed(self) -> discord.Embed:
        shown = self.records[self.page * PAGE_SIZE : (self.page + 1) * PAGE_SIZE]
        parts = [self.header] if self.header else []
        if shown:
            parts.append("\n\n".join(_record_line(record, show_user=self.show_user) for record in shown))
        else:
            parts.append("*Nada guardado todavía.*")
        description = "\n\n".join(parts)
        embed = discord.Embed(title=self.title, description=_clip(description, 4096), color=self.color)
        if self.thumbnail:
            embed.set_thumbnail(url=self.thumbnail)
        embed.set_footer(text=f"{len(self.records)} caso(s) · .noah mod case <nº> para ver uno entero")
        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("❌ Esta lista no es tuya.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        _ = button
        self.page = max(0, self.page - 1)
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="1/1", style=discord.ButtonStyle.secondary, disabled=True)
    async def counter(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        _ = interaction, button

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        _ = button
        self.page = min(self.pages - 1, self.page + 1)
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)


def build_user_pager(
    bot: commands.Bot,
    guild: discord.Guild,
    owner_id: int,
    user: discord.abc.User,
    *,
    kinds: frozenset[str] | None = None,
    title: str | None = None,
) -> CasePager:
    store = _store(bot)
    records = store.records(guild.id, user_ids=[user.id], kinds=kinds)
    member = guild.get_member(user.id)
    header = [_user_line(user.id, user.name), _summary_text(store, guild.id, user.id)]
    if member and member.joined_at:
        header.append(f"-# En el servidor desde <t:{int(member.joined_at.timestamp())}:D>")
    elif member is None:
        header.append("-# Ya no está en el servidor")
    return CasePager(
        owner_id,
        title or f"Expediente · {getattr(user, 'display_name', user.name)}",
        "\n".join(header),
        records,
        show_user=False,
        color=discord.Color.from_rgb(180, 108, 255),
        thumbnail=user.display_avatar.url,
    )


# ---------------------------------------------------------------------------
# Formularios


SEVERITY_OPTIONS = [
    discord.SelectOption(label=label, value=key, emoji=emoji) for key, (emoji, label) in SEVERITIES.items()
]


class MessageCaseModal(discord.ui.Modal):
    def __init__(self, bot: commands.Bot, kind: str, message: discord.Message, previous: list[dict[str, Any]]) -> None:
        super().__init__(title=MODAL_TITLES[kind], timeout=900)
        self.bot = bot
        self.kind = kind
        self.message = message

        snippet = _one_line(message.content, 300) or "*(sin texto)*"
        if message.attachments:
            snippet += f" · 📷 {len(message.attachments)}"
        preview = f"**{discord.utils.escape_markdown(message.author.display_name)}** en <#{message.channel.id}>\n> {snippet}"
        if previous:
            ids = ", ".join(f"#{record['id']}" for record in previous[:5])
            preview += f"\n-# Este mensaje ya está en: {ids}"
        if kind == KIND_DELETE:
            preview += "\n-# Se guardará una copia y se borrará del canal."
        self.add_item(discord.ui.TextDisplay(preview))

        evidence = kind == KIND_EVIDENCE
        self.reason = discord.ui.TextInput(
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=1000,
            placeholder="Por qué se guarda (opcional)" if evidence else "Qué norma incumple (opcional)",
        )
        self.add_item(
            discord.ui.Label(text="Nota" if evidence else "Motivo", description="Opcional", component=self.reason)
        )

        self.severity: discord.ui.Select | None = None
        if not evidence:
            self.severity = discord.ui.Select(
                placeholder="Sin indicar", options=SEVERITY_OPTIONS, required=False, min_values=0, max_values=1
            )
            self.add_item(discord.ui.Label(text="Gravedad", description="Opcional", component=self.severity))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        severity = self.severity.values[0] if self.severity and self.severity.values else None
        await handle_message_case(
            self.bot, interaction, self.kind, self.message, self.reason.value.strip(), severity
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await _reply_error(interaction, f"❌ Algo falló al guardar el caso: {error}")


class NoteModal(discord.ui.Modal):
    def __init__(self, bot: commands.Bot, user: discord.abc.User) -> None:
        super().__init__(title="Añadir nota", timeout=900)
        self.bot = bot
        self.user = user
        self.add_item(
            discord.ui.TextDisplay(f"Nota sobre **{discord.utils.escape_markdown(user.display_name)}** (`{user.id}`)")
        )
        self.note = discord.ui.TextInput(style=discord.TextStyle.paragraph, max_length=1500, required=True)
        self.add_item(discord.ui.Label(text="Nota", component=self.note))
        self.reference = discord.ui.TextInput(
            required=False,
            max_length=300,
            placeholder="Enlace a un mensaje, o p. ej. «VC 24/11 ~22:30»",
        )
        self.add_item(
            discord.ui.Label(text="Referencia", description="Enlace o momento (opcional)", component=self.reference)
        )
        self.files = discord.ui.FileUpload(required=False, min_values=0, max_values=10)
        self.add_item(discord.ui.Label(text="Capturas", description="Hasta 10 (opcional)", component=self.files))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        record, error = await create_note(
            self.bot,
            interaction.guild,
            moderator=interaction.user,
            user=self.user,
            note=self.note.value.strip(),
            reference=self.reference.value.strip() or None,
            attachments=list(self.files.values),
        )
        await interaction.followup.send(error or _saved_text(self.bot, interaction.guild, record), ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await _reply_error(interaction, f"❌ Algo falló al guardar la nota: {error}")


async def _reply_error(interaction: discord.Interaction, text: str) -> None:
    try:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)
    except discord.HTTPException:
        pass


def _saved_text(bot: commands.Bot, guild: discord.Guild, record: dict[str, Any] | None) -> str:
    if record is None:
        return "❌ No se pudo guardar."
    summary = _store(bot).summary(guild.id, int(record["user_id"]))
    link = f" · [ver en logs]({record['log_url']})" if record.get("log_url") else ""
    lines = [f"{kind_label(record['kind'])} · caso **#{record['id']}** guardado{link}"]
    if record.get("kind") == KIND_DELETE and not record.get("deleted"):
        lines.append("⚠️ No pude borrar el mensaje (¿me faltan permisos?). La copia sí está guardada.")
    if record.get("skipped_attachments"):
        lines.append(f"⚠️ Sin copia por tamaño: {', '.join(record['skipped_attachments'])}")
    lines.append(
        f"-# <@{record['user_id']}> acumula {summary['infractions']} infracción(es) · "
        f"{summary['total']} caso(s) en total"
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Acciones


async def handle_message_case(
    bot: commands.Bot,
    interaction: discord.Interaction,
    kind: str,
    message: discord.Message,
    reason: str,
    severity: str | None,
) -> None:
    await interaction.response.defer(ephemeral=True, thinking=True)
    guild = interaction.guild
    store = _store(bot)
    author = message.author

    record = store.add_record(
        guild.id,
        {
            "kind": kind,
            "user_id": author.id,
            "user_name": author.name,
            "user_display": getattr(author, "display_name", author.name),
            "moderator_id": interaction.user.id,
            "moderator_name": interaction.user.name,
            "reason": reason or None,
            "severity": severity if kind in INFRACTION_KINDS else None,
            "message": _snapshot_message(message),
            "deleted": False,
        },
    )
    published, error = await _publish_case(bot, guild, record, author.display_avatar.url, message.attachments)
    if published is None:
        # Sin copia no se borra nada: el caso se descarta para no dejarlo a medias.
        store.remove_record(guild.id, int(record["id"]))
        await interaction.followup.send(error or "❌ No se pudo guardar el caso.", ephemeral=True)
        return
    record = published

    if kind == KIND_DELETE:
        deleted = True
        try:
            await message.delete()
        except discord.NotFound:
            pass  # ya no existía: la copia está hecha igual
        except discord.HTTPException:
            deleted = False
        if deleted:
            record = store.update_record(guild.id, int(record["id"]), deleted=True, deleted_at=int(time.time())) or record
        await _refresh_log_card(bot, guild, record)

    await interaction.followup.send(_saved_text(bot, guild, record), ephemeral=True)


async def create_note(
    bot: commands.Bot,
    guild: discord.Guild,
    *,
    moderator: discord.abc.User,
    user: discord.abc.User,
    note: str,
    reference: str | None,
    attachments: list[discord.Attachment],
    linked_message: discord.Message | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    store = _store(bot)
    problem = _log_channel_problem(guild, store)
    if problem:
        return None, problem
    payload: dict[str, Any] = {
        "kind": KIND_NOTE,
        "user_id": user.id,
        "user_name": user.name,
        "user_display": getattr(user, "display_name", user.name),
        "moderator_id": moderator.id,
        "moderator_name": moderator.name,
        "note": note,
        "reference": reference,
    }
    if linked_message is not None:
        payload["message"] = _snapshot_message(linked_message)
    record = store.add_record(guild.id, payload)
    files = attachments + (linked_message.attachments if linked_message else [])
    published, error = await _publish_case(bot, guild, record, user.display_avatar.url, files)
    if published is None:
        store.remove_record(guild.id, int(record["id"]))
        return None, error
    return published, None


async def _open_message_case(bot: commands.Bot, interaction: discord.Interaction, kind: str, message: discord.Message) -> None:
    guild = interaction.guild
    if guild is None or not _is_admin(interaction.user):
        await interaction.response.send_message("❌ Solo para administradores.", ephemeral=True)
        return
    store = _store(bot)
    problem = _log_channel_problem(guild, store)
    if problem:
        await interaction.response.send_message(problem, ephemeral=True)
        return
    if (
        kind == KIND_DELETE
        and message.author.id != guild.me.id
        and not message.channel.permissions_for(guild.me).manage_messages
    ):
        await interaction.response.send_message(
            f"❌ No tengo *Gestionar mensajes* en <#{message.channel.id}>, no podría borrarlo.",
            ephemeral=True,
        )
        return
    previous = store.records_for_message(guild.id, message.id)
    await interaction.response.send_modal(MessageCaseModal(bot, kind, message, previous))


# ---------------------------------------------------------------------------
# Registro


def _register_context_menus(bot: commands.Bot) -> None:
    if getattr(bot, "_noah_modlog_menus", False):
        return
    bot._noah_modlog_menus = True

    admin_only = discord.Permissions(administrator=True)

    async def flag_message(interaction: discord.Interaction, message: discord.Message) -> None:
        await _open_message_case(bot, interaction, KIND_FLAG, message)

    async def delete_message(interaction: discord.Interaction, message: discord.Message) -> None:
        await _open_message_case(bot, interaction, KIND_DELETE, message)

    async def save_evidence(interaction: discord.Interaction, message: discord.Message) -> None:
        await _open_message_case(bot, interaction, KIND_EVIDENCE, message)

    async def view_record(interaction: discord.Interaction, user: discord.User | discord.Member) -> None:
        if interaction.guild is None or not _is_admin(interaction.user):
            await interaction.response.send_message("❌ Solo para administradores.", ephemeral=True)
            return
        pager = build_user_pager(bot, interaction.guild, interaction.user.id, user)
        await interaction.response.send_message(
            embed=pager.build_embed(), view=pager, ephemeral=True, allowed_mentions=NO_MENTIONS
        )

    async def add_note(interaction: discord.Interaction, user: discord.User | discord.Member) -> None:
        guild = interaction.guild
        if guild is None or not _is_admin(interaction.user):
            await interaction.response.send_message("❌ Solo para administradores.", ephemeral=True)
            return
        problem = _log_channel_problem(guild, _store(bot))
        if problem:
            await interaction.response.send_message(problem, ephemeral=True)
            return
        await interaction.response.send_modal(NoteModal(bot, user))

    menus = (
        ("Marcar mensaje", flag_message),
        ("Eliminar y marcar", delete_message),
        ("Guardar evidencia", save_evidence),
        ("Ver expediente", view_record),
        ("Añadir nota", add_note),
    )
    for name, callback in menus:
        menu = app_commands.ContextMenu(name=name, callback=callback)
        menu.default_permissions = admin_only
        menu.guild_only = True
        menu.allowed_contexts = app_commands.AppCommandContext(guild=True, dm_channel=False, private_channel=False)
        bot.tree.add_command(menu)

    @bot.listen("on_ready")
    async def _sync_app_commands() -> None:
        if getattr(bot, "_noah_tree_synced", False):
            return
        bot._noah_tree_synced = True
        try:
            synced = await bot.tree.sync()
            print(f"[modlog] {len(synced)} comandos de aplicación sincronizados")
        except discord.HTTPException as exc:
            bot._noah_tree_synced = False
            print(f"[modlog] No se pudieron sincronizar los comandos de aplicación: {exc}")


def _parse_case_id(raw: str) -> int | None:
    raw = raw.strip().lstrip("#")
    return int(raw) if raw.isdigit() else None


class RemoveCaseView(discord.ui.View):
    def __init__(self, bot: commands.Bot, owner_id: int, guild: discord.Guild, record_id: int) -> None:
        super().__init__(timeout=120)
        self.bot = bot
        self.owner_id = owner_id
        self.guild = guild
        self.record_id = record_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("❌ No es tuyo.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Retirar caso", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        _ = button
        store = _store(self.bot)
        removed = store.remove_record(self.guild.id, self.record_id)
        if removed is None:
            await interaction.response.edit_message(content="❌ Ese caso ya no existe.", embed=None, view=None)
            return
        removed.update(removed=True, removed_by=interaction.user.id, removed_at=int(time.time()))
        await _refresh_log_card(self.bot, self.guild, removed)
        await interaction.response.edit_message(
            content=f"✅ Caso **#{self.record_id}** retirado del expediente. La tarjeta de logs queda tachada.",
            embed=None,
            view=None,
        )

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        _ = button
        await interaction.response.edit_message(content="Cancelado.", embed=None, view=None)


def register_modlog_commands(bot: commands.Bot, noah_group: commands.Group) -> None:
    _register_context_menus(bot)

    async def deny(ctx: commands.Context) -> bool:
        if ctx.guild is None or not _is_admin(ctx.author):
            await ctx.send("❌ Solo para administradores.")
            return True
        return False

    @noah_group.group(name="mod", invoke_without_command=True)
    async def mod(ctx: commands.Context) -> None:
        await send_help(ctx, "mod")

    @mod.command(name="help")
    async def mod_help(ctx: commands.Context) -> None:
        await send_help(ctx, "mod")

    @mod.command(name="config")
    async def mod_config(ctx: commands.Context, channel: discord.TextChannel | None = None) -> None:
        if await deny(ctx):
            return
        store = _store(ctx.bot)
        if channel is None:
            current = _log_channel(ctx.guild, store)
            problem = _log_channel_problem(ctx.guild, store) if current else None
            text = (
                f"Canal de logs de moderación: {current.mention}" + (f"\n{problem}" if problem else " ✅")
                if current
                else f"No hay canal de logs. {CONFIG_HINT}"
            )
            await ctx.send(text)
            return
        store.set_log_channel(ctx.guild.id, channel.id)
        problem = _log_channel_problem(ctx.guild, store)
        lines = [f"✅ Las copias de moderación irán a {channel.mention}."]
        if problem:
            lines.append(problem)
        if channel.permissions_for(ctx.guild.default_role).view_channel:
            lines.append("⚠️ Ese canal lo puede ver @everyone: debería ser solo para staff.")
        await ctx.send("\n".join(lines), allowed_mentions=NO_MENTIONS)

    @mod.command(name="flags")
    async def mod_flags(ctx: commands.Context, users: commands.Greedy[discord.User]) -> None:
        if await deny(ctx):
            return
        store = _store(ctx.bot)
        user_ids = [user.id for user in users] or None
        records = store.records(ctx.guild.id, user_ids=user_ids, kinds=INFRACTION_KINDS)
        if users:
            header = "\n".join(
                f"<@{user.id}> · {store.summary(ctx.guild.id, user.id)['infractions']} infracción(es)" for user in users
            )
            title = "Infracciones · " + ", ".join(getattr(user, "display_name", user.name) for user in users[:3])
            if len(users) > 3:
                title += f" y {len(users) - 3} más"
        else:
            people = len({record.get("user_id") for record in records})
            header = f"**{len(records)}** infracciones de **{people}** usuario(s)."
            title = "Infracciones del servidor"
        pager = CasePager(
            ctx.author.id,
            _clip(title, 256),
            header,
            records,
            show_user=True,
            color=KIND_COLORS[KIND_DELETE],
        )
        await ctx.send(embed=pager.build_embed(), view=pager, allowed_mentions=NO_MENTIONS)

    @mod.command(name="user", aliases=["expediente"])
    async def mod_user(ctx: commands.Context, user: discord.User) -> None:
        if await deny(ctx):
            return
        pager = build_user_pager(ctx.bot, ctx.guild, ctx.author.id, user)
        await ctx.send(embed=pager.build_embed(), view=pager, allowed_mentions=NO_MENTIONS)

    @mod.command(name="notes")
    async def mod_notes(ctx: commands.Context, user: discord.User) -> None:
        if await deny(ctx):
            return
        pager = build_user_pager(
            ctx.bot,
            ctx.guild,
            ctx.author.id,
            user,
            kinds=frozenset({KIND_NOTE, KIND_EVIDENCE}),
            title=f"Notas y evidencias · {getattr(user, 'display_name', user.name)}",
        )
        await ctx.send(embed=pager.build_embed(), view=pager, allowed_mentions=NO_MENTIONS)

    @mod.command(name="case")
    async def mod_case(ctx: commands.Context, case_id: str) -> None:
        if await deny(ctx):
            return
        record_id = _parse_case_id(case_id)
        record = _store(ctx.bot).get_record(ctx.guild.id, record_id) if record_id else None
        if record is None:
            await ctx.send("❌ No existe ese caso. Usa `.noah mod case <nº>`.")
            return
        user = ctx.bot.get_user(int(record["user_id"]))
        embed = build_case_embed(record, user.display_avatar.url if user else None, full=True)
        await ctx.send(embed=embed, allowed_mentions=NO_MENTIONS)

    @mod.command(name="note")
    async def mod_note(ctx: commands.Context, user: discord.User, *, text: str = "") -> None:
        if await deny(ctx):
            return
        linked: discord.Message | None = None
        reference = ctx.message.reference
        if reference and reference.message_id:
            linked = reference.resolved if isinstance(reference.resolved, discord.Message) else None
            if linked is None:
                try:
                    linked = await ctx.channel.fetch_message(reference.message_id)
                except discord.HTTPException:
                    linked = None
        if not text and not ctx.message.attachments and linked is None:
            await ctx.send("❌ Usa `.noah mod note @user <texto>` (puedes adjuntar capturas o responder a un mensaje).")
            return
        async with ctx.typing():
            record, error = await create_note(
                ctx.bot,
                ctx.guild,
                moderator=ctx.author,
                user=user,
                note=text,
                reference=linked.jump_url if linked else None,
                attachments=list(ctx.message.attachments),
                linked_message=linked,
            )
        await ctx.send(error or _saved_text(ctx.bot, ctx.guild, record), allowed_mentions=NO_MENTIONS)

    @mod.command(name="remove")
    async def mod_remove(ctx: commands.Context, case_id: str) -> None:
        if await deny(ctx):
            return
        record_id = _parse_case_id(case_id)
        record = _store(ctx.bot).get_record(ctx.guild.id, record_id) if record_id else None
        if record is None:
            await ctx.send("❌ No existe ese caso.")
            return
        user = ctx.bot.get_user(int(record["user_id"]))
        await ctx.send(
            "¿Retirar este caso del expediente?",
            embed=build_case_embed(record, user.display_avatar.url if user else None),
            view=RemoveCaseView(ctx.bot, ctx.author.id, ctx.guild, int(record["id"])),
            allowed_mentions=NO_MENTIONS,
        )
