"""Tarjeta de `.noah monitor card`, dibujada con Pillow (mismo estilo que inspect)."""

import io
import math
import time

from PIL import Image, ImageDraw

from noah_bot.modules.inspect_card import (
    BACKGROUND,
    LINE,
    MUTED,
    TEXT,
    Badge,
    _draw_badges,
    _draw_spaced,
    _fit,
    _font,
)
from noah_bot.modules.monitor import (
    SAMPLE_SECONDS,
    WINDOW_SECONDS,
    Snapshot,
    format_count,
    format_duration,
    format_mb,
    health_tone,
    plural,
)

WIDTH, HEIGHT = 1200, 760
MARGIN = 56
CPU_COLOR = (87, 242, 135)
RAM_COLOR = (240, 178, 50)
GRID = (38, 40, 46)
HEALTH_LABEL = {"ok": "estable", "warn": "cargado", "bad": "al límite"}


def _stats_row(draw: ImageDraw.ImageDraw, y: float, stats: list[tuple[str, str, str]]) -> None:
    column = (WIDTH - 2 * MARGIN) / len(stats)
    label_font = _font("semibold", 18)
    value_font = _font("semibold", 40)
    sub_font = _font("regular", 21)
    for index, (label, value, sub) in enumerate(stats):
        x = MARGIN + index * column
        inner = column - 28
        if index:
            draw.line((x - 14, y + 2, x - 14, y + 104), fill=LINE, width=2)
        _draw_spaced(draw, (x, y), label.upper(), label_font, 1.8, MUTED)
        draw.text((x, y + 28), _fit(draw, value, value_font, inner), font=value_font, fill=TEXT)
        draw.text((x, y + 80), _fit(draw, sub, sub_font, inner), font=sub_font, fill=MUTED)


def _segments(points: list[tuple[float, float]], max_gap: float) -> list[list[tuple[float, float]]]:
    """Corta la línea donde faltan muestras (Noah estaba apagado)."""
    segments: list[list[tuple[float, float]]] = []
    for at, value in points:
        if segments and at - segments[-1][-1][0] <= max_gap:
            segments[-1].append((at, value))
        else:
            segments.append([(at, value)])
    return segments


def _downsample(points: list[tuple[float, float]], bins: int, start: float) -> list[tuple[float, float]]:
    """Media por columna para no dibujar 1.440 puntos en 1.000 píxeles."""
    span = WINDOW_SECONDS / bins
    grouped: dict[int, list[tuple[float, float]]] = {}
    for at, value in points:
        grouped.setdefault(int((at - start) // span), []).append((at, value))
    result = []
    for key in sorted(grouped):
        chunk = grouped[key]
        result.append((sum(a for a, _ in chunk) / len(chunk), sum(v for _, v in chunk) / len(chunk)))
    return result


def _nice_ceiling(value: float) -> float:
    if value <= 0:
        return 1
    magnitude = 10 ** math.floor(math.log10(value))
    for step in (1, 2, 2.5, 5, 10):
        if value <= step * magnitude:
            return step * magnitude
    return 10 * magnitude


def _draw_chart(image: Image.Image, box: tuple[int, int, int, int], snapshot: Snapshot) -> None:
    draw = ImageDraw.Draw(image)
    left, top, right, bottom = box
    width, height = right - left, bottom - top
    now = time.time()
    start = now - WINDOW_SECONDS
    small = _font("regular", 19)

    for fraction in (0, 0.5, 1):
        y = bottom - fraction * height
        draw.line((left, y, right, y), fill=GRID if fraction else LINE, width=2 if not fraction else 1)

    for label, fraction in (("hace 24 h", 0), ("hace 12 h", 0.5), ("ahora", 1)):
        anchor = "la" if fraction == 0 else ("ra" if fraction == 1 else "ma")
        draw.text((left + fraction * width, bottom + 12), label, font=small, fill=MUTED, anchor=anchor)

    samples = snapshot.samples
    if len(samples) < 2:
        draw.text(
            ((left + right) / 2, (top + bottom) / 2),
            "Recogiendo datos: la gráfica se llena con una muestra por minuto.",
            font=_font("regular", 24),
            fill=MUTED,
            anchor="mm",
        )
        return

    cpu_max = _nice_ceiling(max(10.0, max(s.cpu for s in samples) * 1.2))
    ram_values = [s.rss_mb for s in samples]
    ram_lo, ram_hi = min(ram_values), max(ram_values)
    pad = max(10.0, (ram_hi - ram_lo) * 0.15)
    ram_lo, ram_hi = max(0.0, ram_lo - pad), ram_hi + pad

    # Escalas: CPU a la izquierda, RAM a la derecha.
    draw.text((left - 12, top), f"{cpu_max:g}%", font=small, fill=CPU_COLOR, anchor="rm")
    draw.text((left - 12, bottom), "0%", font=small, fill=CPU_COLOR, anchor="rm")
    draw.text((right + 12, top), format_mb(ram_hi), font=small, fill=RAM_COLOR, anchor="lm")
    draw.text((right + 12, bottom), format_mb(ram_lo), font=small, fill=RAM_COLOR, anchor="lm")

    def to_xy(at: float, value: float, lo: float, hi: float) -> tuple[float, float]:
        x = left + (at - start) / WINDOW_SECONDS * width
        y = bottom - (value - lo) / (hi - lo) * height
        return x, min(bottom, max(top, y))

    bins = width // 4
    gap = SAMPLE_SECONDS * 3.5
    series = (
        ([(s.at, s.rss_mb) for s in samples], ram_lo, ram_hi, RAM_COLOR, False),
        ([(s.at, s.cpu) for s in samples], 0.0, cpu_max, CPU_COLOR, True),
    )

    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay)
    for points, lo, hi, color, filled in series:
        for segment in _segments(_downsample(points, bins, start), gap * 2):
            xy = [to_xy(at, value, lo, hi) for at, value in segment]
            if filled and len(xy) > 1:
                overlay_draw.polygon([(xy[0][0], bottom), *xy, (xy[-1][0], bottom)], fill=(*color, 38))
    image.alpha_composite(overlay)

    draw = ImageDraw.Draw(image)
    for points, lo, hi, color, _ in series:
        for segment in _segments(_downsample(points, bins, start), gap * 2):
            xy = [to_xy(at, value, lo, hi) for at, value in segment]
            if len(xy) == 1:
                x, y = xy[0]
                draw.ellipse((x - 3, y - 3, x + 3, y + 3), fill=color)
            else:
                draw.line(xy, fill=color, width=4, joint="curve")


def render_monitor_card(snapshot: Snapshot) -> bytes:
    image = Image.new("RGBA", (WIDTH, HEIGHT), (*BACKGROUND, 255))
    draw = ImageDraw.Draw(image)

    top = 44
    draw.text((MARGIN, top), "Noah", font=_font("semibold", 58), fill=TEXT)
    ping = f"{snapshot.latency_ms:.0f} ms" if snapshot.latency_ms is not None else "sin ping"
    sub = f"encendido hace {format_duration(snapshot.uptime_seconds)}   ·   {ping}   ·   PID {snapshot.pid}"
    draw.text((MARGIN, top + 80), sub, font=_font("regular", 25), fill=MUTED)

    tone = health_tone(snapshot)
    badges = [
        Badge(HEALTH_LABEL[tone], tone),
        Badge(plural(snapshot.boots_total, "despertar", "despertares"), "info"),
        Badge(
            plural(snapshot.reconnects_total, "reconexión", "reconexiones"),
            "warn" if snapshot.reconnects_today >= 5 else "info",
        ),
    ]
    if snapshot.errors_today:
        badges.append(Badge(plural(snapshot.errors_today, "error hoy", "errores hoy"), "bad"))
    _draw_badges(draw, MARGIN, top + 128, WIDTH - MARGIN, badges)

    divider = top + 200
    draw.line((MARGIN, divider, WIDTH - MARGIN, divider), fill=LINE, width=2)

    _stats_row(
        draw,
        divider + 26,
        [
            ("CPU", f"{snapshot.cpu:.1f}%", f"máx 24 h {snapshot.cpu_peak:.0f}%"),
            ("RAM", format_mb(snapshot.rss_mb), f"pico {format_mb(snapshot.rss_peak_mb)}"),
            ("Disco", format_mb(snapshot.disk_mb), f"{format_count(snapshot.disk_files)} archivos"),
            (
                "Comandos hoy",
                format_count(snapshot.commands_today),
                f"{format_count(snapshot.commands_total)} en total",
            ),
        ],
    )

    chart_title = divider + 160
    label_font = _font("semibold", 18)
    _draw_spaced(draw, (MARGIN, chart_title), "ÚLTIMAS 24 H", label_font, 1.8, MUTED)
    legend_font = _font("regular", 20)
    x = WIDTH - MARGIN
    for label, color in (("RAM", RAM_COLOR), ("CPU", CPU_COLOR)):
        width = draw.textlength(label, font=legend_font)
        draw.text((x - width, chart_title - 2), label, font=legend_font, fill=color)
        draw.ellipse((x - width - 20, chart_title + 6, x - width - 8, chart_title + 18), fill=color)
        x -= width + 46

    _draw_chart(image, (MARGIN + 52, chart_title + 44, WIDTH - MARGIN - 82, chart_title + 214), snapshot)
    draw = ImageDraw.Draw(image)

    footer = HEIGHT - 66
    draw.line((MARGIN, footer - 18, WIDTH - MARGIN, footer - 18), fill=LINE, width=2)
    footer_font = _font("regular", 21)
    top_names = " · ".join(name for name, _ in snapshot.top_commands[:4]) or "sin comandos todavía"
    draw.text((MARGIN, footer), _fit(draw, f"top: {top_names}", footer_font, 640), font=footer_font, fill=MUTED)
    right_text = (
        f"{format_count(snapshot.messages_today)} mensajes hoy   ·   "
        f"{snapshot.threads} hilos   ·   {snapshot.async_tasks} tareas"
    )
    draw.text((WIDTH - MARGIN, footer), right_text, font=footer_font, fill=MUTED, anchor="ra")

    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()

