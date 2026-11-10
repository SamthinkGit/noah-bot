"""Banner de la agenda deportiva (una pista vista desde arriba), dibujado con Pillow."""

import io
from functools import lru_cache

from PIL import Image, ImageDraw

from noah_bot.modules.billboard_banner import _draw_spaced, _font

WIDTH, HEIGHT = 1100, 260
BACKGROUND = (27, 79, 138)
LINES = (133, 183, 235)
TITLE_COLOR = (255, 255, 255)
SUBTITLE_COLOR = (181, 212, 244)
MARGIN = 28
LINE_WIDTH = 5


@lru_cache(maxsize=16)
def render_banner(subtitle: str) -> bytes:
    """PNG con «AGENDA DEPORTIVA» sobre una pista y el subtítulo (rango de días)."""
    image = Image.new("RGB", (WIDTH, HEIGHT), BACKGROUND)
    draw = ImageDraw.Draw(image)

    # Líneas de la pista: borde, medio campo, círculo central y áreas.
    draw.rectangle(
        (MARGIN, MARGIN, WIDTH - MARGIN, HEIGHT - MARGIN), outline=LINES, width=LINE_WIDTH
    )
    draw.line((WIDTH // 2, MARGIN, WIDTH // 2, HEIGHT - MARGIN), fill=LINES, width=LINE_WIDTH)
    radius = 52
    draw.ellipse(
        (WIDTH // 2 - radius, HEIGHT // 2 - radius, WIDTH // 2 + radius, HEIGHT // 2 + radius),
        outline=LINES,
        width=LINE_WIDTH,
    )
    box_height = 120
    box_top = (HEIGHT - box_height) // 2
    draw.rectangle(
        (MARGIN, box_top, MARGIN + 90, box_top + box_height), outline=LINES, width=LINE_WIDTH
    )
    draw.rectangle(
        (WIDTH - MARGIN - 90, box_top, WIDTH - MARGIN, box_top + box_height),
        outline=LINES,
        width=LINE_WIDTH,
    )

    # Banda detrás del texto para que se lea sobre las líneas.
    draw.rectangle((150, 62, WIDTH - 150, 200), fill=BACKGROUND)
    _draw_spaced(draw, WIDTH // 2, 66, "AGENDA DEPORTIVA", _font(84), 8, TITLE_COLOR)
    _draw_spaced(draw, WIDTH // 2, 168, subtitle.upper(), _font(26), 5, SUBTITLE_COLOR)

    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()
