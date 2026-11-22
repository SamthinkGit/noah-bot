import copy
import json
import os
import random
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo


SCHEMA_VERSION = 2
try:
    LOCAL_TIMEZONE = ZoneInfo("Europe/Madrid")
except Exception:
    LOCAL_TIMEZONE = datetime.now().astimezone().tzinfo or timezone.utc
LINK_COOLDOWN_SECONDS = 5 * 60
SPAM_UNLINK_CHANCE = 0.5
MAX_LINK_PV = 100.0
EPSILON = 1e-9


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _to_iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def _from_iso(value: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(value) if value else None


def _today_local(now: Optional[datetime] = None) -> str:
    now = now or _utc_now()
    return now.astimezone(LOCAL_TIMEZONE).date().isoformat()


@dataclass(frozen=True, slots=True)
class RelicDefinition:
    key: str
    title: str
    tier: int
    spawn_weight: float
    link_value: float
    reward_essence: int
    image_name: str
    color: int
    auto_claim: bool = False
    aliases: tuple[str, ...] = ()


RELIC_TYPES: dict[str, RelicDefinition] = {
    "vestigio": RelicDefinition(
        key="vestigio",
        title="Vestigio",
        tier=0,
        spawn_weight=85.0,
        link_value=4.0,
        reward_essence=1,
        image_name="vestigio.jpeg",
        color=0xA88E63,
        aliases=("vestigio",),
    ),
    "fragmento": RelicDefinition(
        key="fragmento",
        title="Fragmento",
        tier=1,
        spawn_weight=10.0,
        link_value=2.0,
        reward_essence=3,
        image_name="fragmento.jpeg",
        color=0x4F86C6,
        aliases=("fragmento",),
    ),
    "amuleto": RelicDefinition(
        key="amuleto",
        title="Amuleto",
        tier=2,
        spawn_weight=5.0,
        link_value=0.5,
        reward_essence=5,
        image_name="amuleto.jpeg",
        color=0x2E8B57,
        aliases=("amuleto",),
    ),
    "reliquia": RelicDefinition(
        key="reliquia",
        title="Reliquia",
        tier=3,
        spawn_weight=1.0,
        link_value=0.5,
        reward_essence=20,
        image_name="reliquia.jpeg",
        color=0xD4AF37,
        aliases=("reliquia",),
    ),
    "marca_oscura": RelicDefinition(
        key="marca_oscura",
        title="Marca Oscura",
        tier=4,
        spawn_weight=0.1,
        link_value=0.0,
        reward_essence=25,
        image_name="marca.jpeg",
        color=0x4B0082,
        auto_claim=True,
        aliases=("marcaoscura", "marca_oscura", "marca-oscura", "marca oscura"),
    ),
}

RELIC_ORDER = tuple(RELIC_TYPES.keys())

_RELIC_ALIASES: dict[str, str] = {}
for relic_key, relic_definition in RELIC_TYPES.items():
    _RELIC_ALIASES[relic_key] = relic_key
    for alias in relic_definition.aliases:
        _RELIC_ALIASES[re.sub(r"[\s_-]+", "", alias.lower())] = relic_key


def resolve_relic_type(raw_value: str) -> Optional[str]:
    sanitized = re.sub(r"[\s_-]+", "", raw_value.strip().lower())
    return _RELIC_ALIASES.get(sanitized)


class RelicsGameManager:
    def __init__(self, json_path: str, rng: Optional[random.Random] = None) -> None:
        self.json_path = json_path
        self.rng = rng or random.Random()
        self._state: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "users": {},
            "active_relic": None,
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
                "users": {},
                "active_relic": None,
            }

        self._state.setdefault("schema_version", SCHEMA_VERSION)
        self._state.setdefault("users", {})
        self._state.setdefault("active_relic", None)
        self._normalize_active_relic_state()

        for user_id in list(self._state["users"].keys()):
            self._ensure_user(user_id)

        self._save()

    def _normalize_active_relic_state(self) -> None:
        active_relic = self._state.get("active_relic")
        if active_relic is None:
            return

        active_relic.setdefault("linkers", {})
        active_relic.setdefault("alerts", {})

        for user_id, alert_state in list(active_relic["alerts"].items()):
            active_relic["alerts"][str(user_id)] = {
                "channel_id": alert_state.get("channel_id"),
                "message_id": alert_state.get("message_id"),
            }

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.json_path) or ".", exist_ok=True)
        with open(self.json_path, "w", encoding="utf-8") as file:
            json.dump(self._state, file, indent=2, ensure_ascii=False)

    def _default_user_state(self) -> dict[str, Any]:
        return {
            "essence_extracts": 0,
            "last_spawn_date": None,
            "last_link_at": None,
            "linked_counts": {relic_key: 0 for relic_key in RELIC_ORDER},
        }

    def _ensure_user(self, user_id: str) -> dict[str, Any]:
        user_id = str(user_id)
        user_state = self._state["users"].setdefault(user_id, self._default_user_state())
        user_state.setdefault("essence_extracts", 0)
        user_state.setdefault("last_spawn_date", None)
        user_state.setdefault("last_link_at", None)
        user_state.setdefault(
            "linked_counts",
            {relic_key: 0 for relic_key in RELIC_ORDER},
        )

        for relic_key in RELIC_ORDER:
            user_state["linked_counts"].setdefault(relic_key, 0)

        return user_state

    def _select_random_relic_type(self) -> str:
        keys = list(RELIC_ORDER)
        weights = [RELIC_TYPES[key].spawn_weight for key in keys]
        return self.rng.choices(keys, weights=weights, k=1)[0]

    def _build_relic_state(
        self,
        relic_type: str,
        user_id: str,
        guild_id: int | None,
        channel_id: int | None,
        now: Optional[datetime] = None,
    ) -> dict[str, Any]:
        now = now or _utc_now()
        return {
            "type": relic_type,
            "spawned_by": str(user_id),
            "spawned_at": _to_iso(now),
            "guild_id": int(guild_id) if guild_id is not None else None,
            "channel_id": int(channel_id) if channel_id is not None else None,
            "message_id": None,
            "linkers": {},
            "alerts": {},
            "claimed_by": None,
            "claimed_at": None,
        }

    def _public_relic_view(self, relic_state: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
        if relic_state is None:
            return None

        definition = RELIC_TYPES[relic_state["type"]]
        linkers = []
        for user_id, raw_link_data in relic_state.get("linkers", {}).items():
            linkers.append(
                {
                    "user_id": str(user_id),
                    "pv": float(raw_link_data.get("pv", 0.0)),
                    "attempts": int(raw_link_data.get("attempts", 0)),
                }
            )

        linkers.sort(key=lambda item: (-item["pv"], item["user_id"]))

        return {
            "type": definition.key,
            "title": definition.title,
            "tier": definition.tier,
            "link_value": definition.link_value,
            "reward_essence": definition.reward_essence,
            "image_name": definition.image_name,
            "color": definition.color,
            "auto_claim": definition.auto_claim,
            "spawned_by": relic_state.get("spawned_by"),
            "spawned_at": relic_state.get("spawned_at"),
            "guild_id": relic_state.get("guild_id"),
            "channel_id": relic_state.get("channel_id"),
            "message_id": relic_state.get("message_id"),
            "claimed_by": relic_state.get("claimed_by"),
            "claimed_at": relic_state.get("claimed_at"),
            "linkers": linkers,
        }

    def _reward_user_for_relic(
        self,
        user_id: str,
        relic_type: str,
    ) -> dict[str, Any]:
        user_state = self._ensure_user(user_id)
        definition = RELIC_TYPES[relic_type]
        user_state["essence_extracts"] += definition.reward_essence
        user_state["linked_counts"][relic_type] += 1

        return {
            "reward_essence": definition.reward_essence,
            "essence_extracts": user_state["essence_extracts"],
            "linked_counts": copy.deepcopy(user_state["linked_counts"]),
        }

    def get_active_relic(self) -> Optional[dict[str, Any]]:
        return self._public_relic_view(self._state.get("active_relic"))

    def has_active_relic(self) -> bool:
        return self._state.get("active_relic") is not None

    def get_active_link_alerts(self) -> list[dict[str, Any]]:
        active_relic = self._state.get("active_relic")
        if active_relic is None:
            return []

        alerts = []
        for user_id, alert_state in active_relic.get("alerts", {}).items():
            alerts.append(
                {
                    "user_id": str(user_id),
                    "channel_id": alert_state.get("channel_id"),
                    "message_id": alert_state.get("message_id"),
                }
            )

        alerts.sort(key=lambda item: item["user_id"])
        return alerts

    def toggle_link_alert(
        self,
        user_id: str,
        channel_id: int | None,
    ) -> dict[str, Any]:
        active_relic = self._state.get("active_relic")
        if active_relic is None:
            return {"ok": False, "code": "no_active_relic"}

        alerts = active_relic.setdefault("alerts", {})
        user_id = str(user_id)
        existing_alert = alerts.get(user_id)

        if existing_alert is not None:
            removed_alert = {
                "user_id": user_id,
                "channel_id": existing_alert.get("channel_id"),
                "message_id": existing_alert.get("message_id"),
            }
            del alerts[user_id]
            self._save()
            return {
                "ok": True,
                "code": "disabled",
                "alert": removed_alert,
            }

        alerts[user_id] = {
            "channel_id": int(channel_id) if channel_id is not None else None,
            "message_id": None,
        }
        self._save()
        return {
            "ok": True,
            "code": "enabled",
            "alert": {
                "user_id": user_id,
                "channel_id": int(channel_id) if channel_id is not None else None,
                "message_id": None,
            },
        }

    def set_link_alert_message(self, user_id: str, message_id: int | None) -> bool:
        active_relic = self._state.get("active_relic")
        if active_relic is None:
            return False

        alerts = active_relic.setdefault("alerts", {})
        alert_state = alerts.get(str(user_id))
        if alert_state is None:
            return False

        alert_state["message_id"] = int(message_id) if message_id is not None else None
        self._save()
        return True

    def clear_link_alert_message(self, user_id: str) -> Optional[dict[str, Any]]:
        active_relic = self._state.get("active_relic")
        if active_relic is None:
            return None

        alerts = active_relic.setdefault("alerts", {})
        alert_state = alerts.get(str(user_id))
        if alert_state is None:
            return None

        cleared_alert = {
            "user_id": str(user_id),
            "channel_id": alert_state.get("channel_id"),
            "message_id": alert_state.get("message_id"),
        }
        alert_state["message_id"] = None
        self._save()
        return cleared_alert

    def set_active_message(self, message_id: int, channel_id: int | None = None) -> None:
        active_relic = self._state.get("active_relic")
        if active_relic is None:
            return

        active_relic["message_id"] = int(message_id)
        if channel_id is not None:
            active_relic["channel_id"] = int(channel_id)
        self._save()

    def clear_active_relic(self) -> Optional[dict[str, Any]]:
        relic = self.get_active_relic()
        self._state["active_relic"] = None
        self._save()
        return relic

    def get_user_inventory(self, user_id: str) -> dict[str, Any]:
        user_state = self._ensure_user(user_id)
        return {
            "user_id": str(user_id),
            "essence_extracts": int(user_state["essence_extracts"]),
            "last_spawn_date": user_state.get("last_spawn_date"),
            "last_link_at": user_state.get("last_link_at"),
            "linked_counts": copy.deepcopy(user_state["linked_counts"]),
        }

    def get_link_cooldown_remaining(
        self,
        user_id: str,
        now: Optional[datetime] = None,
    ) -> dict[str, Any]:
        now = now or _utc_now()
        user_state = self._ensure_user(user_id)
        last_link_at = _from_iso(user_state.get("last_link_at"))

        if last_link_at is None:
            return {"ready": True, "seconds_left": 0}

        elapsed = (now - last_link_at).total_seconds()
        seconds_left = max(0, int(LINK_COOLDOWN_SECONDS - elapsed))
        return {
            "ready": seconds_left == 0,
            "seconds_left": seconds_left,
        }

    def spawn_relic(
        self,
        user_id: str,
        guild_id: int | None,
        channel_id: int | None,
        forced_type: str | None = None,
        ignore_daily_limit: bool = False,
        now: Optional[datetime] = None,
    ) -> dict[str, Any]:
        now = now or _utc_now()

        if self._state.get("active_relic") is not None:
            return {"ok": False, "code": "active_exists", "relic": self.get_active_relic()}

        self._ensure_user(user_id)

        relic_type = forced_type or self._select_random_relic_type()
        relic_state = self._build_relic_state(relic_type, user_id, guild_id, channel_id, now)

        if RELIC_TYPES[relic_type].auto_claim:
            relic_state["claimed_by"] = str(user_id)
            relic_state["claimed_at"] = _to_iso(now)
            reward = self._reward_user_for_relic(user_id, relic_type)
            self._save()
            return {
                "ok": True,
                "code": "auto_claim",
                "relic": self._public_relic_view(relic_state),
                "winner_id": str(user_id),
                **reward,
            }

        self._state["active_relic"] = relic_state
        self._save()
        return {
            "ok": True,
            "code": "spawned",
            "relic": self._public_relic_view(relic_state),
        }

    def link_user(self, user_id: str, now: Optional[datetime] = None) -> dict[str, Any]:
        now = now or _utc_now()
        active_relic = self._state.get("active_relic")
        if active_relic is None:
            return {"ok": False, "code": "no_active_relic"}

        alerts = active_relic.setdefault("alerts", {})
        alert_state = alerts.get(str(user_id))
        cleared_alert = None
        had_alert_message = False
        if alert_state is not None:
            cleared_alert = {
                "user_id": str(user_id),
                "channel_id": alert_state.get("channel_id"),
                "message_id": alert_state.get("message_id"),
            }
            had_alert_message = cleared_alert["message_id"] is not None
            alert_state["message_id"] = None

        user_state = self._ensure_user(user_id)
        last_link_at = _from_iso(user_state.get("last_link_at"))

        if last_link_at is not None:
            elapsed = (now - last_link_at).total_seconds()
            if elapsed < LINK_COOLDOWN_SECONDS:
                removed_pv = 0.0
                was_unlinked = False
                linkers = active_relic.setdefault("linkers", {})
                current_link = linkers.get(str(user_id))

                if current_link is not None and self.rng.random() < SPAM_UNLINK_CHANCE:
                    removed_pv = float(current_link.get("pv", 0.0))
                    del linkers[str(user_id)]
                    was_unlinked = True
                    self._save()

                if had_alert_message and not was_unlinked:
                    self._save()

                return {
                    "ok": False,
                    "code": "cooldown",
                    "seconds_left": max(0, int(LINK_COOLDOWN_SECONDS - elapsed)),
                    "was_unlinked": was_unlinked,
                    "removed_pv": removed_pv,
                    "cleared_alert": cleared_alert,
                    "relic": self.get_active_relic(),
                }

        definition = RELIC_TYPES[active_relic["type"]]
        linkers = active_relic.setdefault("linkers", {})
        current_link = linkers.setdefault(str(user_id), {"pv": 0.0, "attempts": 0})
        current_link["pv"] = min(
            MAX_LINK_PV,
            round(float(current_link.get("pv", 0.0)) + definition.link_value, 6),
        )
        current_link["attempts"] = int(current_link.get("attempts", 0)) + 1
        user_state["last_link_at"] = _to_iso(now)

        bound = self.rng.random() < (current_link["pv"] / 100.0)
        if not bound:
            self._save()
            return {
                "ok": True,
                "code": "linked",
                "added_pv": definition.link_value,
                "current_pv": current_link["pv"],
                "attempts": current_link["attempts"],
                "cleared_alert": cleared_alert,
                "relic": self.get_active_relic(),
            }

        active_relic["claimed_by"] = str(user_id)
        active_relic["claimed_at"] = _to_iso(now)
        reward = self._reward_user_for_relic(user_id, active_relic["type"])
        snapshot = self._public_relic_view(active_relic)
        cleanup_alerts = self.get_active_link_alerts()
        self._state["active_relic"] = None
        self._save()

        return {
            "ok": True,
            "code": "claimed",
            "winner_id": str(user_id),
            "added_pv": definition.link_value,
            "current_pv": current_link["pv"],
            "cleared_alert": cleared_alert,
            "cleanup_alerts": cleanup_alerts,
            "relic": snapshot,
            **reward,
        }

    def sacrifice_link(self, user_id: str) -> dict[str, Any]:
        active_relic = self._state.get("active_relic")
        if active_relic is None:
            return {"ok": False, "code": "no_active_relic"}

        linkers = active_relic.setdefault("linkers", {})
        own_link = linkers.get(str(user_id))
        if own_link is None:
            return {"ok": False, "code": "not_linked"}

        lost_pv = float(own_link.get("pv", 0.0))
        if lost_pv <= 0:
            del linkers[str(user_id)]
            self._save()
            return {"ok": False, "code": "not_linked"}

        del linkers[str(user_id)]
        affected_ids = [linked_user_id for linked_user_id in list(linkers.keys())]

        remaining_to_remove = lost_pv
        affected_count = 0

        while remaining_to_remove > EPSILON:
            candidates = [
                linked_user_id
                for linked_user_id in affected_ids
                if float(linkers[linked_user_id].get("pv", 0.0)) > EPSILON
            ]
            if not candidates:
                break

            share = remaining_to_remove / len(candidates)
            removed_this_round = 0.0

            for linked_user_id in candidates:
                current_pv = float(linkers[linked_user_id].get("pv", 0.0))
                deduction = min(current_pv, share)
                if deduction > EPSILON:
                    linkers[linked_user_id]["pv"] = round(current_pv - deduction, 6)
                    removed_this_round += deduction
                    affected_count += 1

            if removed_this_round <= EPSILON:
                break

            remaining_to_remove -= removed_this_round

        for linked_user_id in list(linkers.keys()):
            if float(linkers[linked_user_id].get("pv", 0.0)) <= EPSILON:
                del linkers[linked_user_id]

        total_removed_from_others = round(lost_pv - max(remaining_to_remove, 0.0), 6)
        self._save()

        return {
            "ok": True,
            "code": "sacrificed",
            "lost_pv": round(lost_pv, 6),
            "removed_from_others": total_removed_from_others,
            "affected_count": len(affected_ids),
            "relic": self.get_active_relic(),
        }

    def gift_essence(self, from_user_id: str, to_user_id: str, quantity: int) -> dict[str, Any]:
        if quantity <= 0:
            return {"ok": False, "code": "invalid_quantity"}

        if str(from_user_id) == str(to_user_id):
            return {"ok": False, "code": "same_user"}

        sender = self._ensure_user(from_user_id)
        receiver = self._ensure_user(to_user_id)

        if int(sender["essence_extracts"]) < quantity:
            return {
                "ok": False,
                "code": "insufficient_funds",
                "current_balance": int(sender["essence_extracts"]),
            }

        sender["essence_extracts"] -= quantity
        receiver["essence_extracts"] += quantity
        self._save()

        return {
            "ok": True,
            "code": "gifted",
            "quantity": quantity,
            "sender_balance": int(sender["essence_extracts"]),
            "receiver_balance": int(receiver["essence_extracts"]),
        }
