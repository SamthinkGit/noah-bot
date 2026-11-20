"""Configuración de Noah AI: API key, modelo, canales, límites, preferencias y hilos.

Todo se guarda en `noah_ai.json` (ignorado por git). La API key nunca se muestra
entera: solo sus últimos caracteres.
"""

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

DEFAULT_MODEL = "gpt-6-luna"
DEFAULT_DAILY_LIMIT = 30
PERSONA_LIMIT = 400
LOCAL_TZ = ZoneInfo("Europe/Madrid")

LENGTHS: dict[str, str] = {
    "short": "Corta",
    "normal": "Normal",
    "long": "Detallada",
}


# Temas que el admin puede permitir o prohibir a Noah AI (para usuarios no admin).
TOPICS: dict[str, tuple[str, str]] = {
    "voz_actividad": ("Actividad de voz", "Última conexión y quién está en voz ahora"),
    "voz_medias": ("Medias de voz", "Cuántas horas suele estar alguien en VC"),
    "voz_companeros": ("Coincidencias en voz", "Con quién coincide más alguien en VC"),
    "relaciones": ("Con quién habla", "A quién escribe más y conversaciones entre dos"),
    "presentaciones": ("Presentaciones", "Si alguien se ha presentado y qué dijo"),
    "perfiles": ("Perfiles", "Fecha de entrada, roles y antigüedad de la cuenta"),
    "confesiones": ("Confesiones", "Opinar sobre quién pudo escribir una confesión"),
    "servidor": ("Actividad del servidor", "Últimos en entrar y gente en voz ahora"),
}


def local_now() -> datetime:
    return datetime.now(LOCAL_TZ)


def today_key() -> str:
    return local_now().strftime("%Y-%m-%d")


def mask_key(api_key: str | None) -> str:
    if not api_key:
        return "sin configurar"
    return f"sk-…{api_key[-4:]}" if len(api_key) > 8 else "sk-…"


class AiSettingsStore:
    def __init__(self, json_path: str = "noah_ai.json") -> None:
        self.json_path = Path(json_path)
        self._state: dict[str, Any] = self._load()

    # -- Persistencia -----------------------------------------------------------

    def _load(self) -> dict[str, Any]:
        state: dict[str, Any] = {"openai": {}, "langsmith": {}, "guilds": {}, "users": {}}
        if not self.json_path.exists():
            return state
        try:
            with self.json_path.open("r", encoding="utf-8") as file:
                payload = json.load(file)
        except (OSError, json.JSONDecodeError):
            return state
        if not isinstance(payload, dict):
            return state
        for key in state:
            if isinstance(payload.get(key), dict):
                state[key] = payload[key]
        # Versión anterior: marcaba modelos "sin razonamiento" para siempre por error.
        state["openai"].pop("no_reasoning_models", None)
        return state

    def _save(self) -> None:
        tmp_path = self.json_path.with_suffix(".tmp")
        with tmp_path.open("w", encoding="utf-8") as file:
            json.dump(self._state, file, indent=2, ensure_ascii=False)
        tmp_path.replace(self.json_path)
        # La API key vive aquí: que solo la pueda leer el usuario del bot.
        try:
            os.chmod(self.json_path, 0o600)
        except OSError:
            pass

    def _guild(self, guild_id: int) -> dict[str, Any]:
        return self._state["guilds"].setdefault(str(guild_id), {})

    @staticmethod
    def _int(value: Any) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    # -- OpenAI -----------------------------------------------------------------

    @property
    def api_key(self) -> str | None:
        stored = self._state["openai"].get("api_key")
        if isinstance(stored, str) and stored.strip():
            return stored.strip()
        env_key = os.getenv("OPENAI_API_KEY")
        return env_key.strip() if env_key and env_key.strip() else None

    @property
    def api_key_source(self) -> str:
        stored = self._state["openai"].get("api_key")
        if isinstance(stored, str) and stored.strip():
            return "config"
        return "env" if self.api_key else "none"

    def set_api_key(self, api_key: str | None) -> None:
        self._state["openai"]["api_key"] = api_key.strip() if api_key else None
        self._state["openai"].pop("key_status", None)
        self._save()

    @property
    def model(self) -> str:
        model = self._state["openai"].get("model")
        return model.strip() if isinstance(model, str) and model.strip() else DEFAULT_MODEL

    def set_model(self, model: str) -> None:
        self._state["openai"]["model"] = model.strip() or DEFAULT_MODEL
        self._save()

    def key_status(self) -> str | None:
        status = self._state["openai"].get("key_status")
        return status if isinstance(status, str) else None

    def set_key_status(self, status: str | None) -> None:
        self._state["openai"]["key_status"] = status
        self._save()

    # -- LangSmith (registro de llamadas) -------------------------------------------

    @property
    def langsmith_api_key(self) -> str | None:
        stored = self._state["langsmith"].get("api_key")
        if isinstance(stored, str) and stored.strip():
            return stored.strip()
        for name in ("LANGSMITH_API_KEY", "LANGCHAIN_API_KEY"):
            value = os.getenv(name)
            if value and value.strip():
                return value.strip()
        return None

    @property
    def langsmith_key_source(self) -> str:
        stored = self._state["langsmith"].get("api_key")
        if isinstance(stored, str) and stored.strip():
            return "config"
        return "env" if self.langsmith_api_key else "none"

    @property
    def langsmith_project(self) -> str:
        stored = self._state["langsmith"].get("project")
        if isinstance(stored, str) and stored.strip():
            return stored.strip()
        for name in ("LANGSMITH_PROJECT", "LANGCHAIN_PROJECT"):
            value = os.getenv(name)
            if value and value.strip():
                return value.strip()
        return "noah-bot"

    @property
    def langsmith_switch(self) -> bool:
        """Interruptor del admin (independiente de que haya key)."""
        return bool(self._state["langsmith"].get("enabled", True))

    @property
    def langsmith_enabled(self) -> bool:
        return bool(self.langsmith_api_key) and self.langsmith_switch

    def set_langsmith(self, *, api_key: str | None = None, project: str | None = None, enabled: bool | None = None) -> None:
        """`api_key=None` deja la key como está; `""` la borra."""
        data = self._state["langsmith"]
        if api_key is not None:
            data["api_key"] = api_key.strip() or None
        if project is not None:
            data["project"] = project.strip() or None
        if enabled is not None:
            data["enabled"] = enabled
        self._save()

    # -- Servidor ----------------------------------------------------------------

    def is_enabled(self, guild_id: int) -> bool:
        return bool(self._guild(guild_id).get("enabled", True))

    def set_enabled(self, guild_id: int, enabled: bool) -> None:
        self._guild(guild_id)["enabled"] = enabled
        self._save()

    def get_channel(self, guild_id: int, name: str) -> int | None:
        """`name`: `panel`, `threads` o `staff`."""
        return self._int(self._guild(guild_id).get(f"{name}_channel_id"))

    def set_channel(self, guild_id: int, name: str, channel_id: int | None) -> None:
        self._guild(guild_id)[f"{name}_channel_id"] = channel_id
        self._save()

    def get_panel_message(self, guild_id: int) -> tuple[int, int] | None:
        panel = self._guild(guild_id).get("panel_message")
        if not isinstance(panel, dict):
            return None
        channel_id, message_id = self._int(panel.get("channel_id")), self._int(panel.get("message_id"))
        if channel_id is None or message_id is None:
            return None
        return channel_id, message_id

    def set_panel_message(self, guild_id: int, channel_id: int, message_id: int) -> None:
        self._guild(guild_id)["panel_message"] = {"channel_id": channel_id, "message_id": message_id}
        self._save()

    def panel_guild_ids(self) -> list[int]:
        return [
            int(guild_id)
            for guild_id, data in self._state["guilds"].items()
            if isinstance(data, dict) and isinstance(data.get("panel_message"), dict)
        ]

    def readable_channels(self, guild_id: int) -> set[int]:
        raw = self._guild(guild_id).get("readable_channel_ids", [])
        return {value for item in raw if (value := self._int(item)) is not None}

    def set_readable_channels(self, guild_id: int, channel_ids: list[int]) -> None:
        self._guild(guild_id)["readable_channel_ids"] = sorted(set(channel_ids))
        self._save()

    def daily_limit(self, guild_id: int) -> int:
        value = self._int(self._guild(guild_id).get("daily_limit"))
        return DEFAULT_DAILY_LIMIT if value is None else max(0, value)

    def set_daily_limit(self, guild_id: int, limit: int) -> None:
        self._guild(guild_id)["daily_limit"] = max(0, limit)
        self._save()

    def disabled_topics(self, guild_id: int) -> set[str]:
        raw = self._guild(guild_id).get("disabled_topics", [])
        return {topic for topic in raw if topic in TOPICS} if isinstance(raw, list) else set()

    def enabled_topics(self, guild_id: int) -> set[str]:
        return set(TOPICS) - self.disabled_topics(guild_id)

    def set_enabled_topics(self, guild_id: int, topics: list[str]) -> None:
        self._guild(guild_id)["disabled_topics"] = sorted(set(TOPICS) - set(topics))
        self._save()

    # -- Uso diario ---------------------------------------------------------------

    def _usage(self, guild_id: int) -> dict[str, Any]:
        guild = self._guild(guild_id)
        usage = guild.get("usage")
        today = today_key()
        if not isinstance(usage, dict) or usage.get("date") != today:
            usage = {"date": today, "users": {}, "total": 0}
            guild["usage"] = usage
        return usage

    def used_today(self, guild_id: int, user_id: int) -> int:
        return int(self._usage(guild_id)["users"].get(str(user_id), 0))

    def total_today(self, guild_id: int) -> int:
        return int(self._usage(guild_id).get("total", 0))

    def add_usage(self, guild_id: int, user_id: int) -> int:
        usage = self._usage(guild_id)
        used = int(usage["users"].get(str(user_id), 0)) + 1
        usage["users"][str(user_id)] = used
        usage["total"] = int(usage.get("total", 0)) + 1
        self._save()
        return used

    # -- Preferencias de usuario ----------------------------------------------------

    def user_prefs(self, user_id: int) -> dict[str, Any]:
        raw = self._state["users"].get(str(user_id))
        raw = raw if isinstance(raw, dict) else {}
        length = raw.get("length")
        persona = raw.get("persona")
        return {
            "length": length if length in LENGTHS else "normal",
            "persona": persona[:PERSONA_LIMIT] if isinstance(persona, str) else "",
            "show_reasoning": bool(raw.get("show_reasoning", True)),
        }

    def set_user_prefs(self, user_id: int, *, length: str, persona: str, show_reasoning: bool) -> None:
        self._state["users"][str(user_id)] = {
            "length": length if length in LENGTHS else "normal",
            "persona": persona.strip()[:PERSONA_LIMIT],
            "show_reasoning": show_reasoning,
        }
        self._save()

    # -- Hilos ----------------------------------------------------------------------

    def _threads(self, guild_id: int) -> dict[str, Any]:
        threads = self._guild(guild_id).setdefault("threads", {})
        return threads if isinstance(threads, dict) else {}

    def register_thread(self, guild_id: int, thread_id: int, owner_id: int) -> None:
        self._threads(guild_id)[str(thread_id)] = {
            "owner_id": owner_id,
            "created_at": local_now().isoformat(),
            "strikes": 0,
            "questions": 0,
            "closed": False,
        }
        self._save()

    def get_thread(self, guild_id: int, thread_id: int) -> dict[str, Any] | None:
        data = self._threads(guild_id).get(str(thread_id))
        return data if isinstance(data, dict) else None

    def list_threads(self, guild_id: int) -> list[tuple[int, dict[str, Any]]]:
        items = [
            (int(thread_id), data)
            for thread_id, data in self._threads(guild_id).items()
            if isinstance(data, dict)
        ]
        items.sort(key=lambda item: str(item[1].get("created_at", "")), reverse=True)
        return items

    def last_thread_created(self, guild_id: int, owner_id: int) -> datetime | None:
        latest: datetime | None = None
        for _, data in self.list_threads(guild_id):
            if data.get("owner_id") != owner_id:
                continue
            try:
                created = datetime.fromisoformat(str(data.get("created_at")))
            except ValueError:
                continue
            if latest is None or created > latest:
                latest = created
        return latest

    def add_thread_question(self, guild_id: int, thread_id: int) -> None:
        data = self.get_thread(guild_id, thread_id)
        if data is not None:
            data["questions"] = int(data.get("questions", 0)) + 1
            data["last_activity"] = local_now().isoformat()
            self._save()

    def add_strike(self, guild_id: int, thread_id: int, reason: str) -> int:
        data = self.get_thread(guild_id, thread_id)
        if data is None:
            return 0
        data["strikes"] = int(data.get("strikes", 0)) + 1
        reasons = data.setdefault("strike_reasons", [])
        if isinstance(reasons, list):
            reasons.append(reason[:300])
        self._save()
        return int(data["strikes"])

    def set_thread_value(self, guild_id: int, thread_id: int, key: str, value: Any) -> None:
        data = self.get_thread(guild_id, thread_id)
        if data is not None:
            data[key] = value
            self._save()

    def close_thread(self, guild_id: int, thread_id: int, by: str = "noah", closer_id: int | None = None) -> None:
        """`by`: `noah` (por saltarse las reglas) o `user` (con el botón)."""
        data = self.get_thread(guild_id, thread_id)
        if data is not None:
            data["closed"] = True
            data["closed_by"] = by
            data["closed_by_id"] = closer_id
            data["closed_at"] = local_now().isoformat()
            self._save()
