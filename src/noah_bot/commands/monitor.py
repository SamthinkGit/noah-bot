"""`.noah monitor`: consumo de Noah (CPU, RAM, disco) y estadísticas de uso."""

import asyncio
import io

import discord
from discord.ext import commands, tasks

from noah_bot.modules.bot_context import get_bot_context
from noah_bot.modules.discord_formatter import EmbedTable
from noah_bot.modules.monitor import (
    DISK_BUDGET_MB,
    RAM_BUDGET_MB,
    SAMPLE_SECONDS,
    Snapshot,
    format_count,
    format_duration,
    format_mb,
    health_tone,
    hourly_buckets,
    plural,
)
from noah_bot.modules.monitor_card import render_monitor_card

BAR_WIDTH = 16
SPARK_CHARS = "▁▂▃▄▅▆▇█"
TONE_COLORS = {
    "ok": discord.Color.from_rgb(87, 242, 135),
    "warn": discord.Color.from_rgb(250, 196, 82),
    "bad": discord.Color.from_rgb(250, 119, 124),
}
TONE_LABEL = {"ok": "estable", "warn": "cargado", "bad": "al límite"}
EVENT_LABEL = {
    "boot": ("🟢", "Arranque"),
    "ready": ("🔁", "Reconexión (sesión nueva)"),
    "resume": ("↪️", "Reconexión (reanudada)"),
}


def _bar(value: float, maximum: float) -> str:
    ratio = 0.0 if maximum <= 0 else max(0.0, min(1.0, value / maximum))
    filled = round(ratio * BAR_WIDTH)
    if value > 0 and filled == 0:
        filled = 1
    return "█" * filled + "░" * (BAR_WIDTH - filled)


def _sparkline(values: list[float | None], lo: float | None = None, hi: float | None = None) -> str:
    present = [value for value in values if value is not None]
    if not present:
        return "·" * len(values)
    lo = min(present) if lo is None else lo
    hi = max(present) if hi is None else hi
    span = hi - lo
    chars = []
    for value in values:
        if value is None:
            chars.append("·")
        elif span <= 0:
            chars.append(SPARK_CHARS[0])
        else:
            index = int((value - lo) / span * (len(SPARK_CHARS) - 1))
            chars.append(SPARK_CHARS[max(0, min(len(SPARK_CHARS) - 1, index))])
    return "".join(chars)


def _top_line(snapshot: Snapshot) -> str:
    total = max(1, snapshot.commands_total)
    parts = [f"{name} {count * 100 / total:.0f}%" for name, count in snapshot.top_commands[:4]]
    return " · ".join(parts) if parts else "sin comandos todavía"


def build_text_embed(snapshot: Snapshot) -> discord.Embed:
    tone = health_tone(snapshot)
    ping = f"{snapshot.latency_ms:.0f} ms" if snapshot.latency_ms is not None else "—"

    cpu_hours = hourly_buckets(snapshot.samples, "cpu", mode="max")
    ram_hours = hourly_buckets(snapshot.samples, "rss_mb")
    cpu_scale = max(10.0, max((v for v in cpu_hours if v is not None), default=0.0))

    lines = [
        f"● {TONE_LABEL[tone]}  uptime {format_duration(snapshot.uptime_seconds)}  ping {ping}",
        "",
        f"CPU   {_bar(snapshot.cpu, 100)} {snapshot.cpu:5.1f}%  máx {snapshot.cpu_peak:.0f}%",
        f"RAM   {_bar(snapshot.rss_mb, RAM_BUDGET_MB)} {format_mb(snapshot.rss_mb):>6}  de {format_mb(RAM_BUDGET_MB)}",
        f"DISCO {_bar(snapshot.disk_mb, DISK_BUDGET_MB)} {format_mb(snapshot.disk_mb):>6}  {format_count(snapshot.disk_files)} arch.",
        "",
        f"CPU 24h  {_sparkline(cpu_hours, 0, cpu_scale)}",
        f"RAM 24h  {_sparkline(ram_hours)}",
        "",
        f"despertares  {format_count(snapshot.boots_total):>6} · {snapshot.boots_today} hoy",
        f"reconexiones {format_count(snapshot.reconnects_total):>6} · {snapshot.reconnects_today} hoy",
        f"comandos     {format_count(snapshot.commands_total):>6} · {snapshot.commands_today} hoy",
        f"errores      {format_count(snapshot.errors_total):>6} · {snapshot.errors_today} hoy",
        f"mensajes hoy {format_count(snapshot.messages_today):>6}",
        "",
        f"top  {_top_line(snapshot)}",
    ]

    embed = discord.Embed(
        title="Noah · monitor",
        description="```\n" + "\n".join(lines) + "\n```",
        color=TONE_COLORS[tone],
    )
    embed.set_footer(
        text=(
            f"Python {snapshot.python} · discord.py {snapshot.discord_py} · PID {snapshot.pid} · "
            f"{snapshot.threads} hilos · {snapshot.async_tasks} tareas\n"
            "· = hora sin datos · .noah monitor card para la gráfica"
        )
    )
    return embed


def build_wakes_embed(snapshot: Snapshot, limit: int = 12) -> discord.Embed:
    embed = discord.Embed(
        title="Noah · despertares",
        description=(
            f"{plural(snapshot.boots_total, 'arranque', 'arranques')} "
            f"({snapshot.boots_today} hoy) y "
            f"{plural(snapshot.reconnects_total, 'reconexión', 'reconexiones')} "
            f"({snapshot.reconnects_today} hoy).\n"
            f"Encendido ahora desde hace **{format_duration(snapshot.uptime_seconds)}**."
        ),
        color=discord.Color.blurple(),
    )
    events = list(reversed(snapshot.events))[:limit]
    if events:
        rows = []
        for event in events:
            emoji, label = EVENT_LABEL.get(event.get("kind", ""), ("•", event.get("kind", "?")))
            at = int(event.get("at", 0))
            rows.append(f"{emoji} {label} · <t:{at}:f> (<t:{at}:R>)")
        embed.add_field(name=f"Últimos {len(events)}", value="\n".join(rows), inline=False)
    else:
        embed.add_field(name="Últimos", value="Sin registros todavía.", inline=False)
    embed.set_footer(
        text="Arranque = el proceso se inicia · Reconexión = se cae la conexión con Discord y vuelve"
    )
    return embed


async def _take_snapshot(bot: commands.Bot) -> Snapshot:
    store = get_bot_context(bot).monitor
    return await asyncio.to_thread(store.snapshot, bot.latency, len(asyncio.all_tasks()))


def register_monitor_commands(bot: commands.Bot, noah_group: commands.Group) -> None:
    @tasks.loop(seconds=SAMPLE_SECONDS)
    async def monitor_sampler() -> None:
        await asyncio.to_thread(get_bot_context(bot).monitor.sample)

    @monitor_sampler.error
    async def monitor_sampler_error(error: BaseException) -> None:
        _ = error
        monitor_sampler.restart()

    @bot.listen("on_ready")
    async def _monitor_ready() -> None:
        await asyncio.to_thread(get_bot_context(bot).monitor.record_ready)
        if not monitor_sampler.is_running():
            monitor_sampler.start()

    @bot.listen("on_resumed")
    async def _monitor_resumed() -> None:
        get_bot_context(bot).monitor.record_resume()

    @bot.listen("on_command")
    async def _monitor_command(ctx: commands.Context) -> None:
        if ctx.command is not None:
            get_bot_context(bot).monitor.record_command(ctx.command.qualified_name)

    @bot.listen("on_command_error")
    async def _monitor_command_error(ctx: commands.Context, error: commands.CommandError) -> None:
        _ = ctx
        if isinstance(error, commands.CommandNotFound):
            return
        get_bot_context(bot).monitor.record_error()

    @bot.listen("on_message")
    async def _monitor_message(message: discord.Message) -> None:
        if not message.author.bot:
            get_bot_context(bot).monitor.record_message()

    @noah_group.group(name="monitor", invoke_without_command=True)
    async def monitor(ctx: commands.Context) -> None:
        async with ctx.typing():
            snapshot = await _take_snapshot(ctx.bot)
        await ctx.send(embed=build_text_embed(snapshot))

    @monitor.command(name="card")
    async def monitor_card(ctx: commands.Context) -> None:
        async with ctx.typing():
            snapshot = await _take_snapshot(ctx.bot)
            png = await asyncio.to_thread(render_monitor_card, snapshot)
        await ctx.send(file=discord.File(io.BytesIO(png), filename="noah_monitor.png"))

    @monitor.command(name="wakes")
    async def monitor_wakes(ctx: commands.Context) -> None:
        snapshot = await _take_snapshot(ctx.bot)
        await ctx.send(embed=build_wakes_embed(snapshot))

    @monitor.command(name="help")
    async def monitor_help(ctx: commands.Context) -> None:
        chart = EmbedTable(headers=["Command", "Description"], title="Noah Monitor")
        chart.add_row([".noah monitor", "CPU, RAM, disco y uso en texto (vista terminal)."])
        chart.add_row([".noah monitor card", "Tarjeta con gráfica de CPU y RAM de 24 h."])
        chart.add_row([".noah monitor wakes", "Últimos arranques y reconexiones."])
        await ctx.send(embed=chart.render())
