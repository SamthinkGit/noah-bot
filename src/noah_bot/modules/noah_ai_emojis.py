"""Emojis de la app para el progreso de Noah AI: puntos de estado y un spinner animado.

Se dibujan con Pillow y se suben como emojis de la aplicación (no ocupan huecos de
emojis del servidor). Si algo falla se usan emojis normales.
"""

from __future__ import annotations

import io
import logging
import math

import discord
from PIL import Image, ImageDraw

_log = logging.getLogger(__name__)

SIZE = 96
COLORS = {
    "ok": (35, 165, 90),
    "deny": (218, 55, 60),
    "error": (240, 178, 50),
}
SPIN_COLOR = (240, 178, 50)
FALLBACK = {"run": "⏳", "ok": "🟢", "deny": "🔴", "error": "🟡"}
NAMES = {"run": "noah_ai_spin", "ok": "noah_ai_ok", "deny": "noah_ai_deny", "error": "noah_ai_warn"}

_emojis: dict[str, str] = {}
_loaded = False


def dot(status: str) -> str:
    return _emojis.get(status) or FALLBACK.get(status, "•")


def _dot_png(color: tuple[int, int, int]) -> bytes:
    scale = 4
    image = Image.new("RGBA", (SIZE * scale, SIZE * scale), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    radius = SIZE * scale * 0.22
    center = SIZE * scale / 2
    draw.ellipse((center - radius, center - radius, center + radius, center + radius), fill=(*color, 255))
    image = image.resize((SIZE, SIZE), Image.LANCZOS)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _spinner_gif() -> bytes:
    frames: list[Image.Image] = []
    scale = 4
    count = 12
    big = SIZE * scale
    radius = big * 0.26
    width = int(big * 0.075)
    center = big / 2
    box = (center - radius, center - radius, center + radius, center + radius)
    for index in range(count):
        image = Image.new("RGBA", (big, big), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        start = index * (360 / count)
        draw.arc(box, start=0, end=360, fill=(*SPIN_COLOR, 70), width=width)
        draw.arc(box, start=start, end=start + 250, fill=(*SPIN_COLOR, 255), width=width)
        # Remates redondos del arco.
        for angle in (start, start + 250):
            rad = math.radians(angle)
            x = center + radius * math.cos(rad) - width / 2 * math.cos(rad)
            y = center + radius * math.sin(rad) - width / 2 * math.sin(rad)
            draw.ellipse((x - width / 2, y - width / 2, x + width / 2, y + width / 2), fill=(*SPIN_COLOR, 255))
        small = image.resize((SIZE, SIZE), Image.LANCZOS)
        # GIF: un color transparente; el resto, sin semitransparencias.
        alpha = small.getchannel("A")
        mask = alpha.point(lambda value: 255 if value < 110 else 0)
        frame = small.convert("RGB").convert("P", palette=Image.ADAPTIVE, colors=255)
        frame.paste(255, mask=mask)
        frames.append(frame)
    buffer = io.BytesIO()
    frames[0].save(
        buffer,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=70,
        loop=0,
        transparency=255,
        disposal=2,
    )
    return buffer.getvalue()


def _image_for(status: str) -> bytes:
    return _spinner_gif() if status == "run" else _dot_png(COLORS[status])


async def load(client: discord.Client) -> None:
    """Busca o crea los emojis de la app. Solo hace trabajo la primera vez."""
    global _loaded
    if _loaded:
        return
    _loaded = True
    try:
        existing = {emoji.name: emoji for emoji in await client.fetch_application_emojis()}
        for status, name in NAMES.items():
            emoji = existing.get(name)
            if emoji is None:
                emoji = await client.create_application_emoji(name=name, image=_image_for(status))
            _emojis[status] = str(emoji)
    except (discord.HTTPException, ValueError, OSError) as error:
        _log.warning("No he podido preparar los emojis de Noah AI: %s", error)
