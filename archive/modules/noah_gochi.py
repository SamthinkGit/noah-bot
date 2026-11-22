from __future__ import annotations

import json
import os
import random
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo


SCHEMA_VERSION = 1
INTERACTION_COOLDOWN_HOURS = 1
DEFAULT_STATES = {
    "energy": 70,
    "happiness": 70,
    "confidence": 55,
    "social_battery": 65,
}
TRAIT_LABELS = {
    "height": "Altura",
    "build": "Complexion",
    "sociability": "Sociable / Introvertido",
    "quirkiness": "Peculiar / Comun",
    "favorite_color": "Color preferido",
    "nickname": "Apodo",
    "strength": "Fuerte / Debil",
    "speed": "Rapido / Lento",
}
RELATION_AXES: list[tuple[str, str, str]] = [
    ("quirkiness", "Peculiaridad", "Normalidad"),
    ("romance", "Romanticismo", "Odio"),
    ("loyalty", "Lealtad", "Traicion"),
    ("friendship", "Amistad", "Indiferencia"),
    ("dominance", "Dominancia", "Sumision"),
    ("trust", "Confianza", "Sospecha"),
    ("fun", "Diversion", "Aburrimiento"),
    ("admiration", "Admiracion", "Desprecio"),
    ("stability", "Estabilidad", "Caos"),
    ("support", "Apoyo", "Rivalidad"),
]
RELATION_LABELS = {key: (left, right) for key, left, right in RELATION_AXES}

try:
    LOCAL_TIMEZONE = ZoneInfo("Europe/Madrid")
except Exception:
    LOCAL_TIMEZONE = datetime.now().astimezone().tzinfo or timezone.utc


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _to_iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _from_iso(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _today_local(now: datetime | None = None) -> str:
    now = now or _utc_now()
    return now.astimezone(LOCAL_TIMEZONE).date().isoformat()


def _seconds_until_next_local_day(now: datetime | None = None) -> int:
    now = now or _utc_now()
    local_now = now.astimezone(LOCAL_TIMEZONE)
    next_day = datetime.combine(
        local_now.date() + timedelta(days=1),
        time.min,
        tzinfo=LOCAL_TIMEZONE,
    )
    return max(0, int((next_day - local_now).total_seconds()))


def _interaction_cooldown_from_raw(value: Any) -> datetime | None:
    if not value:
        return None

    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            parsed = None

        if parsed is not None:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=LOCAL_TIMEZONE)
            return parsed.astimezone(timezone.utc)

        try:
            local_day = datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError:
            return None

        return datetime.combine(
            local_day,
            time.min,
            tzinfo=LOCAL_TIMEZONE,
        ).astimezone(timezone.utc)

    return None


def _clamp(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, int(value)))


@dataclass(slots=True, frozen=True)
class RelationView:
    users: tuple[str, str]
    scores: dict[str, int]
    recent_changes: list[dict[str, Any]]
    last_interaction: dict[str, Any] | None


class NoahGochiManager:
    def __init__(self, json_path: str, rng: random.Random | None = None) -> None:
        self.json_path = json_path
        self.rng = rng or random.Random()
        self._state: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "guilds": {},
        }
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.json_path):
            self._save()
            return

        try:
            with open(self.json_path, "r", encoding="utf-8") as file:
                self._state = json.load(file)
        except (json.JSONDecodeError, OSError):
            self._state = {
                "schema_version": SCHEMA_VERSION,
                "guilds": {},
            }

        self._state.setdefault("schema_version", SCHEMA_VERSION)
        self._state.setdefault("guilds", {})

        for guild_id in list(self._state["guilds"].keys()):
            self._ensure_guild(guild_id)

        self._save()

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.json_path) or ".", exist_ok=True)
        with open(self.json_path, "w", encoding="utf-8") as file:
            json.dump(self._state, file, indent=2, ensure_ascii=False)

    def _ensure_guild(self, guild_id: str | int) -> dict[str, Any]:
        guild_key = str(guild_id)
        guild_state = self._state["guilds"].setdefault(
            guild_key,
            {
                "characters": {},
                "relations": {},
                "topics": [],
                "daily_cooldowns": {},
                "interaction_cooldowns": {},
            },
        )
        guild_state.setdefault("characters", {})
        guild_state.setdefault("relations", {})
        guild_state.setdefault("topics", [])
        guild_state.setdefault("daily_cooldowns", {})
        guild_state.setdefault("interaction_cooldowns", {})

        for relation_key, relation_state in guild_state["relations"].items():
            relation_state.setdefault("users", relation_key.split(":"))
            relation_state.setdefault("scores", self._default_relation_scores())
            relation_state.setdefault("recent_changes", [])
            relation_state.setdefault("last_interaction", None)

        for user_id, character in guild_state["characters"].items():
            guild_state["characters"][str(user_id)] = self._normalize_character(character)

        return guild_state

    def _normalize_character(self, raw: dict[str, Any]) -> dict[str, Any]:
        traits = raw.get("traits", {})
        normalized_traits = {
            key: str(traits.get(key, "")).strip()
            for key in TRAIT_LABELS
        }

        raw_states = raw.get("states", {})
        states = {
            key: _clamp(raw_states.get(key, default_value), 0, 100)
            for key, default_value in DEFAULT_STATES.items()
        }

        return {
            "name": str(raw.get("name", "Noah Gochi")).strip() or "Noah Gochi",
            "image_url": raw.get("image_url"),
            "traits": normalized_traits,
            "states": states,
            "created_at": raw.get("created_at"),
            "updated_at": raw.get("updated_at"),
        }

    def _default_relation_scores(self) -> dict[str, int]:
        return {axis_key: 0 for axis_key, _, _ in RELATION_AXES}

    def _pair_key(self, user_a: str | int, user_b: str | int) -> str:
        left, right = sorted((str(user_a), str(user_b)))
        return f"{left}:{right}"

    def _default_relation_state(self, user_a: str | int, user_b: str | int) -> dict[str, Any]:
        left, right = sorted((str(user_a), str(user_b)))
        return {
            "users": [left, right],
            "scores": self._default_relation_scores(),
            "recent_changes": [],
            "last_interaction": None,
        }

    def create_or_update_character(
        self,
        guild_id: str | int,
        user_id: str | int,
        name: str,
    ) -> dict[str, Any]:
        guild_state = self._ensure_guild(guild_id)
        user_key = str(user_id)
        previous = guild_state["characters"].get(user_key)
        now = _utc_now()

        character = self._normalize_character(previous or {})
        character["name"] = name.strip() or character["name"]
        character["updated_at"] = _to_iso(now)
        if previous is None:
            character["created_at"] = _to_iso(now)
        else:
            character["created_at"] = previous.get("created_at") or _to_iso(now)

        guild_state["characters"][user_key] = character
        self._save()
        return character

    def get_character(self, guild_id: str | int, user_id: str | int) -> dict[str, Any] | None:
        guild_state = self._ensure_guild(guild_id)
        raw = guild_state["characters"].get(str(user_id))
        if raw is None:
            return None
        return self._normalize_character(raw)

    def list_character_user_ids(self, guild_id: str | int) -> list[str]:
        guild_state = self._ensure_guild(guild_id)
        return sorted(guild_state["characters"].keys())

    def update_trait(
        self,
        guild_id: str | int,
        user_id: str | int,
        trait_key: str,
        value: str,
    ) -> dict[str, Any] | None:
        if trait_key not in TRAIT_LABELS:
            raise KeyError(f"Unknown trait: {trait_key}")

        guild_state = self._ensure_guild(guild_id)
        user_key = str(user_id)
        character = guild_state["characters"].get(user_key)
        if character is None:
            return None

        character = self._normalize_character(character)
        character["traits"][trait_key] = value.strip()
        character["updated_at"] = _to_iso(_utc_now())
        guild_state["characters"][user_key] = character
        self._save()
        return character

    def set_character_image(
        self,
        guild_id: str | int,
        user_id: str | int,
        image_url: str,
    ) -> dict[str, Any] | None:
        guild_state = self._ensure_guild(guild_id)
        user_key = str(user_id)
        character = guild_state["characters"].get(user_key)
        if character is None:
            return None

        character = self._normalize_character(character)
        character["image_url"] = image_url
        character["updated_at"] = _to_iso(_utc_now())
        guild_state["characters"][user_key] = character
        self._save()
        return character

    def apply_state_deltas(
        self,
        guild_id: str | int,
        user_id: str | int,
        deltas: dict[str, int],
    ) -> dict[str, int] | None:
        if not deltas:
            character = self.get_character(guild_id, user_id)
            return dict(character["states"]) if character is not None else None

        guild_state = self._ensure_guild(guild_id)
        user_key = str(user_id)
        character = guild_state["characters"].get(user_key)
        if character is None:
            return None

        character = self._normalize_character(character)
        for state_key, delta in deltas.items():
            if state_key not in DEFAULT_STATES:
                continue
            current_value = int(
                character["states"].get(state_key, DEFAULT_STATES[state_key])
            )
            character["states"][state_key] = _clamp(current_value + int(delta), 0, 100)

        character["updated_at"] = _to_iso(_utc_now())
        guild_state["characters"][user_key] = character
        self._save()
        return dict(character["states"])

    def add_topic(self, guild_id: str | int, topic: str) -> dict[str, Any]:
        guild_state = self._ensure_guild(guild_id)
        normalized = " ".join(topic.split()).strip()
        if not normalized:
            return {"ok": False, "code": "empty"}

        topics = guild_state["topics"]
        lowered = {existing.lower() for existing in topics}
        if normalized.lower() in lowered:
            return {"ok": False, "code": "duplicate", "topics": list(topics)}

        topics.append(normalized)
        guild_state["topics"] = topics[-50:]
        self._save()
        return {"ok": True, "topic": normalized, "topics": list(guild_state["topics"])}

    def get_topics(self, guild_id: str | int) -> list[str]:
        guild_state = self._ensure_guild(guild_id)
        return list(guild_state["topics"])

    def get_or_create_relation(
        self,
        guild_id: str | int,
        user_a: str | int,
        user_b: str | int,
    ) -> RelationView:
        guild_state = self._ensure_guild(guild_id)
        relation_key = self._pair_key(user_a, user_b)
        relation = guild_state["relations"].setdefault(
            relation_key,
            self._default_relation_state(user_a, user_b),
        )
        relation.setdefault("scores", self._default_relation_scores())
        relation.setdefault("recent_changes", [])
        relation.setdefault("last_interaction", None)
        self._save()
        return RelationView(
            users=tuple(relation["users"]),
            scores=dict(relation["scores"]),
            recent_changes=list(relation["recent_changes"]),
            last_interaction=relation["last_interaction"],
        )

    def apply_relation_update(
        self,
        guild_id: str | int,
        user_a: str | int,
        user_b: str | int,
        deltas: dict[str, int],
        *,
        summary: str,
        source: str,
        now: datetime | None = None,
    ) -> RelationView:
        now = now or _utc_now()
        guild_state = self._ensure_guild(guild_id)
        relation_key = self._pair_key(user_a, user_b)
        relation = guild_state["relations"].setdefault(
            relation_key,
            self._default_relation_state(user_a, user_b),
        )

        scores = relation.setdefault("scores", self._default_relation_scores())
        recent_changes = relation.setdefault("recent_changes", [])
        effective_changes: list[dict[str, Any]] = []

        for axis_key, delta in deltas.items():
            if axis_key not in RELATION_LABELS:
                continue
            sanitized_delta = int(delta)
            if sanitized_delta == 0:
                continue
            scores[axis_key] = _clamp(
                int(scores.get(axis_key, 0)) + sanitized_delta,
                -100,
                100,
            )
            effective_changes.append(
                {
                    "axis": axis_key,
                    "delta": sanitized_delta,
                    "source": source,
                    "created_at": _to_iso(now),
                }
            )

        if effective_changes:
            recent_changes.extend(effective_changes)
            relation["recent_changes"] = recent_changes[-20:]

        relation["last_interaction"] = {
            "summary": summary,
            "source": source,
            "created_at": _to_iso(now),
        }

        self._save()
        return RelationView(
            users=tuple(relation["users"]),
            scores=dict(scores),
            recent_changes=list(relation["recent_changes"]),
            last_interaction=relation["last_interaction"],
        )

    def get_daily_cooldown_remaining(
        self,
        guild_id: str | int,
        user_id: str | int,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        now = now or _utc_now()
        guild_state = self._ensure_guild(guild_id)
        raw_value = guild_state["daily_cooldowns"].get(str(user_id))
        last_used_at = _from_iso(raw_value)

        if last_used_at is None:
            return {"ready": True, "seconds_left": 0}

        if last_used_at.astimezone(LOCAL_TIMEZONE).date() != now.astimezone(LOCAL_TIMEZONE).date():
            return {"ready": True, "seconds_left": 0}

        seconds_left = _seconds_until_next_local_day(now)
        return {"ready": seconds_left == 0, "seconds_left": seconds_left}

    def mark_daily_used(
        self,
        guild_id: str | int,
        user_id: str | int,
        now: datetime | None = None,
    ) -> None:
        now = now or _utc_now()
        guild_state = self._ensure_guild(guild_id)
        guild_state["daily_cooldowns"][str(user_id)] = _to_iso(now)
        self._save()

    def get_interaction_cooldown_remaining(
        self,
        guild_id: str | int,
        actor_id: str | int,
        target_id: str | int,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        now = now or _utc_now()
        guild_state = self._ensure_guild(guild_id)
        actor_key = str(actor_id)
        target_key = str(target_id)
        actor_cooldowns = guild_state["interaction_cooldowns"].setdefault(actor_key, {})
        last_used_at = _interaction_cooldown_from_raw(actor_cooldowns.get(target_key))

        if last_used_at is None:
            return {"ready": True, "seconds_left": 0}

        elapsed = now - last_used_at
        remaining = timedelta(hours=INTERACTION_COOLDOWN_HOURS) - elapsed
        seconds_left = max(0, int(remaining.total_seconds()))
        return {"ready": seconds_left == 0, "seconds_left": seconds_left}

    def has_interacted_today(
        self,
        guild_id: str | int,
        actor_id: str | int,
        target_id: str | int,
        now: datetime | None = None,
    ) -> bool:
        cooldown = self.get_interaction_cooldown_remaining(
            guild_id,
            actor_id,
            target_id,
            now,
        )
        return not cooldown["ready"]

    def mark_interaction_used(
        self,
        guild_id: str | int,
        actor_id: str | int,
        target_id: str | int,
        now: datetime | None = None,
    ) -> None:
        now = now or _utc_now()
        guild_state = self._ensure_guild(guild_id)
        actor_key = str(actor_id)
        target_key = str(target_id)
        actor_cooldowns = guild_state["interaction_cooldowns"].setdefault(actor_key, {})
        actor_cooldowns[target_key] = _to_iso(now)
        self._save()

    def choose_daily_target(
        self,
        guild_id: str | int,
        actor_id: str | int,
    ) -> str | None:
        actor_key = str(actor_id)
        candidates = [
            user_id
            for user_id in self.list_character_user_ids(guild_id)
            if user_id != actor_key
        ]
        if candidates:
            return self.rng.choice(candidates)

        if self.get_character(guild_id, actor_key):
            return actor_key
        return None

    def build_profile_snapshot(
        self,
        guild_id: str | int,
        user_id: str | int,
    ) -> dict[str, Any] | None:
        character = self.get_character(guild_id, user_id)
        if character is None:
            return None

        return {
            "user_id": str(user_id),
            "name": character["name"],
            "traits": dict(character["traits"]),
            "states": dict(character["states"]),
            "image_url": character.get("image_url"),
        }

    def build_relation_snapshot(
        self,
        guild_id: str | int,
        user_a: str | int,
        user_b: str | int,
    ) -> dict[str, Any]:
        relation = self.get_or_create_relation(guild_id, user_a, user_b)
        recent_history = []
        if relation.last_interaction and relation.last_interaction.get("summary"):
            recent_history.append(str(relation.last_interaction["summary"]))

        return {
            "scores": dict(relation.scores),
            "recent_changes": list(relation.recent_changes),
            "recent_history": recent_history,
            "last_interaction": relation.last_interaction,
        }

    def describe_relation(self, scores: dict[str, int]) -> str:
        positive_axes = []
        negative_axes = []

        for axis_key, score in scores.items():
            left_label, right_label = RELATION_LABELS[axis_key]
            if score >= 20:
                positive_axes.append(left_label.lower())
            elif score <= -20:
                negative_axes.append(right_label.lower())

        if not positive_axes and not negative_axes:
            return "Una relacion bastante neutral, todavia buscando una forma clara."

        parts = []
        if positive_axes:
            parts.append(f"hay bastante {positive_axes[0]}")
        if negative_axes:
            parts.append(f"pero tambien se nota algo de {negative_axes[0]}")

        return "Una relacion " + ", ".join(parts) + "."
