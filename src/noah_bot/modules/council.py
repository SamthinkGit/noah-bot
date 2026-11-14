"""Consejo temporal: datos y utilidades (sin nada de Discord).

Cada servidor tiene una configuración (rol del consejo, canal fijo del consejo,
categoría donde se crea si no existe y canal de actas) y, como mucho, un consejo
activo con los excluidos y sus polls. Los votos se guardan con el id de quien vota para que los
admins puedan ver quién votó qué; al finalizar el consejo se borra todo.
"""

import json
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

POLL_OPTIONS_MIN = 2
POLL_OPTIONS_MAX = 4
OPTION_MAX_LENGTH = 80
QUESTION_MAX_LENGTH = 200

_OPTION_PREFIX = re.compile(r"^(?:[-•*·]|\d+[.)-])\s*")

VOTE_OK = "ok"
VOTE_ALREADY = "already"
VOTE_CLOSED = "closed"
VOTE_MISSING = "missing"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def parse_options(text: str) -> list[str]:
    """Opciones del poll, una por línea (se quitan viñetas como «-» o «1.»).

    Lanza `ValueError` con un mensaje para el usuario si no son válidas.
    """
    options: list[str] = []
    seen: set[str] = set()
    for line in (text or "").splitlines():
        option = _OPTION_PREFIX.sub("", " ".join(line.split())).strip()
        if not option:
            continue
        if len(option) > OPTION_MAX_LENGTH:
            raise ValueError(f"Cada opción puede tener como mucho {OPTION_MAX_LENGTH} caracteres.")
        key = option.casefold()
        if key in seen:
            raise ValueError(f"La opción «{option}» está repetida.")
        seen.add(key)
        options.append(option)
    if len(options) < POLL_OPTIONS_MIN:
        raise ValueError("Escribe al menos 2 opciones, una por línea.")
    if len(options) > POLL_OPTIONS_MAX:
        raise ValueError("Como mucho 4 opciones.")
    return options


def poll_votes(poll: dict[str, Any]) -> dict[int, int]:
    """Votos del poll como {id de usuario: índice de la opción}."""
    options = poll.get("options") or []
    votes = poll.get("votes")
    if not isinstance(votes, dict):
        return {}
    result: dict[int, int] = {}
    for user_key, index in votes.items():
        try:
            user_id, option = int(user_key), int(index)
        except (TypeError, ValueError):
            continue
        if 0 <= option < len(options):
            result[user_id] = option
    return result


def tally(poll: dict[str, Any]) -> list[int]:
    counts = [0] * len(poll.get("options") or [])
    for option in poll_votes(poll).values():
        counts[option] += 1
    return counts


def winners(poll: dict[str, Any]) -> list[int]:
    """Índices de las opciones con más votos (varias si hay empate, ninguna sin votos)."""
    counts = tally(poll)
    best = max(counts, default=0)
    if best == 0:
        return []
    return [index for index, count in enumerate(counts) if count == best]


class CouncilStore:
    """Consejo por servidor: configuración y el consejo activo con sus polls."""

    def __init__(self, json_path: str = "council.json") -> None:
        self.json_path = Path(json_path)
        self._guilds: dict[str, dict[str, Any]] = self._load()

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self.json_path.exists():
            return {}
        try:
            with self.json_path.open("r", encoding="utf-8") as file:
                payload = json.load(file)
        except (OSError, json.JSONDecodeError):
            return {}

        guilds = payload.get("guilds") if isinstance(payload, dict) else None
        if not isinstance(guilds, dict):
            return {}
        return {
            str(guild_key): guild_data
            for guild_key, guild_data in guilds.items()
            if isinstance(guild_data, dict)
        }

    def _save(self) -> None:
        with self.json_path.open("w", encoding="utf-8") as file:
            json.dump({"guilds": self._guilds}, file, indent=2, ensure_ascii=False)

    def _guild_data(self, guild_id: int) -> dict[str, Any]:
        return self._guilds.setdefault(str(guild_id), {})

    def _get_int(self, guild_id: int, key: str) -> int | None:
        guild_data = self._guilds.get(str(guild_id))
        if not isinstance(guild_data, dict):
            return None
        try:
            return int(guild_data[key])
        except (KeyError, TypeError, ValueError):
            return None

    def _set_int(self, guild_id: int, key: str, value: int | None) -> None:
        guild_data = self._guild_data(guild_id)
        if value is None:
            guild_data.pop(key, None)
        else:
            guild_data[key] = int(value)
        self._save()

    def guild_ids(self) -> list[int]:
        guild_ids: list[int] = []
        for guild_key in self._guilds:
            try:
                guild_ids.append(int(guild_key))
            except ValueError:
                continue
        return guild_ids

    # -- Configuración ------------------------------------------------------

    def get_role_id(self, guild_id: int) -> int | None:
        return self._get_int(guild_id, "role_id")

    def set_role(self, guild_id: int, role_id: int | None) -> None:
        self._set_int(guild_id, "role_id", role_id)

    def get_category_id(self, guild_id: int) -> int | None:
        return self._get_int(guild_id, "category_id")

    def set_category(self, guild_id: int, category_id: int | None) -> None:
        self._set_int(guild_id, "category_id", category_id)

    def get_channel_id(self, guild_id: int) -> int | None:
        """Canal fijo del consejo: existe siempre y entre consejos queda oculto y vacío."""
        return self._get_int(guild_id, "channel_id")

    def set_channel(self, guild_id: int, channel_id: int | None) -> None:
        self._set_int(guild_id, "channel_id", channel_id)

    def get_log_channel_id(self, guild_id: int) -> int | None:
        return self._get_int(guild_id, "log_channel_id")

    def set_log_channel(self, guild_id: int, channel_id: int | None) -> None:
        self._set_int(guild_id, "log_channel_id", channel_id)

    # -- Consejo activo -----------------------------------------------------

    def get_active(self, guild_id: int) -> dict[str, Any] | None:
        guild_data = self._guilds.get(str(guild_id))
        active = guild_data.get("active") if isinstance(guild_data, dict) else None
        return active if isinstance(active, dict) else None

    def start(
        self,
        guild_id: int,
        *,
        channel_id: int,
        topic: str,
        description: str,
        opened_by: int,
        excluded: list[int],
    ) -> dict[str, Any]:
        active = {
            "channel_id": int(channel_id),
            "header_message_id": None,
            "topic": topic,
            "description": description,
            "opened_by": int(opened_by),
            "opened_at": _now_iso(),
            "met_at": None,
            "excluded": sorted({int(user_id) for user_id in excluded}),
            "polls": {},
        }
        self._guild_data(guild_id)["active"] = active
        self._save()
        return active

    def set_header(self, guild_id: int, message_id: int | None) -> None:
        active = self.get_active(guild_id)
        if active is not None:
            active["header_message_id"] = message_id
            self._save()

    def mark_met(self, guild_id: int) -> None:
        active = self.get_active(guild_id)
        if active is not None and not active.get("met_at"):
            active["met_at"] = _now_iso()
            self._save()

    def excluded_ids(self, guild_id: int) -> set[int]:
        active = self.get_active(guild_id)
        if active is None:
            return set()
        result: set[int] = set()
        for user_id in active.get("excluded") or []:
            try:
                result.add(int(user_id))
            except (TypeError, ValueError):
                continue
        return result

    def set_excluded(self, guild_id: int, user_ids: set[int]) -> list[str]:
        """Guarda los excluidos y quita sus votos de los polls abiertos.

        Devuelve los ids de los polls en los que se ha quitado algún voto.
        """
        active = self.get_active(guild_id)
        if active is None:
            return []
        active["excluded"] = sorted(int(user_id) for user_id in user_ids)
        touched: list[str] = []
        for poll_id, poll in self._polls(active).items():
            if poll.get("closed"):
                continue
            votes = poll.get("votes")
            if not isinstance(votes, dict):
                continue
            removed = [key for key in votes if _as_int(key) in user_ids]
            for key in removed:
                votes.pop(key, None)
            if removed:
                touched.append(poll_id)
        self._save()
        return touched

    def end(self, guild_id: int) -> dict[str, Any] | None:
        guild_data = self._guilds.get(str(guild_id))
        if not isinstance(guild_data, dict):
            return None
        active = guild_data.pop("active", None)
        self._save()
        return active if isinstance(active, dict) else None

    # -- Polls --------------------------------------------------------------

    @staticmethod
    def _polls(active: dict[str, Any]) -> dict[str, dict[str, Any]]:
        polls = active.get("polls")
        if not isinstance(polls, dict):
            polls = {}
            active["polls"] = polls
        return polls

    def list_polls(self, guild_id: int) -> list[dict[str, Any]]:
        active = self.get_active(guild_id)
        if active is None:
            return []
        polls = [poll for poll in self._polls(active).values() if isinstance(poll, dict)]
        return sorted(polls, key=lambda poll: poll.get("created_at") or "")

    def get_poll(self, guild_id: int, poll_id: str) -> dict[str, Any] | None:
        active = self.get_active(guild_id)
        if active is None:
            return None
        poll = self._polls(active).get(poll_id)
        return poll if isinstance(poll, dict) else None

    def add_poll(
        self,
        guild_id: int,
        *,
        question: str,
        options: list[str],
        live: bool,
        created_by: int,
    ) -> dict[str, Any] | None:
        active = self.get_active(guild_id)
        if active is None:
            return None
        polls = self._polls(active)
        poll_id = secrets.token_hex(4)
        while poll_id in polls:
            poll_id = secrets.token_hex(4)
        poll = {
            "id": poll_id,
            "question": question,
            "options": list(options),
            "live": bool(live),
            "closed": False,
            "message_id": None,
            "created_by": int(created_by),
            "created_at": _now_iso(),
            "closed_at": None,
            "votes": {},
        }
        polls[poll_id] = poll
        self._save()
        return poll

    def set_poll_message(self, guild_id: int, poll_id: str, message_id: int) -> None:
        poll = self.get_poll(guild_id, poll_id)
        if poll is not None:
            poll["message_id"] = int(message_id)
            self._save()

    def remove_poll(self, guild_id: int, poll_id: str) -> None:
        active = self.get_active(guild_id)
        if active is not None and self._polls(active).pop(poll_id, None) is not None:
            self._save()

    def set_poll_live(self, guild_id: int, poll_id: str, live: bool) -> None:
        poll = self.get_poll(guild_id, poll_id)
        if poll is not None:
            poll["live"] = bool(live)
            self._save()

    def vote(self, guild_id: int, poll_id: str, user_id: int, option: int) -> tuple[str, int | None]:
        """Registra el voto. Cada persona vota una sola vez y no puede cambiarlo.

        Devuelve el resultado y, si ya había votado, la opción que eligió.
        """
        poll = self.get_poll(guild_id, poll_id)
        if poll is None or not 0 <= option < len(poll.get("options") or []):
            return VOTE_MISSING, None
        if poll.get("closed"):
            return VOTE_CLOSED, None
        previous = poll_votes(poll).get(int(user_id))
        if previous is not None:
            return VOTE_ALREADY, previous
        votes = poll.get("votes")
        if not isinstance(votes, dict):
            votes = {}
            poll["votes"] = votes
        votes[str(int(user_id))] = int(option)
        self._save()
        return VOTE_OK, option

    def close_poll(self, guild_id: int, poll_id: str) -> bool:
        poll = self.get_poll(guild_id, poll_id)
        if poll is None or poll.get("closed"):
            return False
        poll["closed"] = True
        poll["closed_at"] = _now_iso()
        self._save()
        return True

    def close_all_polls(self, guild_id: int) -> list[dict[str, Any]]:
        closed: list[dict[str, Any]] = []
        for poll in self.list_polls(guild_id):
            if not poll.get("closed"):
                poll["closed"] = True
                poll["closed_at"] = _now_iso()
                closed.append(poll)
        if closed:
            self._save()
        return closed


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
