"""Expediente de moderación: mensajes marcados, borrados, evidencias y notas.

Cada caso se guarda en `modlog.json` por servidor con un número correlativo. Las
copias de los adjuntos viven en el canal de logs del staff (Noah los resube allí),
así que un caso sigue teniendo sus imágenes aunque el mensaje original se borre.

Tipos de caso:
- ``flag``: mensaje marcado (se queda en el canal).
- ``delete``: mensaje marcado y eliminado.
- ``evidence``: mensaje guardado como evidencia, sin contar como infracción.
- ``note``: nota sobre el usuario (texto, capturas, enlaces o momentos).
"""

import json
import os
import threading
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

KIND_FLAG = "flag"
KIND_DELETE = "delete"
KIND_EVIDENCE = "evidence"
KIND_NOTE = "note"

KINDS: dict[str, tuple[str, str]] = {
    KIND_FLAG: ("🚩", "Marcado"),
    KIND_DELETE: ("🗑️", "Eliminado"),
    KIND_EVIDENCE: ("📎", "Evidencia"),
    KIND_NOTE: ("📝", "Nota"),
}
INFRACTION_KINDS = frozenset({KIND_FLAG, KIND_DELETE})

SEVERITIES: dict[str, tuple[str, str]] = {
    "leve": ("🟡", "Leve"),
    "media": ("🟠", "Media"),
    "grave": ("🔴", "Grave"),
}


def kind_label(kind: str) -> str:
    emoji, label = KINDS.get(kind, ("•", kind))
    return f"{emoji} {label}"


def severity_label(severity: str | None) -> str:
    if not severity or severity not in SEVERITIES:
        return ""
    emoji, label = SEVERITIES[severity]
    return f"{emoji} {label}"


class ModlogStore:
    def __init__(self, json_path: str = "modlog.json") -> None:
        self._path = Path(json_path)
        self._lock = threading.RLock()
        self._state = self._load()

    # ---------- persistencia ----------

    def _load(self) -> dict[str, Any]:
        state: dict[str, Any] = {}
        if self._path.exists():
            try:
                state = json.loads(self._path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                state = {}
        state.setdefault("guilds", {})
        return state

    def _save(self) -> None:
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self._path)

    def _guild(self, guild_id: int) -> dict[str, Any]:
        guild = self._state["guilds"].setdefault(str(guild_id), {})
        guild.setdefault("log_channel_id", None)
        guild.setdefault("next_id", 1)
        guild.setdefault("records", [])
        return guild

    # ---------- configuración ----------

    def get_log_channel(self, guild_id: int) -> int | None:
        with self._lock:
            value = self._guild(guild_id)["log_channel_id"]
            return int(value) if value else None

    def set_log_channel(self, guild_id: int, channel_id: int | None) -> None:
        with self._lock:
            self._guild(guild_id)["log_channel_id"] = channel_id
            self._save()

    # ---------- casos ----------

    def add_record(self, guild_id: int, record: dict[str, Any]) -> dict[str, Any]:
        """Guarda un caso nuevo y le asigna número y fecha."""
        with self._lock:
            guild = self._guild(guild_id)
            stored = dict(record)
            stored["id"] = int(guild["next_id"])
            stored.setdefault("created_at", int(time.time()))
            guild["next_id"] = stored["id"] + 1
            guild["records"].append(stored)
            self._save()
            return dict(stored)

    def update_record(self, guild_id: int, record_id: int, **fields: Any) -> dict[str, Any] | None:
        with self._lock:
            for record in self._guild(guild_id)["records"]:
                if int(record.get("id", 0)) == record_id:
                    record.update(fields)
                    self._save()
                    return dict(record)
        return None

    def get_record(self, guild_id: int, record_id: int) -> dict[str, Any] | None:
        with self._lock:
            for record in self._guild(guild_id)["records"]:
                if int(record.get("id", 0)) == record_id:
                    return dict(record)
        return None

    def remove_record(self, guild_id: int, record_id: int) -> dict[str, Any] | None:
        with self._lock:
            records = self._guild(guild_id)["records"]
            for index, record in enumerate(records):
                if int(record.get("id", 0)) == record_id:
                    removed = records.pop(index)
                    self._save()
                    return dict(removed)
        return None

    def records(
        self,
        guild_id: int,
        *,
        user_ids: Iterable[int] | None = None,
        kinds: Iterable[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Casos del servidor, del más reciente al más antiguo."""
        users = {int(uid) for uid in user_ids} if user_ids else None
        wanted = set(kinds) if kinds else None
        with self._lock:
            result = [
                dict(record)
                for record in self._guild(guild_id)["records"]
                if (users is None or int(record.get("user_id", 0)) in users)
                and (wanted is None or record.get("kind") in wanted)
            ]
        result.sort(key=lambda record: (int(record.get("created_at", 0)), int(record.get("id", 0))), reverse=True)
        return result

    def records_for_message(self, guild_id: int, message_id: int) -> list[dict[str, Any]]:
        with self._lock:
            return [
                dict(record)
                for record in self._guild(guild_id)["records"]
                if int((record.get("message") or {}).get("id", 0) or 0) == message_id
            ]

    def summary(self, guild_id: int, user_id: int) -> dict[str, Any]:
        records = self.records(guild_id, user_ids=[user_id])
        counts = {kind: 0 for kind in KINDS}
        severities = {key: 0 for key in SEVERITIES}
        for record in records:
            counts[record.get("kind", "")] = counts.get(record.get("kind", ""), 0) + 1
            if record.get("kind") in INFRACTION_KINDS and record.get("severity") in severities:
                severities[record["severity"]] += 1
        infractions = [record for record in records if record.get("kind") in INFRACTION_KINDS]
        return {
            "total": len(records),
            "counts": counts,
            "infractions": len(infractions),
            "severities": severities,
            "last_infraction_at": infractions[0].get("created_at") if infractions else None,
        }
