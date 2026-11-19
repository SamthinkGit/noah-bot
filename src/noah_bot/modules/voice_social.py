"""Registro detallado de llamadas: con quién coincide cada persona y cuánto suele estar.

Va en un fichero aparte (`voice_social.json`) y NO toca `voice_stats.json` ni el
sistema de niveles de VC: solo lee quién está conectado cada minuto.

Cuenta igual que los stats de voz: no cuenta a quien está ensordecido, a los bots,
el canal AFK ni los canales excluidos de `.noah vc`.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from itertools import combinations
from pathlib import Path
from typing import Any, Iterable

from noah_bot.modules.ai_settings import LOCAL_TZ

DAILY_KEEP_DAYS = 120
MAX_TICK_SECONDS = 180
SAVE_EVERY_SECONDS = 120


def _today() -> str:
    return datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")


class VoiceSocialStore:
    def __init__(self, json_path: str = "voice_social.json") -> None:
        self.json_path = Path(json_path)
        self._state: dict[str, Any] = self._load()
        self._last_tick: float | None = None
        self._present: dict[int, set[int]] = {}
        self._dirty = False
        self._last_save = time.monotonic()

    def _load(self) -> dict[str, Any]:
        state: dict[str, Any] = {"since": _today(), "guilds": {}}
        if not self.json_path.exists():
            return state
        try:
            with self.json_path.open("r", encoding="utf-8") as file:
                payload = json.load(file)
        except (OSError, json.JSONDecodeError):
            return state
        if isinstance(payload, dict) and isinstance(payload.get("guilds"), dict):
            state["guilds"] = payload["guilds"]
            state["since"] = payload.get("since") or state["since"]
        return state

    def save(self, force: bool = False) -> None:
        if not self._dirty and not force:
            return
        tmp_path = self.json_path.with_suffix(".tmp")
        with tmp_path.open("w", encoding="utf-8") as file:
            json.dump(self._state, file, ensure_ascii=False)
        tmp_path.replace(self.json_path)
        self._dirty = False
        self._last_save = time.monotonic()

    def _guild(self, guild_id: int) -> dict[str, Any]:
        guild = self._state["guilds"].setdefault(str(guild_id), {})
        guild.setdefault("users", {})
        guild.setdefault("pairs", {})
        return guild

    @property
    def since(self) -> str:
        return str(self._state.get("since"))

    # -- Registro -------------------------------------------------------------------

    def tick(self, rooms: dict[int, Iterable[Iterable[int]]]) -> None:
        """`rooms`: guild_id -> lista de canales, cada uno con los ids de quien cuenta.

        Se llama cada minuto. Suma el tiempo transcurrido a cada persona y a cada
        pareja que comparte canal.
        """
        now = time.monotonic()
        elapsed = 0 if self._last_tick is None else min(now - self._last_tick, MAX_TICK_SECONDS)
        self._last_tick = now
        today = _today()

        for guild_id, channels in rooms.items():
            guild = self._guild(guild_id)
            previous = self._present.get(guild_id, set())
            present: set[int] = set()
            for channel in channels:
                members = sorted(set(channel))
                present.update(members)
                if elapsed <= 0:
                    continue
                for user_id in members:
                    user = guild["users"].setdefault(str(user_id), {"daily": {}, "sessions": 0})
                    daily = user["daily"]
                    daily[today] = int(daily.get(today, 0)) + int(elapsed)
                    # Solo con más gente cuenta como "coincidir".
                    if len(members) > 1:
                        user["shared"] = int(user.get("shared", 0)) + int(elapsed)
                for first, second in combinations(members, 2):
                    key = f"{first}:{second}"
                    guild["pairs"][key] = int(guild["pairs"].get(key, 0)) + int(elapsed)
            for user_id in present - previous:
                user = guild["users"].setdefault(str(user_id), {"daily": {}, "sessions": 0})
                user["sessions"] = int(user.get("sessions", 0)) + 1
                starts = user.setdefault("sessions_daily", {})
                starts[today] = int(starts.get(today, 0)) + 1
            self._present[guild_id] = present
            if elapsed > 0 or present != previous:
                self._dirty = True

        if time.monotonic() - self._last_save >= SAVE_EVERY_SECONDS:
            self._prune()
            self.save()

    def _prune(self) -> None:
        cutoff = (datetime.now(LOCAL_TZ) - timedelta(days=DAILY_KEEP_DAYS)).strftime("%Y-%m-%d")
        for guild in self._state["guilds"].values():
            for user in guild.get("users", {}).values():
                for key in ("daily", "sessions_daily"):
                    daily = user.get(key, {})
                    for day in [day for day in daily if day < cutoff]:
                        del daily[day]

    # -- Consultas -------------------------------------------------------------------

    def averages(self, guild_id: int, user_id: int, days: int = 30) -> dict[str, Any] | None:
        user = self._guild(guild_id)["users"].get(str(user_id))
        if not isinstance(user, dict):
            return None
        daily: dict[str, int] = user.get("daily", {})
        cutoff = (datetime.now(LOCAL_TZ) - timedelta(days=days - 1)).strftime("%Y-%m-%d")
        recent = {day: seconds for day, seconds in daily.items() if day >= cutoff}
        total = sum(recent.values())
        active_days = sum(1 for seconds in recent.values() if seconds >= 60)
        since = max(self.since, cutoff)
        tracked_days = max(1, (datetime.now(LOCAL_TZ).date() - datetime.strptime(since, "%Y-%m-%d").date()).days + 1)
        sessions = sum(
            int(count) for day, count in user.get("sessions_daily", {}).items() if day >= cutoff
        )
        return {
            "window_days": min(days, tracked_days),
            "total_seconds": total,
            "active_days": active_days,
            "avg_per_day": total / min(days, tracked_days),
            "avg_per_active_day": total / active_days if active_days else 0,
            "avg_session": total / sessions if sessions else 0,
            "shared_seconds": int(user.get("shared", 0)),
            "since": self.since,
        }

    def companions(self, guild_id: int, user_id: int, limit: int = 8) -> list[tuple[int, int]]:
        found: list[tuple[int, int]] = []
        for key, seconds in self._guild(guild_id)["pairs"].items():
            first, _, second = key.partition(":")
            if str(user_id) == first:
                found.append((int(second), int(seconds)))
            elif str(user_id) == second:
                found.append((int(first), int(seconds)))
        found.sort(key=lambda item: item[1], reverse=True)
        return found[:limit]


def format_duration(seconds: float) -> str:
    minutes = int(seconds // 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes} min"
