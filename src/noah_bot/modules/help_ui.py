"""Ayuda unificada de Noah.

Cada módulo declara su ayuda como datos (`HelpModule`) junto a sus comandos y la
registra con `register_help`. Esta plantilla pinta siempre igual:

- `.noah help`: índice con una fila por módulo y un botón **Ver** que abre su ayuda
  en privado, con páginas ◀ ▶.
- `.noah <módulo> help`: la ayuda del módulo en un panel.

Se usan componentes V2 (paneles), cuyo límite es de 4.000 caracteres de texto por
mensaje, así que la ayuda ya no se rompe al crecer como pasaba con los embeds.
"""

import math
import re
from dataclasses import dataclass

import discord
from discord.ext import commands

NOAH_COLOR = discord.Color.from_rgb(180, 108, 255)
STAFF_ROLE_NAME = "Funcionarios"
PAGE_SIZE = 6
LOCK = "🔒"

# Orden del índice. Los módulos que no estén aquí van al final por orden de registro.
INDEX_ORDER = (
    "general",
    "ai",
    "monitor",
    "vc",
    "tts",
    "spotify",
    "confess",
    "billboard",
    "sports",
    "presentations",
    "waifu",
    "waifuracer",
    "autogami",
    "steallist",
    "autobump",
    "guard",
    "mod",
    "ruleboard",
    "stickers",
    "rrpp",
    "council",
)

# Comandos sueltos que se enseñan en la cabecera del índice.
QUICK_COMMANDS = ("ping", "d20", "daily", "ask", "quote", "merge")


@dataclass(frozen=True, slots=True)
class HelpEntry:
    usage: str
    text: str
    staff: bool = False


@dataclass(frozen=True, slots=True)
class HelpSection:
    title: str
    entries: tuple[HelpEntry, ...] = ()
    # Texto libre en lugar de (o además de) comandos, p. ej. cómo funciona un panel.
    text: str = ""
    staff: bool = False


@dataclass(frozen=True, slots=True)
class HelpModule:
    key: str
    emoji: str
    name: str
    summary: str  # una línea para el índice
    description: str
    sections: tuple[HelpSection, ...] = ()
    staff: bool = False  # el módulo entero es para staff
    note: str = ""


_REGISTRY: dict[str, HelpModule] = {}


def register_help(module: HelpModule) -> HelpModule:
    _REGISTRY[module.key] = module
    return module


def get_help(key: str) -> HelpModule | None:
    return _REGISTRY.get(key)


def ordered_modules() -> list[HelpModule]:
    ranked = {key: index for index, key in enumerate(INDEX_ORDER)}
    return sorted(_REGISTRY.values(), key=lambda module: ranked.get(module.key, len(ranked)))


def catalog_text() -> str:
    """Todos los comandos en texto plano, para Noah AI."""
    lines: list[str] = []
    for module in ordered_modules():
        staff = " (solo staff)" if module.staff else ""
        lines.append(f"\n## {module.name}{staff}: {module.summary}")
        for section in module.sections:
            for entry in section.entries:
                locked = " (solo staff)" if (entry.staff or section.staff) and not module.staff else ""
                lines.append(f"- {entry.usage}: {entry.text}{locked}")
    return "\n".join(lines).strip()


def is_staff(user: discord.abc.User | None) -> bool:
    if not isinstance(user, discord.Member):
        return False
    permissions = user.guild_permissions
    if permissions.administrator or permissions.manage_guild:
        return True
    return any(role.name == STAFF_ROLE_NAME for role in user.roles)


# ---------------------------------------------------------------------------
# Detalle de un módulo


def _entry_line(entry: HelpEntry) -> str:
    lock = f" {LOCK}" if entry.staff else ""
    return f"`{entry.usage}`{lock}\n-# {entry.text}" if entry.text else f"`{entry.usage}`{lock}"


def module_text_blocks(module: HelpModule, viewer_is_staff: bool) -> list[str]:
    """Bloques de texto del detalle (útil también para medir y probar)."""
    lock = f" {LOCK}" if module.staff else ""
    blocks = [f"## {module.emoji} {module.name}{lock}\n{module.description}"]

    hidden = 0
    has_locked = False
    for section in module.sections:
        restricted = section.staff or module.staff
        entries = [
            entry for entry in section.entries if viewer_is_staff or not (restricted or entry.staff)
        ]
        hidden += len(section.entries) - len(entries)
        has_locked = has_locked or section.staff or any(entry.staff for entry in section.entries)
        show_text = bool(section.text) and (viewer_is_staff or not restricted)
        if not show_text and not entries:
            hidden += 1 if section.text else 0
            continue
        title = f"**{section.title}**" + (f" {LOCK}" if section.staff and not module.staff else "")
        parts = [title]
        if show_text:
            parts.append(section.text)
        parts.extend(_entry_line(entry) for entry in entries)
        blocks.append("\n".join(parts))

    footer: list[str] = []
    if hidden and not viewer_is_staff:
        footer.append(
            f"{LOCK} Sus comandos son solo para staff."
            if module.staff
            else f"{LOCK} Hay más comandos solo para staff."
        )
    elif has_locked and not module.staff:
        footer.append(f"{LOCK} solo staff")
    if module.note:
        footer.append(module.note)
    if footer:
        blocks.append("-# " + " · ".join(footer))
    return blocks


def build_module_view(module: HelpModule, viewer_is_staff: bool) -> discord.ui.LayoutView:
    container = discord.ui.Container(accent_color=NOAH_COLOR)
    blocks = module_text_blocks(module, viewer_is_staff)
    for index, block in enumerate(blocks):
        if index:
            container.add_item(
                discord.ui.Separator(
                    visible=not block.startswith("-# "), spacing=discord.SeparatorSpacing.small
                )
            )
        container.add_item(discord.ui.TextDisplay(block))
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(container)
    return view


async def send_help(ctx: commands.Context, key: str) -> None:
    module = get_help(key)
    if module is None:
        await ctx.send("❌ No hay ayuda para ese módulo. Mira `.noah help`.")
        return
    await ctx.send(
        view=build_module_view(module, is_staff(ctx.author)),
        allowed_mentions=discord.AllowedMentions.none(),
    )


# ---------------------------------------------------------------------------
# Índice


def page_count() -> int:
    return max(1, math.ceil(len(_REGISTRY) / PAGE_SIZE))


class HelpOpenButton(discord.ui.DynamicItem[discord.ui.Button], template=r"noah:help:open:(?P<key>[a-z_]+)"):
    """Botón «Ver» de cada fila. Persistente: funciona tras reiniciar."""

    def __init__(self, key: str) -> None:
        super().__init__(
            discord.ui.Button(
                label="Ver", style=discord.ButtonStyle.secondary, custom_id=f"noah:help:open:{key}"
            )
        )
        self.key = key

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str], /
    ) -> "HelpOpenButton":
        _ = interaction, item
        return cls(match["key"])

    async def callback(self, interaction: discord.Interaction) -> None:
        module = get_help(self.key)
        if module is None:
            await interaction.response.send_message("❌ Ese módulo ya no existe.", ephemeral=True)
            return
        await interaction.response.send_message(
            view=build_module_view(module, is_staff(interaction.user)),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class HelpPageButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"noah:help:page:(?P<page>\d+):(?P<owner>\d+)",
):
    """◀ / ▶. Quien pidió el índice lo pasa de página; el resto recibe su copia privada."""

    def __init__(self, page: int, owner_id: int, *, label: str, disabled: bool = False) -> None:
        super().__init__(
            discord.ui.Button(
                label=label,
                style=discord.ButtonStyle.secondary,
                disabled=disabled,
                custom_id=f"noah:help:page:{page}:{owner_id}",
            )
        )
        self.page = page
        self.owner_id = owner_id

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str], /
    ) -> "HelpPageButton":
        _ = interaction
        return cls(int(match["page"]), int(match["owner"]), label=item.label or "·")

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id == self.owner_id:
            await interaction.response.edit_message(view=build_index_view(self.page, self.owner_id))
        else:
            await interaction.response.send_message(
                view=build_index_view(self.page, interaction.user.id), ephemeral=True
            )


def build_index_view(page: int, owner_id: int) -> discord.ui.LayoutView:
    modules = ordered_modules()
    pages = page_count()
    page = max(0, min(page, pages - 1))
    shown = modules[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]

    container = discord.ui.Container(accent_color=NOAH_COLOR)
    quick = " ".join(f"`{name}`" for name in QUICK_COMMANDS)
    container.add_item(
        discord.ui.TextDisplay(
            "## 🖤 Noah\n"
            "Pulsa **Ver** en un módulo para ver sus comandos (solo lo verás tú).\n"
            f"-# Rápidos: {quick} · todos llevan delante `.noah`"
        )
    )
    container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
    for module in shown:
        lock = f" {LOCK}" if module.staff else ""
        container.add_item(
            discord.ui.Section(
                discord.ui.TextDisplay(f"**{module.emoji} {module.name}**{lock}\n-# {module.summary}"),
                accessory=HelpOpenButton(module.key),
            )
        )
    container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
    container.add_item(
        discord.ui.TextDisplay(
            f"-# {len(modules)} módulos · {LOCK} solo staff · también `.noah <módulo> help`"
        )
    )

    view = discord.ui.LayoutView(timeout=None)
    view.add_item(container)
    if pages > 1:
        row = discord.ui.ActionRow()
        row.add_item(HelpPageButton(max(0, page - 1), owner_id, label="◀", disabled=page == 0))
        row.add_item(
            discord.ui.Button(
                label=f"{page + 1}/{pages}",
                style=discord.ButtonStyle.secondary,
                disabled=True,
                custom_id=f"noah:help:pageinfo:{page}",
            )
        )
        row.add_item(HelpPageButton(min(pages - 1, page + 1), owner_id, label="▶", disabled=page >= pages - 1))
        view.add_item(row)
    return view


def install_help_buttons(bot: commands.Bot) -> None:
    """Registra los botones persistentes (una sola vez)."""
    if getattr(bot, "_noah_help_buttons", False):
        return
    bot._noah_help_buttons = True
    bot.add_dynamic_items(HelpOpenButton, HelpPageButton)
