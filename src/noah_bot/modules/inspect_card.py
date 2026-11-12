"""Tarjeta de perfil de `.noah rrpp inspect`, dibujada con Pillow."""

import io
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

FONTS_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"
FONT_FILES = {"regular": "Inter-Regular.otf", "semibold": "Inter-SemiBold.otf"}

WIDTH, HEIGHT = 1200, 440
MARGIN = 56
AVATAR_SIZE = 184
BACKGROUND = (24, 25, 29)
RING = (52, 55, 62)
TEXT = (242, 243, 245)
MUTED = (148, 155, 164)
LINE = (46, 48, 54)
FALLBACK_AVATAR = (235, 69, 158)
# Tono de cada insignia: (texto, fondo).
TONES: dict[str, tuple[tuple[int, int, int], tuple[int, int, int]]] = {
    "ok": ((87, 242, 135), (22, 58, 37)),
    "warn": ((250, 196, 82), (62, 49, 20)),
    "bad": ((250, 119, 124), (66, 26, 29)),
    "info": ((150, 163, 255), (34, 38, 82)),
}


@dataclass
class Badge:
    text: str
    tone: str = "info"


@dataclass
class Stat:
    label: str
    value: str
    sub: str = ""


@dataclass
class CardData:
    name: str
    username: str
    user_id: int
    avatar: bytes | None = None
    badges: list[Badge] = field(default_factory=list)
    stats: list[Stat] = field(default_factory=list)


@lru_cache(maxsize=32)
def _font(weight: str, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    try:
        return ImageFont.truetype(str(FONTS_DIR / FONT_FILES[weight]), size)
    except OSError:
        for fallback in ("DejaVuSans.ttf", "arial.ttf"):
            try:
                return ImageFont.truetype(fallback, size)
            except OSError:
                continue
        return ImageFont.load_default()


def _fit(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: float) -> str:
    if draw.textlength(text, font=font) <= max_width:
        return text
    while text and draw.textlength(text + "…", font=font) > max_width:
        text = text[:-1]
    return text.rstrip() + "…"


def _draw_spaced(
    draw: ImageDraw.ImageDraw,
    xy: tuple[float, float],
    text: str,
    font: ImageFont.ImageFont,
    spacing: float,
    fill: tuple[int, int, int],
) -> float:
    """Dibuja `text` con espacio entre letras y devuelve el ancho total."""
    x, y = xy
    for char in text:
        draw.text((x, y), char, font=font, fill=fill)
        x += draw.textlength(char, font=font) + spacing
    return x - xy[0] - spacing


def _spaced_width(
    draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, spacing: float
) -> float:
    return sum(draw.textlength(char, font=font) for char in text) + spacing * max(0, len(text) - 1)


def _avatar(data: bytes | None, name: str) -> Image.Image:
    image: Image.Image | None = None
    if data:
        try:
            image = ImageOps.fit(Image.open(io.BytesIO(data)).convert("RGBA"), (AVATAR_SIZE, AVATAR_SIZE))
        except (OSError, ValueError):
            image = None
    if image is None:
        image = Image.new("RGBA", (AVATAR_SIZE, AVATAR_SIZE), FALLBACK_AVATAR)
        draw = ImageDraw.Draw(image)
        initial = (name.strip()[:1] or "?").upper()
        draw.text(
            (AVATAR_SIZE / 2, AVATAR_SIZE / 2), initial, font=_font("semibold", 84), fill=TEXT, anchor="mm"
        )

    # Máscara grande reducida para que el borde del círculo quede suave.
    big = AVATAR_SIZE * 4
    mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, big - 1, big - 1), fill=255)
    image.putalpha(mask.resize((AVATAR_SIZE, AVATAR_SIZE), Image.Resampling.LANCZOS))
    return image


def _draw_badges(draw: ImageDraw.ImageDraw, x: float, y: float, max_x: float, badges: list[Badge]) -> None:
    font = _font("semibold", 19)
    spacing = 1.6
    height = 40
    for badge in badges:
        fg, bg = TONES.get(badge.tone, TONES["info"])
        label = badge.text.upper()
        width = 22 + 12 + _spaced_width(draw, label, font, spacing) + 20
        if x + width > max_x:
            break
        draw.rounded_rectangle((x, y, x + width, y + height), radius=height / 2, fill=bg)
        draw.ellipse((x + 18, y + height / 2 - 5, x + 28, y + height / 2 + 5), fill=fg)
        _draw_spaced(draw, (x + 36, y + 9), label, font, spacing, fg)
        x += width + 12


def render_card(data: CardData) -> bytes:
    """PNG con avatar, nombre, insignias y una fila de datos clave."""
    image = Image.new("RGB", (WIDTH, HEIGHT), BACKGROUND)
    draw = ImageDraw.Draw(image)

    top = 52
    draw.ellipse(
        (MARGIN - 5, top - 5, MARGIN + AVATAR_SIZE + 5, top + AVATAR_SIZE + 5), fill=RING
    )
    avatar = _avatar(data.avatar, data.name)
    image.paste(avatar, (MARGIN, top), avatar)

    x = MARGIN + AVATAR_SIZE + 44
    max_width = WIDTH - MARGIN - x
    name_font = _font("semibold", 58)
    draw.text((x, top + 4), _fit(draw, data.name, name_font, max_width), font=name_font, fill=TEXT)
    handle_font = _font("regular", 25)
    handle = f"@{data.username}   ·   ID {data.user_id}"
    draw.text((x, top + 86), _fit(draw, handle, handle_font, max_width), font=handle_font, fill=MUTED)
    _draw_badges(draw, x, top + 136, WIDTH - MARGIN, data.badges)

    divider = top + AVATAR_SIZE + 46
    draw.line((MARGIN, divider, WIDTH - MARGIN, divider), fill=LINE, width=2)

    if data.stats:
        column = (WIDTH - 2 * MARGIN) / len(data.stats)
        label_font = _font("semibold", 18)
        value_font = _font("semibold", 36)
        sub_font = _font("regular", 21)
        for index, stat in enumerate(data.stats):
            cx = MARGIN + index * column
            inner = column - 28
            if index:
                draw.line((cx - 14, divider + 26, cx - 14, HEIGHT - 30), fill=LINE, width=2)
            _draw_spaced(draw, (cx, divider + 24), stat.label.upper(), label_font, 1.8, MUTED)
            draw.text((cx, divider + 52), _fit(draw, stat.value, value_font, inner), font=value_font, fill=TEXT)
            if stat.sub:
                draw.text((cx, divider + 100), _fit(draw, stat.sub, sub_font, inner), font=sub_font, fill=MUTED)

    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()
