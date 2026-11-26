"""Banner del sticker del canal de música, dibujado con Pillow."""

import io
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

FONTS_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"
DISPLAY_FONT = FONTS_DIR / "Anton-Regular.ttf"
SMALL_FONT = FONTS_DIR / "Inter-SemiBold.otf"

# Se dibuja al doble de tamaño para que se vea nítido en pantallas con zoom.
WIDTH, HEIGHT = 1200, 400
BACKGROUND = (19, 17, 30)
GLOW_LEFT = (34, 211, 238)  # cian del panel
GLOW_RIGHT = (99, 102, 241)  # índigo
TITLE_COLOR = (255, 255, 255)
SMALL_COLOR = (200, 204, 222)
PAD = 64

# Alturas relativas de las barras del ecualizador.
EQ_BARS = (0.42, 0.86, 0.58, 1.0, 0.36, 0.7)
EQ_WIDTH = 18
EQ_GAP = 12
EQ_HEIGHT = 150


def _font(path: Path, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    try:
        return ImageFont.truetype(str(path), size)
    except OSError:
        for fallback in ("impact.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf"):
            try:
                return ImageFont.truetype(fallback, size)
            except OSError:
                continue
        return ImageFont.load_default()


def _glow(size: tuple[int, int], center: tuple[int, int], radius: int, color: tuple[int, int, int]) -> Image.Image:
    """Mancha de luz difuminada sobre fondo negro, para sumar con `screen`."""
    layer = Image.new("RGB", size, (0, 0, 0))
    x, y = center
    ImageDraw.Draw(layer).ellipse((x - radius, y - radius, x + radius, y + radius), fill=color)
    return layer.filter(ImageFilter.GaussianBlur(radius * 0.55))


def _screen(base: Image.Image, light: Image.Image, amount: float) -> Image.Image:
    """Suma la luz al fondo (modo trama) con la intensidad `amount`."""
    dimmed = Image.blend(Image.new("RGB", base.size, (0, 0, 0)), light, amount)
    return ImageChops.screen(base, dimmed)


def _spaced(draw: ImageDraw.ImageDraw, xy: tuple[int, int], text: str, font: ImageFont.ImageFont, spacing: int, fill) -> None:
    x, y = xy
    for char in text:
        draw.text((x, y), char, font=font, fill=fill)
        x += draw.textlength(char, font=font) + spacing


@lru_cache(maxsize=16)
def render_music_banner(server_name: str) -> bytes:
    """PNG con «CANAL DE MÚSICA», el nombre del server y un ecualizador."""
    image = Image.new("RGB", (WIDTH, HEIGHT), BACKGROUND)
    image = _screen(image, _glow(image.size, (40, HEIGHT + 40), 360, GLOW_LEFT), 0.75)
    image = _screen(image, _glow(image.size, (WIDTH - 20, -60), 380, GLOW_RIGHT), 0.85)

    draw = ImageDraw.Draw(image)
    title_font = _font(DISPLAY_FONT, 108)
    small_font = _font(SMALL_FONT, 24)

    title_lines = ("CANAL DE", "MÚSICA")
    line_height = 132
    bottom = HEIGHT - PAD
    first_top = bottom - line_height * len(title_lines) - 2
    for index, line in enumerate(title_lines):
        draw.text((PAD, first_top + index * line_height), line, font=title_font, fill=TITLE_COLOR)

    if server_name:
        _spaced(draw, (PAD + 4, first_top - 34), server_name.upper()[:40], small_font, 5, SMALL_COLOR)

    # Ecualizador en la esquina inferior derecha, apoyado en la misma línea que el título.
    right = WIDTH - PAD
    left = right - len(EQ_BARS) * EQ_WIDTH - (len(EQ_BARS) - 1) * EQ_GAP
    base_y = bottom - 6
    for index, level in enumerate(EQ_BARS):
        x = left + index * (EQ_WIDTH + EQ_GAP)
        top = base_y - int(EQ_HEIGHT * level)
        draw.rounded_rectangle((x, top, x + EQ_WIDTH, base_y), radius=5, fill=(236, 240, 255))

    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()
