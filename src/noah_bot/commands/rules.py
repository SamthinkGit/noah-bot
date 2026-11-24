"""`.noah ruleboard`: panel de normas del servidor editable desde Discord.

El panel publicado es un contenedor de componentes V2: cabecera con la imagen de Noah,
normas con iconos de línea del color del panel, soporte con botón a tickets,
incumplimiento y la firma con el logo del server en pequeño. Los iconos y el logo son
emojis de la aplicación (ver `ruleboard_emojis`).

Todo se gestiona desde `.noah ruleboard config`, y cada cambio edita en sitio el
mensaje publicado.
"""

import logging
import re
import unicodedata
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands

from noah_bot.modules.bot_context import get_bot_context
from noah_bot.modules.help_ui import (
    HelpEntry,
    HelpModule,
    HelpSection,
    register_help,
    send_help,
)
from noah_bot.modules.ruleboard_emojis import ICON_LABELS, RuleboardEmojis
from noah_bot.modules.rules import DEFAULT_COLOR, TEXT_FIELDS, RulesError, RulesStore

TEXT_LIMIT = 4000  # límite de Discord para el texto de un mensaje con componentes V2
RULE_LIMIT = 300
MAX_RULES = 25  # opciones de un desplegable
CONFIG_TIMEOUT = 900
CONFIG_COLOR = discord.Color.from_rgb(180, 108, 255)
CUSTOM_EMOJI = re.compile(r"^<a?:\w{2,32}:\d{15,25}>$")
HEX_COLOR = re.compile(r"^#?([0-9a-fA-F]{6})$")
NO_MENTIONS = discord.AllowedMentions.none()
LOCAL_TZ = ZoneInfo("Europe/Madrid")
EMOJIS = RuleboardEmojis()
log = logging.getLogger(__name__)


HELP = register_help(
    HelpModule(
        key="ruleboard",
        emoji="📜",
        name="Ruleboard",
        summary="Panel de normas del servidor, editable sin republicar",
        description=(
            "Publica las normas en un panel y gestiónalas desde un único menú: cada cambio "
            "actualiza el mensaje publicado en sitio, sin mandar uno nuevo."
        ),
        staff=True,
        sections=(
            HelpSection(
                "Comando",
                (HelpEntry(".noah ruleboard config", "Abre el panel para gestionar las normas."),),
            ),
            HelpSection(
                "Qué se puede hacer",
                text=(
                    "• Elegir una norma en el desplegable y **editarla**, **subirla**, **bajarla** o **quitarla**.\n"
                    "• **Añadir** normas con un icono del catálogo (o un emoji propio) y posición opcional.\n"
                    "• **Textos**: título, soporte e incumplimiento. Usa `{tickets}` para nombrar el canal de tickets.\n"
                    "• **Estilo**: firma del pie y color de la franja y los iconos.\n"
                    "• **Tickets**: canal del botón «Ir a tickets» (vacío = uno con «ticket» en el nombre).\n"
                    "• **Publicar**: elige canal; si ya estaba en otro, se mueve.\n"
                    "• **Vista previa** solo para ti y **Restaurar** la lista original."
                ),
            ),
        ),
    )
)


# ---------------------------------------------------------------------------
# Utilidades


def _store(bot: commands.Bot | discord.Client) -> RulesStore:
    return get_bot_context(bot).rules


def _can_manage(user: discord.abc.User) -> bool:
    if not isinstance(user, discord.Member):
        return False
    perms = user.guild_permissions
    return user.id == user.guild.owner_id or perms.administrator or perms.manage_guild


def _looks_like_emoji(token: str) -> bool:
    if CUSTOM_EMOJI.match(token):
        return True
    if not token or len(token) > 12 or any(ch.isalnum() for ch in token):
        return False
    return any(unicodedata.category(ch) == "So" for ch in token)


def _partial_emoji(emoji: str | None) -> discord.PartialEmoji | None:
    if not emoji or not _looks_like_emoji(emoji):
        return None
    try:
        return discord.PartialEmoji.from_str(emoji)
    except (TypeError, ValueError):
        return None


def _tickets_channel(guild: discord.Guild, data: dict[str, Any]) -> discord.abc.GuildChannel | None:
    channel_id = data.get("tickets_channel_id")
    if channel_id:
        return guild.get_channel(int(channel_id))
    return next((ch for ch in guild.text_channels if "ticket" in ch.name.lower()), None)


def _avatar_url(bot: commands.Bot | discord.Client) -> str | None:
    return bot.user.display_avatar.url if bot.user is not None else None


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _rule_icon(rule: dict[str, Any], color: int) -> str:
    """Emoji con el que se pinta la norma: su emoji propio, su icono o un punto."""
    return rule.get("emoji") or EMOJIS.icon(rule.get("icon"), color) or "•"


async def _prepare_emojis(bot: discord.Client, guild: discord.Guild) -> list[str]:
    color = int(_store(bot).get(guild.id)["color"])
    return await EMOJIS.prepare(bot, guild, color, _store(bot).colors())


def _problems_note(problems: list[str]) -> str:
    return f" ⚠️ Emojis: {'; '.join(problems[:2])}" if problems else ""


# ---------------------------------------------------------------------------
# Panel publicado


def _rule_lines(rules: list[dict[str, Any]], color: int) -> str:
    width = max(2, len(str(len(rules))))
    return "\n".join(
        f"{_rule_icon(rule, color)} **{index:0{width}d}** · {rule['text']}"
        for index, rule in enumerate(rules, start=1)
    )


def _panel_texts(guild: discord.Guild, data: dict[str, Any]) -> dict[str, str]:
    texts = data["texts"]
    color = int(data["color"])
    tickets = _tickets_channel(guild, data)
    tickets_ref = tickets.mention if tickets is not None else "#tickets"
    count = len(data["rules"])
    updated = datetime.fromtimestamp(int(data["updated_at"]), LOCAL_TZ).strftime("%d/%m/%Y")
    support = texts["soporte"].replace("{tickets}", tickets_ref)
    logo = EMOJIS.logo(guild)
    return {
        "header": (
            f"## {texts['titulo']}\n"
            f"-# {count} {'norma' if count == 1 else 'normas'} · actualizado el {updated}"
        ),
        "rules": _rule_lines(data["rules"], color) or "-# Todavía no hay normas.",
        "support": f"**{texts['soporte_titulo']}**\n{support}" if texts["soporte_titulo"] else support,
        "breach": (
            f"**{texts['incumplimiento_titulo']}**\n-# {texts['incumplimiento']}"
            if texts["incumplimiento_titulo"]
            else f"-# {texts['incumplimiento']}"
        ),
        "footer": f"-# {logo} {texts['firma']}" if logo else f"-# {texts['firma']}",
    }


def panel_length(guild: discord.Guild, data: dict[str, Any]) -> int:
    return sum(len(text) for text in _panel_texts(guild, data).values())


def build_rules_view(guild: discord.Guild, data: dict[str, Any], avatar_url: str | None) -> discord.ui.LayoutView:
    texts = _panel_texts(guild, data)
    box = discord.ui.Container(accent_color=discord.Color(int(data["color"])))

    if avatar_url:
        box.add_item(discord.ui.Section(texts["header"], accessory=discord.ui.Thumbnail(avatar_url)))
    else:
        box.add_item(discord.ui.TextDisplay(texts["header"]))
    box.add_item(discord.ui.Separator())
    box.add_item(discord.ui.TextDisplay(texts["rules"]))
    box.add_item(discord.ui.Separator())

    tickets = _tickets_channel(guild, data)
    if tickets is not None:
        button = discord.ui.Button(
            label="Ir a tickets", emoji=EMOJIS.button_icon() or "🎫", url=tickets.jump_url
        )
        box.add_item(discord.ui.Section(texts["support"], accessory=button))
    else:
        box.add_item(discord.ui.TextDisplay(texts["support"]))
    box.add_item(discord.ui.Separator())
    box.add_item(discord.ui.TextDisplay(texts["breach"]))
    box.add_item(discord.ui.TextDisplay(texts["footer"]))

    view = discord.ui.LayoutView(timeout=None)
    view.add_item(box)
    return view


def _check_length(guild: discord.Guild, data: dict[str, Any]) -> None:
    if panel_length(guild, data) > TEXT_LIMIT:
        raise RulesError(f"El panel pasaría de {TEXT_LIMIT} caracteres (límite de Discord). Acorta algún texto.")


async def _sync_published(bot: discord.Client, guild: discord.Guild) -> str:
    """Edita el panel publicado. Devuelve una línea corta con el resultado."""
    problems = await _prepare_emojis(bot, guild)
    store = _store(bot)
    data = store.get(guild.id)
    if not data.get("message_id"):
        return "Aún sin publicar." + _problems_note(problems)

    channel = guild.get_channel(int(data["channel_id"]))
    if not isinstance(channel, discord.TextChannel):
        store.set_message(guild.id, None, None)
        return "⚠️ El canal del panel ya no existe: vuelve a publicarlo."

    view = build_rules_view(guild, data, _avatar_url(bot))
    try:
        await channel.get_partial_message(int(data["message_id"])).edit(view=view, allowed_mentions=NO_MENTIONS)
    except discord.NotFound:
        store.set_message(guild.id, None, None)
        return "⚠️ El mensaje publicado se borró: vuelve a publicarlo."
    except discord.HTTPException:
        # p. ej. un panel publicado como embed en una versión anterior: se publica de nuevo.
        return await _repost(bot, guild, channel)
    return f"Panel actualizado en {channel.mention}." + _problems_note(problems)


async def _repost(bot: discord.Client, guild: discord.Guild, channel: discord.TextChannel) -> str:
    store = _store(bot)
    data = store.get(guild.id)
    try:
        message = await channel.send(view=build_rules_view(guild, data, _avatar_url(bot)), allowed_mentions=NO_MENTIONS)
    except discord.Forbidden:
        raise RulesError(f"No puedo escribir en {channel.mention}.") from None

    note = f"Publicado en {channel.mention}."
    if data.get("message_id"):
        old_channel = guild.get_channel(int(data["channel_id"]))
        if isinstance(old_channel, discord.TextChannel):
            try:
                await old_channel.get_partial_message(int(data["message_id"])).delete()
                note += f" Borrado el anterior de {old_channel.mention}."
            except discord.NotFound:
                pass
            except discord.HTTPException:
                note += f" No he podido borrar el anterior de {old_channel.mention}."
    store.set_message(guild.id, channel.id, message.id)
    return note


async def _publish(bot: discord.Client, guild: discord.Guild, channel: discord.TextChannel) -> str:
    data = _store(bot).get(guild.id)
    if data.get("message_id") and int(data["channel_id"]) == channel.id:
        return await _sync_published(bot, guild)
    problems = await _prepare_emojis(bot, guild)
    return await _repost(bot, guild, channel) + _problems_note(problems)


# ---------------------------------------------------------------------------
# Panel de configuración


def _button(
    label: str,
    callback: Callable[[discord.Interaction], Awaitable[None]],
    *,
    emoji: str | None = None,
    style: discord.ButtonStyle = discord.ButtonStyle.secondary,
    disabled: bool = False,
) -> discord.ui.Button:
    button = discord.ui.Button(label=label, emoji=emoji, style=style, disabled=disabled)
    button.callback = callback
    return button


def _row(*items: discord.ui.Item[Any]) -> discord.ui.ActionRow:
    row = discord.ui.ActionRow()
    for item in items:
        row.add_item(item)
    return row


class RuleboardConfigView(discord.ui.LayoutView):
    def __init__(self, bot: discord.Client, guild: discord.Guild, notice: str = "") -> None:
        super().__init__(timeout=CONFIG_TIMEOUT)
        self.bot = bot
        self.guild = guild
        self.selected: int | None = None  # número de norma (1..n)
        self.confirm_reset = False
        self.notice = notice
        self.message: discord.Message | None = None
        self.render()

    # ---------- pintado ----------

    def render(self) -> None:
        self.clear_items()
        data = _store(self.bot).get(self.guild.id)
        rules = data["rules"]
        color = int(data["color"])
        if self.selected is not None and not 1 <= self.selected <= len(rules):
            self.selected = None

        if data.get("message_id"):
            jump = f"https://discord.com/channels/{self.guild.id}/{data['channel_id']}/{data['message_id']}"
            published = f"Publicado en <#{data['channel_id']}> · [ir al mensaje]({jump})"
        else:
            published = "Sin publicar"
        tickets = _tickets_channel(self.guild, data)
        tickets_text = tickets.mention if tickets is not None else "ninguno"
        if not data.get("tickets_channel_id") and tickets is not None:
            tickets_text += " (auto)"
        used = panel_length(self.guild, data)

        box = discord.ui.Container(accent_color=discord.Color(color))
        box.add_item(
            discord.ui.TextDisplay(
                "## 📜 Ruleboard · configuración\n"
                f"-# {published}\n"
                f"-# Tickets: {tickets_text} · Color: #{color:06x} · {used}/{TEXT_LIMIT} caracteres"
            )
        )
        box.add_item(discord.ui.Separator())

        width = max(2, len(str(len(rules))))
        lines = []
        for index, rule in enumerate(rules, start=1):
            line = f"{_rule_icon(rule, color)} `{index:0{width}d}` {_clip(rule['text'], 90)}"
            lines.append(f"**▸** {line}" if index == self.selected else line)
        box.add_item(discord.ui.TextDisplay("\n".join(lines) or "-# Todavía no hay normas."))

        if rules:
            options = [
                discord.SelectOption(
                    label=_clip(f"{index}. {rule['text']}", 100),
                    value=str(index),
                    emoji=_partial_emoji(_rule_icon(rule, color)),
                    default=index == self.selected,
                )
                for index, rule in enumerate(rules, start=1)
            ]
            select = discord.ui.Select(placeholder="Elige una norma para editarla, moverla o quitarla", options=options)
            select.callback = self._on_select
            box.add_item(_row(select))

        none_selected = self.selected is None
        box.add_item(
            _row(
                _button(
                    "Añadir",
                    self._on_add,
                    emoji="➕",
                    style=discord.ButtonStyle.success,
                    disabled=len(rules) >= MAX_RULES,
                ),
                _button("Editar", self._on_edit, emoji="✏️", disabled=none_selected),
                _button("Subir", self._on_up, emoji="⬆️", disabled=none_selected or self.selected == 1),
                _button("Bajar", self._on_down, emoji="⬇️", disabled=none_selected or self.selected == len(rules)),
                _button(
                    "Quitar",
                    self._on_remove,
                    emoji="🗑️",
                    style=discord.ButtonStyle.danger,
                    disabled=none_selected,
                ),
            )
        )
        box.add_item(discord.ui.Separator(visible=False, spacing=discord.SeparatorSpacing.small))
        box.add_item(
            _row(
                _button("Textos", self._on_texts, emoji="📝"),
                _button("Estilo", self._on_style, emoji="🎨"),
                _button("Tickets", self._on_tickets, emoji="🎫"),
                _button(
                    "¿Seguro? Pulsa otra vez" if self.confirm_reset else "Restaurar",
                    self._on_reset,
                    emoji="♻️",
                    style=discord.ButtonStyle.danger if self.confirm_reset else discord.ButtonStyle.secondary,
                ),
            )
        )
        box.add_item(
            _row(
                _button(
                    "Mover / publicar" if data.get("message_id") else "Publicar",
                    self._on_publish,
                    emoji="📤",
                    style=discord.ButtonStyle.primary,
                ),
                _button("Vista previa", self._on_preview, emoji="👁️"),
                _button("Cerrar", self._on_close, emoji="✖️"),
            )
        )
        if self.notice:
            box.add_item(discord.ui.Separator())
            box.add_item(discord.ui.TextDisplay(f"-# {self.notice}"))
        self.add_item(box)

    async def refresh(self, interaction: discord.Interaction, notice: str = "") -> None:
        self.notice = notice
        self.render()
        if interaction.response.is_done():
            await interaction.edit_original_response(view=self, allowed_mentions=NO_MENTIONS)
        else:
            await interaction.response.edit_message(view=self, allowed_mentions=NO_MENTIONS)

    async def changed(self, interaction: discord.Interaction, text: str) -> None:
        """Tras un cambio: actualiza el panel publicado y el de configuración."""
        self.confirm_reset = False
        if not interaction.response.is_done():
            await interaction.response.defer()
        sync = await _sync_published(self.bot, self.guild)
        await self.refresh(interaction, f"{text} · {sync}")

    # ---------- control de acceso ----------

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not _can_manage(interaction.user):
            await interaction.response.send_message("❌ Solo los admins pueden gestionar las normas.", ephemeral=True)
            return False
        return True

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item[Any]) -> None:
        text = f"❌ {error}" if isinstance(error, RulesError) else "❌ Algo ha fallado. Inténtalo de nuevo."
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)
        if not isinstance(error, RulesError):
            await super().on_error(interaction, error, item)

    async def on_timeout(self) -> None:
        if self.message is None:
            return
        closed = discord.ui.LayoutView()
        box = discord.ui.Container(accent_color=CONFIG_COLOR)
        box.add_item(discord.ui.TextDisplay("-# Panel de normas cerrado. Ábrelo con `.noah ruleboard config`."))
        closed.add_item(box)
        try:
            await self.message.edit(view=closed)
        except discord.HTTPException:
            pass

    # ---------- acciones ----------

    async def _on_select(self, interaction: discord.Interaction) -> None:
        values = interaction.data.get("values") or []
        self.selected = int(values[0]) if values else None
        self.confirm_reset = False
        await self.refresh(interaction)

    async def _on_add(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(RuleModal(self))

    async def _on_edit(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(RuleModal(self, self.selected))

    async def _move(self, interaction: discord.Interaction, delta: int) -> None:
        number = self.selected
        self.selected = _store(self.bot).move(self.guild.id, number, number + delta)
        await self.changed(interaction, f"Norma {number} movida a la posición {self.selected}.")

    async def _on_up(self, interaction: discord.Interaction) -> None:
        await self._move(interaction, -1)

    async def _on_down(self, interaction: discord.Interaction) -> None:
        await self._move(interaction, 1)

    async def _on_remove(self, interaction: discord.Interaction) -> None:
        rule = _store(self.bot).remove(self.guild.id, self.selected)
        self.selected = None
        await self.changed(interaction, f"🗑️ Quitada: {_clip(rule['text'], 60)}")

    async def _on_texts(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(TextsModal(self))

    async def _on_style(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(StyleModal(self))

    async def _on_tickets(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(ChannelModal(self, kind="tickets"))

    async def _on_publish(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(ChannelModal(self, kind="publish"))

    async def _on_reset(self, interaction: discord.Interaction) -> None:
        if not self.confirm_reset:
            self.confirm_reset = True
            await self.refresh(interaction, "Esto sustituye todas las normas por la lista original. Pulsa otra vez para confirmar.")
            return
        _store(self.bot).reset_rules(self.guild.id)
        self.selected = None
        await self.changed(interaction, "♻️ Normas restauradas a la lista original.")

    async def _on_preview(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        problems = await _prepare_emojis(self.bot, self.guild)
        data = _store(self.bot).get(self.guild.id)
        await interaction.followup.send(
            view=build_rules_view(self.guild, data, _avatar_url(self.bot)),
            ephemeral=True,
            allowed_mentions=NO_MENTIONS,
        )
        if problems:
            await interaction.followup.send(_problems_note(problems).strip(), ephemeral=True)

    async def _on_close(self, interaction: discord.Interaction) -> None:
        self.stop()
        await interaction.response.defer()
        await interaction.delete_original_response()


# ---------------------------------------------------------------------------
# Formularios


class RuleModal(discord.ui.Modal):
    def __init__(self, parent: RuleboardConfigView, number: int | None = None) -> None:
        super().__init__(title=f"Editar norma {number}" if number else "Nueva norma", timeout=CONFIG_TIMEOUT)
        self.parent = parent
        self.number = number
        data = _store(parent.bot).get(parent.guild.id)
        color = int(data["color"])
        rule = data["rules"][number - 1] if number else {}

        self.text_input = discord.ui.TextInput(
            style=discord.TextStyle.paragraph,
            max_length=RULE_LIMIT,
            default=rule.get("text"),
            placeholder="Respetar el uso de los canales de texto.",
        )
        self.icon_select = discord.ui.Select(
            placeholder="Elige un icono",
            min_values=0,
            max_values=1,
            required=False,
            options=[
                discord.SelectOption(
                    label=label,
                    value=key,
                    emoji=_partial_emoji(EMOJIS.icon(key, color)),
                    default=key == rule.get("icon"),
                )
                for key, label in ICON_LABELS.items()
            ],
        )
        self.emoji_input = discord.ui.TextInput(
            required=False, max_length=64, default=rule.get("emoji"), placeholder="📁 o <:nombre:id>"
        )
        self.add_item(discord.ui.Label(text="Norma", component=self.text_input))
        self.add_item(discord.ui.Label(text="Icono", component=self.icon_select))
        self.add_item(
            discord.ui.Label(
                text="Emoji propio (opcional)",
                description="Si lo pones, se usa en vez del icono. Normal o del server (<:nombre:id>).",
                component=self.emoji_input,
            )
        )
        self.position_input: discord.ui.TextInput | None = None
        if number is None:
            self.position_input = discord.ui.TextInput(required=False, max_length=3, placeholder="Al final")
            self.add_item(
                discord.ui.Label(
                    text="Posición (opcional)",
                    description="Número donde colocarla. Vacío = al final.",
                    component=self.position_input,
                )
            )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        parent = self.parent
        text = self.text_input.value.strip()
        emoji = self.emoji_input.value.strip() or None
        icon = None if emoji else (self.icon_select.values[0] if self.icon_select.values else None)
        if emoji and not _looks_like_emoji(emoji):
            raise RulesError(f"`{emoji}` no parece un emoji.")
        if not text:
            raise RulesError("Falta el texto de la norma.")

        position = None
        if self.position_input is not None and self.position_input.value.strip():
            raw = self.position_input.value.strip()
            if not raw.isdigit():
                raise RulesError(f"`{raw}` no es una posición válida.")
            position = int(raw)

        store = _store(parent.bot)
        preview = store.get(parent.guild.id)
        new_rule = {"icon": icon, "emoji": emoji, "text": text}
        if self.number is None:
            if len(preview["rules"]) >= MAX_RULES:
                raise RulesError(f"Máximo {MAX_RULES} normas.")
            preview["rules"].append(new_rule)
        else:
            preview["rules"][self.number - 1] = new_rule
        _check_length(parent.guild, preview)

        if self.number is None:
            parent.selected = store.add(parent.guild.id, text, icon=icon, emoji=emoji, position=position)
            await parent.changed(interaction, f"➕ Norma {parent.selected} añadida.")
        else:
            store.edit(parent.guild.id, self.number, text=text, icon=icon, emoji=emoji)
            await parent.changed(interaction, f"✏️ Norma {self.number} editada.")

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await _modal_error(interaction, error)


class TextsModal(discord.ui.Modal, title="Textos del panel"):
    FIELDS = (
        ("titulo", "Título", discord.TextStyle.short, 100, "Vacío = texto original."),
        ("soporte_titulo", "Título de soporte", discord.TextStyle.short, 100, "Vacío = texto original."),
        ("soporte", "Texto de soporte", discord.TextStyle.paragraph, 800, "Usa {tickets} para nombrar el canal de tickets."),
        ("incumplimiento_titulo", "Título de incumplimiento", discord.TextStyle.short, 100, "Opcional. Vacío = sin título."),
        ("incumplimiento", "Texto de incumplimiento", discord.TextStyle.paragraph, 800, "Vacío = texto original."),
    )

    def __init__(self, parent: RuleboardConfigView) -> None:
        super().__init__(timeout=CONFIG_TIMEOUT)
        self.parent = parent
        texts = _store(parent.bot).get(parent.guild.id)["texts"]
        self.inputs: dict[str, discord.ui.TextInput] = {}
        for key, label, style, limit, description in self.FIELDS:
            field = discord.ui.TextInput(style=style, required=False, max_length=limit, default=texts[key] or None)
            self.inputs[key] = field
            self.add_item(discord.ui.Label(text=label, description=description, component=field))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        parent = self.parent
        store = _store(parent.bot)
        preview = store.get(parent.guild.id)
        values = {key: field.value.strip() or None for key, field in self.inputs.items()}
        for key, value in values.items():
            preview["texts"][key] = value or TEXT_FIELDS[key]
        _check_length(parent.guild, preview)
        for key, value in values.items():
            store.set_text(parent.guild.id, key, None if value in (None, TEXT_FIELDS[key]) else value)
        await parent.changed(interaction, "📝 Textos actualizados.")

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await _modal_error(interaction, error)


class StyleModal(discord.ui.Modal, title="Estilo del panel"):
    def __init__(self, parent: RuleboardConfigView) -> None:
        super().__init__(timeout=CONFIG_TIMEOUT)
        self.parent = parent
        data = _store(parent.bot).get(parent.guild.id)
        self.signature = discord.ui.TextInput(required=False, max_length=100, default=data["texts"]["firma"])
        self.color = discord.ui.TextInput(
            required=False, max_length=7, default=f"#{int(data['color']):06x}", placeholder=f"#{DEFAULT_COLOR:06x}"
        )
        self.add_item(
            discord.ui.Label(
                text="Firma",
                description="Va al pie, junto al logo del server. Vacío = original.",
                component=self.signature,
            )
        )
        self.add_item(
            discord.ui.Label(
                text="Color de la franja y los iconos",
                description=f"Hexadecimal. Vacío = #{DEFAULT_COLOR:06x} (azul cian).",
                component=self.color,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        parent = self.parent
        store = _store(parent.bot)
        raw_color = self.color.value.strip()
        color = DEFAULT_COLOR
        if raw_color:
            match = HEX_COLOR.match(raw_color)
            if match is None:
                raise RulesError(f"Usa un color en hexadecimal, por ejemplo `#{DEFAULT_COLOR:06x}`.")
            color = int(match.group(1), 16)
        signature = self.signature.value.strip() or None

        preview = store.get(parent.guild.id)
        preview["texts"]["firma"] = signature or TEXT_FIELDS["firma"]
        _check_length(parent.guild, preview)

        store.set_text(parent.guild.id, "firma", signature)
        store.set_color(parent.guild.id, color)
        await parent.changed(interaction, "🎨 Estilo actualizado.")

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await _modal_error(interaction, error)


class ChannelModal(discord.ui.Modal):
    def __init__(self, parent: RuleboardConfigView, *, kind: str) -> None:
        publish = kind == "publish"
        super().__init__(title="Publicar normas" if publish else "Canal de tickets", timeout=CONFIG_TIMEOUT)
        self.parent = parent
        self.kind = kind
        data = _store(parent.bot).get(parent.guild.id)
        current = data.get("channel_id") if publish else data.get("tickets_channel_id")
        defaults = []
        if current and parent.guild.get_channel(int(current)) is not None:
            defaults = [discord.Object(id=int(current), type=discord.TextChannel)]

        self.select = discord.ui.ChannelSelect(
            channel_types=[discord.ChannelType.text],
            placeholder="Elige un canal",
            min_values=1 if publish else 0,
            max_values=1,
            required=publish,
            default_values=defaults,
        )
        self.add_item(
            discord.ui.Label(
                text="Canal" if publish else "Canal de tickets",
                description=(
                    "Si ya estaba publicado en otro canal, se mueve aquí y se borra el anterior."
                    if publish
                    else "Vacío = automático (un canal con «ticket» en el nombre)."
                ),
                component=self.select,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        parent = self.parent
        picked = self.select.values[0].id if self.select.values else None
        channel = parent.guild.get_channel(picked) if picked else None

        if self.kind == "tickets":
            _store(parent.bot).set_tickets_channel(parent.guild.id, channel.id if channel else None)
            label = channel.mention if channel else "automático"
            await parent.changed(interaction, f"🎫 Canal de tickets: {label}.")
            return

        if not isinstance(channel, discord.TextChannel):
            raise RulesError("Elige un canal de texto.")
        await interaction.response.defer()
        note = await _publish(parent.bot, parent.guild, channel)
        await parent.refresh(interaction, f"📤 {note}")

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await _modal_error(interaction, error)


async def _modal_error(interaction: discord.Interaction, error: Exception) -> None:
    text = f"❌ {error}" if isinstance(error, RulesError) else "❌ Algo ha fallado. Inténtalo de nuevo."
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True)
    else:
        await interaction.response.send_message(text, ephemeral=True)
    if not isinstance(error, RulesError):
        log.exception("Error en un formulario de ruleboard", exc_info=error)


# ---------------------------------------------------------------------------
# Comandos


def register_rules_commands(bot: commands.Bot, noah_group: commands.Group) -> None:
    @noah_group.group(name="ruleboard", invoke_without_command=True)
    async def ruleboard(ctx: commands.Context) -> None:
        await send_help(ctx, "ruleboard")

    @ruleboard.command(name="config")
    async def ruleboard_config(ctx: commands.Context) -> None:
        if ctx.guild is None:
            return
        if not _can_manage(ctx.author):
            await ctx.send("❌ Solo los admins pueden gestionar las normas.")
            return
        async with ctx.typing():
            # La primera vez sube los iconos y el logo como emojis de la aplicación.
            problems = await _prepare_emojis(ctx.bot, ctx.guild)
        view = RuleboardConfigView(ctx.bot, ctx.guild, notice=_problems_note(problems).strip())
        view.message = await ctx.send(view=view, allowed_mentions=NO_MENTIONS)

    @ruleboard.command(name="help")
    async def ruleboard_help(ctx: commands.Context) -> None:
        await send_help(ctx, "ruleboard")
