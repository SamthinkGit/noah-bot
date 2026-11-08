"""Banner de marquesina de la cartelera, dibujado con Pillow."""

import io
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

FONT_PATH = Path(__file__).resolve().parent.parent / "assets" / "fonts" / "Anton-Regular.ttf"

# Se dibuja al doble de tamaño para que se vea nítido en pantallas con zoom.
WIDTH, HEIGHT = 1100, 300
BACKGROUND = (92, 15, 18)
BULB = (255, 213, 107)
TITLE_COLOR = (255, 244, 214)
SUBTITLE_COLOR = (255, 213, 107)
BULB_COUNT = 22
BULB_RADIUS = 7
BULB_MARGIN = 26


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    try:
        return ImageFont.truetype(str(FONT_PATH), size)
    except OSError:
        for fallback in ("impact.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf"):
            try:
                return ImageFont.truetype(fallback, size)
            except OSError:
                continue
        return ImageFont.load_default()


def _spaced_width(
    draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, spacing: int
) -> int:
    widths = [draw.textlength(char, font=font) for char in text]
    return int(sum(widths) + spacing * max(0, len(text) - 1))


def _draw_spaced(
    draw: ImageDraw.ImageDraw,
    center_x: int,
    top: int,
    text: str,
    font: ImageFont.ImageFont,
    spacing: int,
    fill: tuple[int, int, int],
) -> None:
    x = center_x - _spaced_width(draw, text, font, spacing) / 2
    for char in text:
        draw.text((x, top), char, font=font, fill=fill)
        x += draw.textlength(char, font=font) + spacing


def _bulb_row(layer: Image.Image, y: int) -> None:
    draw = ImageDraw.Draw(layer)
    step = (WIDTH - 2 * BULB_MARGIN) / (BULB_COUNT - 1)
    for index in range(BULB_COUNT):
        x = BULB_MARGIN + index * step
        draw.ellipse(
            (x - BULB_RADIUS, y - BULB_RADIUS, x + BULB_RADIUS, y + BULB_RADIUS),
            fill=BULB,
        )


@lru_cache(maxsize=16)
def render_banner(subtitle: str) -> bytes:
    """PNG del banner con «CARTELERA» y el subtítulo (p. ej. el rango de días)."""
    image = Image.new("RGB", (WIDTH, HEIGHT), BACKGROUND)

    # Bombillas con un halo suave detrás, como una marquesina de cine.
    glow = Image.new("RGB", (WIDTH, HEIGHT), BACKGROUND)
    for y in (BULB_MARGIN, HEIGHT - BULB_MARGIN):
        _bulb_row(glow, y)
    image = Image.blend(image, glow.filter(ImageFilter.GaussianBlur(9)), 0.6)
    for y in (BULB_MARGIN, HEIGHT - BULB_MARGIN):
        _bulb_row(image, y)

    draw = ImageDraw.Draw(image)
    title_font = _font(118)
    subtitle_font = _font(30)
    _draw_spaced(draw, WIDTH // 2, 58, "CARTELERA", title_font, 14, TITLE_COLOR)
    _draw_spaced(
        draw, WIDTH // 2, 212, subtitle.upper(), subtitle_font, 6, SUBTITLE_COLOR
    )

    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()
