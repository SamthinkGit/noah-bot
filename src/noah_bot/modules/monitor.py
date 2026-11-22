"""Monitor de Noah: consumo del proceso y estadísticas de uso.

Mide CPU, RAM y disco del propio proceso con psutil, guarda una muestra por minuto
(24 h) y cuenta arranques, reconexiones, comandos y errores. Todo se persiste en
`monitor_stats.json` para que sobreviva a los reinicios.
"""

import json
import os
import platform
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import psutil

from noah_bot.modules.daily_stats import current_day_key

SAMPLE_SECONDS = 60
WINDOW_SECONDS = 24 * 3600
MAX_SAMPLES = WINDOW_SECONDS // SAMPLE_SECONDS
MAX_EVENTS = 50
DAYS_KEPT = 30
SAVE_EVERY_SAMPLES = 5

# Referencia de las barras: no son límites reales, solo la escala del dibujo.
RAM_BUDGET_MB = int(os.getenv("NOAH_MONITOR_RAM_MB", "1024"))
DISK_BUDGET_MB = int(os.getenv("NOAH_MONITOR_DISK_MB", "1024"))

# Datos que Noah guarda en la carpeta desde la que se ejecuta.
DATA_DIRS = ("media", "billboard_posters", "sports_media")


@dataclass(slots=True)
class Sample:
    at: float
    cpu: float  # % de la máquina entera (0-100)
    rss_mb: float


@dataclass(slots=True)
class Snapshot:
    """Foto del momento, lista para pintar en texto o en tarjeta."""

    cpu: float
    cpu_peak: float
    rss_mb: float
    rss_peak_mb: float
    system_ram_mb: float
    disk_mb: float
    disk_files: int
    uptime_seconds: float
    latency_ms: float | None
    threads: int
    async_tasks: int
    pid: int
    python: str
    discord_py: str
    boots_total: int
    boots_today: int
    reconnects_total: int
    reconnects_today: int
    commands_total: int
    commands_today: int
    errors_total: int
    errors_today: int
    messages_today: int
    top_commands: list[tuple[str, int]]
    samples: list[Sample] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)


def _command_family(qualified_name: str) -> str:
    """`noah ai chat` -> `ai`, `noah ping` -> `ping`, `claim` -> `claim`."""
    parts = qualified_name.split()
    if len(parts) > 1 and parts[0] == "noah":
        return parts[1]
    return parts[0] if parts else "?"


def _folder_size(path: Path) -> tuple[int, int]:
    total = files = 0
    for root, _, names in os.walk(path):
        for name in names:
            try:
                total += (Path(root) / name).stat().st_size
                files += 1
            except OSError:
                continue
    return total, files


def measure_disk(base: Path | None = None) -> tuple[float, int]:
    """MB y número de archivos de datos de Noah (JSON sueltos + carpetas de media)."""
    base = base or Path.cwd()
    total = files = 0
    for path in base.glob("*.json"):
        try:
            total += path.stat().st_size
            files += 1
        except OSError:
            continue
    for name in DATA_DIRS:
        folder = base / name
        if folder.is_dir():
            size, count = _folder_size(folder)
            total += size
            files += count
    return total / (1024 * 1024), files


class MonitorStore:
    def __init__(self, json_path: str = "monitor_stats.json") -> None:
        self._path = Path(json_path)
        self._lock = threading.Lock()
        self._process = psutil.Process()
        self._process.cpu_percent(None)  # la primera lectura siempre es 0
        # Instancia aparte para las consultas: cada una guarda su propia base de CPU,
        # así una consulta no acorta la ventana de la muestra por minuto.
        self._live = psutil.Process()
        self._cpu_count = psutil.cpu_count() or 1
        self._started_at = self._process.create_time()
        self._boot_registered = False
        self._unsaved = 0
        self._state = self._load()

    # ---------- persistencia ----------

    def _load(self) -> dict[str, Any]:
        state: dict[str, Any] = {}
        if self._path.exists():
            try:
                state = json.loads(self._path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                state = {}
        state.setdefault("boots_total", 0)
        state.setdefault("reconnects_total", 0)
        state.setdefault("commands_total", 0)
        state.setdefault("errors_total", 0)
        state.setdefault("commands_by_name", {})
        state.setdefault("daily", {})
        state.setdefault("events", [])
        state.setdefault("samples", [])
        return state

    def save(self) -> None:
        with self._lock:
            payload = json.dumps(self._state, ensure_ascii=False)
            self._unsaved = 0
        tmp = self._path.with_suffix(".tmp")
        try:
            tmp.write_text(payload, encoding="utf-8")
            tmp.replace(self._path)
        except OSError:
            pass

    def _day(self, key: str | None = None) -> dict[str, int]:
        key = key or current_day_key()
        daily = self._state["daily"]
        day = daily.setdefault(
            key, {"boots": 0, "reconnects": 0, "commands": 0, "errors": 0, "messages": 0}
        )
        if len(daily) > DAYS_KEPT:
            for old in sorted(daily)[: len(daily) - DAYS_KEPT]:
                daily.pop(old, None)
        return day

    def _add_event(self, kind: str, detail: str = "") -> None:
        events = self._state["events"]
        events.append({"kind": kind, "at": time.time(), "detail": detail})
        del events[:-MAX_EVENTS]

    # ---------- eventos ----------

    def record_ready(self) -> None:
        """`on_ready`: el primero del proceso es un arranque; los siguientes, reconexiones."""
        with self._lock:
            if not self._boot_registered:
                self._boot_registered = True
                self._state["boots_total"] += 1
                self._day()["boots"] += 1
                self._add_event("boot")
            else:
                self._state["reconnects_total"] += 1
                self._day()["reconnects"] += 1
                self._add_event("ready", "sesión nueva")
        self.save()

    def record_resume(self) -> None:
        with self._lock:
            self._state["reconnects_total"] += 1
            self._day()["reconnects"] += 1
            self._add_event("resume", "sesión reanudada")
            self._unsaved += 1

    def record_command(self, qualified_name: str) -> None:
        with self._lock:
            family = _command_family(qualified_name)
            by_name = self._state["commands_by_name"]
            by_name[family] = int(by_name.get(family, 0)) + 1
            self._state["commands_total"] += 1
            self._day()["commands"] += 1
            self._unsaved += 1

    def record_error(self) -> None:
        with self._lock:
            self._state["errors_total"] += 1
            self._day()["errors"] += 1
            self._unsaved += 1

    def record_message(self) -> None:
        with self._lock:
            self._day()["messages"] += 1

    # ---------- muestreo ----------

    def _measure(self) -> tuple[float, float]:
        cpu = self._process.cpu_percent(None) / self._cpu_count
        rss_mb = self._process.memory_info().rss / (1024 * 1024)
        return round(min(cpu, 100.0), 2), round(rss_mb, 1)

    def sample(self) -> None:
        """Se llama cada minuto: guarda CPU y RAM y recorta la ventana de 24 h."""
        cpu, rss_mb = self._measure()
        now = time.time()
        with self._lock:
            samples = self._state["samples"]
            samples.append([round(now, 1), cpu, rss_mb])
            cutoff = now - WINDOW_SECONDS
            while samples and samples[0][0] < cutoff:
                samples.pop(0)
            del samples[:-MAX_SAMPLES]
            self._unsaved += 1
            should_save = self._unsaved >= SAVE_EVERY_SAMPLES
        if should_save:
            self.save()

    # ---------- lectura ----------

    def snapshot(self, latency: float | None, async_tasks: int) -> Snapshot:
        import discord  # solo para la versión

        # CPU instantánea medida durante 0,5 s (bloquea: llamar desde un hilo).
        cpu = self._live.cpu_percent(0.5) / self._cpu_count
        cpu = round(min(cpu, 100.0), 1)
        rss_mb = self._process.memory_info().rss / (1024 * 1024)
        disk_mb, disk_files = measure_disk()
        now = time.time()

        with self._lock:
            samples = [Sample(at, c, r) for at, c, r in self._state["samples"] if at >= now - WINDOW_SECONDS]
            today = dict(self._day())
            by_name = Counter({k: int(v) for k, v in self._state["commands_by_name"].items()})
            events = list(self._state["events"])
            boots_total = int(self._state["boots_total"])
            reconnects_total = int(self._state["reconnects_total"])
            commands_total = int(self._state["commands_total"])
            errors_total = int(self._state["errors_total"])

        cpu_peak = max([cpu, *(s.cpu for s in samples)])
        rss_peak = max([rss_mb, *(s.rss_mb for s in samples)])

        return Snapshot(
            cpu=cpu,
            cpu_peak=round(cpu_peak, 1),
            rss_mb=round(rss_mb, 1),
            rss_peak_mb=round(rss_peak, 1),
            system_ram_mb=psutil.virtual_memory().total / (1024 * 1024),
            disk_mb=round(disk_mb, 1),
            disk_files=disk_files,
            uptime_seconds=now - self._started_at,
            latency_ms=None if latency is None or latency != latency else latency * 1000,
            threads=self._process.num_threads(),
            async_tasks=async_tasks,
            pid=self._process.pid,
            python=platform.python_version(),
            discord_py=discord.__version__,
            boots_total=boots_total,
            boots_today=int(today.get("boots", 0)),
            reconnects_total=reconnects_total,
            reconnects_today=int(today.get("reconnects", 0)),
            commands_total=commands_total,
            commands_today=int(today.get("commands", 0)),
            errors_total=errors_total,
            errors_today=int(today.get("errors", 0)),
            messages_today=int(today.get("messages", 0)),
            top_commands=by_name.most_common(5),
            samples=samples,
            events=events,
        )


# ---------- formato compartido por la vista de texto y la tarjeta ----------


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days}d {hours:02d}h {minutes:02d}m"
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m"


def format_mb(value: float) -> str:
    if value >= 1024:
        return f"{value / 1024:.1f}".removesuffix(".0") + " GB"
    return f"{value:.0f} MB"


def format_count(value: int) -> str:
    return f"{value:,}".replace(",", ".")


def plural(count: int, singular: str, many: str) -> str:
    return f"{format_count(count)} {singular if count == 1 else many}"


def hourly_buckets(samples: list[Sample], attr: str, mode: str = "avg", hours: int = 24) -> list[float | None]:
    """Agrupa las muestras en `hours` tramos de una hora (el último acaba ahora)."""
    now = time.time()
    buckets: list[list[float]] = [[] for _ in range(hours)]
    for sample in samples:
        index = hours - 1 - int((now - sample.at) // 3600)
        if 0 <= index < hours:
            buckets[index].append(getattr(sample, attr))
    result: list[float | None] = []
    for values in buckets:
        if not values:
            result.append(None)
        elif mode == "max":
            result.append(max(values))
        else:
            result.append(sum(values) / len(values))
    return result


def health_tone(snapshot: Snapshot) -> str:
    """ok / warn / bad según CPU y RAM respecto a su escala."""
    ram_ratio = snapshot.rss_mb / RAM_BUDGET_MB
    if snapshot.cpu >= 60 or ram_ratio >= 0.9:
        return "bad"
    if snapshot.cpu >= 25 or ram_ratio >= 0.6:
        return "warn"
    return "ok"


def data_since(snapshot: Snapshot) -> datetime | None:
    if not snapshot.samples:
        return None
    return datetime.fromtimestamp(snapshot.samples[0].at).astimezone()
