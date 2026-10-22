import json
import time
from pathlib import Path
from typing import Any


DEFAULT_RRPP_EMOJI = "📩"
POINTS_PER_LEVEL = 5
CLAIM_POINTS = {"greeting": 1, "presentation": 1, "vc": 3}
# Punto extra por claim si el usuario había pedido ayuda reaccionando a una cita RRPP.
HELP_BONUS_POINTS = 1
CLAIM_ACCEPTED = "accepted"
CLAIM_REJECTED = "rejected"


def level_for_points(points: int) -> int:
    return max(0, points) // POINTS_PER_LEVEL


class RrppStore:
    def __init__(self, json_path: str = "rrpp.json") -> None:
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
        guild_data = self._guilds.setdefault(str(guild_id), {})
        for key in ("messages", "claims", "announced_levels", "help_users"):
            if not isinstance(guild_data.get(key), dict):
                guild_data[key] = {}
        return guild_data

    def _get_int(self, guild_id: int, key: str) -> int | None:
        guild_data = self._guilds.get(str(guild_id))
        if not isinstance(guild_data, dict):
            return None

        try:
            return int(guild_data[key])
        except (KeyError, TypeError, ValueError):
            return None

    def set_role(self, guild_id: int, role_id: int | None) -> None:
        self._guild_data(guild_id)["role_id"] = str(role_id) if role_id else None
        self._save()

    def get_role_id(self, guild_id: int) -> int | None:
        return self._get_int(guild_id, "role_id")

    def set_channel(self, guild_id: int, channel_id: int | None) -> None:
        self._guild_data(guild_id)["channel_id"] = str(channel_id) if channel_id else None
        self._save()

    def get_channel_id(self, guild_id: int) -> int | None:
        return self._get_int(guild_id, "channel_id")

    def set_named_channel(self, guild_id: int, name: str, channel_id: int | None) -> None:
        """Guarda los canales extra: `levels`, `logs` o `spam`."""
        self._guild_data(guild_id)[f"{name}_channel_id"] = str(channel_id) if channel_id else None
        self._save()

    def get_named_channel_id(self, guild_id: int, name: str) -> int | None:
        return self._get_int(guild_id, f"{name}_channel_id")

    def set_emoji(self, guild_id: int, emoji: str) -> None:
        self._guild_data(guild_id)["emoji"] = emoji
        self._save()

    def get_emoji(self, guild_id: int) -> str:
        guild_data = self._guilds.get(str(guild_id))
        emoji = guild_data.get("emoji") if isinstance(guild_data, dict) else None
        return emoji if isinstance(emoji, str) and emoji else DEFAULT_RRPP_EMOJI

    def add_message(
        self,
        guild_id: int,
        channel_id: int,
        message_id: int,
        quoted_user_id: int,
        emoji: str,
    ) -> None:
        self._guild_data(guild_id)["messages"][str(message_id)] = {
            "channel_id": str(channel_id),
            "quoted_user_id": str(quoted_user_id),
            "emoji": emoji,
            "created_at": time.time(),
            "notified_users": [],
        }
        self._save()

    def get_message(self, guild_id: int, message_id: int) -> dict[str, Any] | None:
        guild_data = self._guilds.get(str(guild_id))
        if not isinstance(guild_data, dict):
            return None

        messages = guild_data.get("messages")
        if not isinstance(messages, dict):
            return None

        payload = messages.get(str(message_id))
        return payload if isinstance(payload, dict) else None

    def list_messages(self, guild_id: int) -> list[tuple[int, dict[str, Any]]]:
        """Devuelve los mensajes RRPP activos, del más reciente al más antiguo."""
        guild_data = self._guilds.get(str(guild_id))
        messages = guild_data.get("messages") if isinstance(guild_data, dict) else None
        if not isinstance(messages, dict):
            return []

        result: list[tuple[int, dict[str, Any]]] = []
        for message_key, payload in messages.items():
            if not isinstance(payload, dict):
                continue
            try:
                result.append((int(message_key), payload))
            except (TypeError, ValueError):
                continue

        result.sort(key=lambda item: item[1].get("created_at", 0), reverse=True)
        return result

    def remove_message(self, guild_id: int, message_id: int) -> bool:
        guild_data = self._guilds.get(str(guild_id))
        messages = guild_data.get("messages") if isinstance(guild_data, dict) else None
        if not isinstance(messages, dict) or messages.pop(str(message_id), None) is None:
            return False

        self._save()
        return True

    def mark_notified(self, guild_id: int, message_id: int, user_id: int) -> bool:
        """Registra que se ha avisado por este usuario. Devuelve False si ya estaba."""
        payload = self.get_message(guild_id, message_id)
        if payload is None:
            return False

        notified = payload.get("notified_users")
        if not isinstance(notified, list):
            notified = []
            payload["notified_users"] = notified

        user_key = str(user_id)
        if user_key in notified:
            return False

        notified.append(user_key)
        # Se guarda aparte para no perderlo cuando se desactive o borre la cita.
        self._guild_data(guild_id)["help_users"].setdefault(user_key, time.time())
        self._save()
        return True

    def has_requested_help(self, guild_id: int, user_id: int) -> bool:
        user_key = str(user_id)
        guild_data = self._guilds.get(str(guild_id))
        help_users = guild_data.get("help_users") if isinstance(guild_data, dict) else None
        if isinstance(help_users, dict) and user_key in help_users:
            return True

        return any(
            isinstance(payload.get("notified_users"), list)
            and user_key in payload["notified_users"]
            for _, payload in self.list_messages(guild_id)
        )

    def unmark_notified(self, guild_id: int, message_id: int, user_id: int) -> None:
        payload = self.get_message(guild_id, message_id)
        notified = payload.get("notified_users") if payload is not None else None
        if isinstance(notified, list) and str(user_id) in notified:
            notified.remove(str(user_id))
            self._save()

    def _claims(self, guild_id: int) -> dict[str, Any]:
        guild_data = self._guilds.get(str(guild_id))
        claims = guild_data.get("claims") if isinstance(guild_data, dict) else None
        return claims if isinstance(claims, dict) else {}

    def add_claim(
        self,
        guild_id: int,
        rrpp_id: int,
        target_id: int,
        claim_type: str,
        help_bonus: bool = False,
        **details: Any,
    ) -> dict[str, Any]:
        guild_data = self._guild_data(guild_id)
        claim_id = int(guild_data.get("next_claim_id", 1))
        guild_data["next_claim_id"] = claim_id + 1

        claim = {
            "id": claim_id,
            "rrpp_id": str(rrpp_id),
            "target_id": str(target_id),
            "type": claim_type,
            "points": CLAIM_POINTS.get(claim_type, 0) + (HELP_BONUS_POINTS if help_bonus else 0),
            "help_bonus": help_bonus,
            "status": CLAIM_ACCEPTED,
            "created_at": time.time(),
            **details,
        }
        guild_data["claims"][str(claim_id)] = claim
        self._save()
        return claim

    def get_claim(self, guild_id: int, claim_id: int) -> dict[str, Any] | None:
        claim = self._claims(guild_id).get(str(claim_id))
        return claim if isinstance(claim, dict) else None

    def update_claim(self, guild_id: int, claim_id: int, **fields: Any) -> None:
        claim = self.get_claim(guild_id, claim_id)
        if claim is None:
            return
        claim.update(fields)
        self._save()

    def list_claims(self, guild_id: int, rrpp_id: int | None = None) -> list[dict[str, Any]]:
        """Devuelve los claims, del más reciente al más antiguo."""
        claims = [
            claim
            for claim in self._claims(guild_id).values()
            if isinstance(claim, dict)
            and (rrpp_id is None or claim.get("rrpp_id") == str(rrpp_id))
        ]
        claims.sort(key=lambda claim: claim.get("created_at", 0), reverse=True)
        return claims

    def find_claim(
        self, guild_id: int, rrpp_id: int, target_id: int, claim_type: str
    ) -> dict[str, Any] | None:
        """Busca el claim de ese tipo del RRPP sobre el usuario, rechazado o no."""
        for claim in self.list_claims(guild_id, rrpp_id):
            if claim.get("target_id") == str(target_id) and claim.get("type") == claim_type:
                return claim
        return None

    def is_source_used(self, guild_id: int, message_id: int) -> bool:
        return any(
            claim.get("source_message_id") == str(message_id)
            for claim in self.list_claims(guild_id)
        )

    def get_points(self, guild_id: int, rrpp_id: int) -> int:
        return sum(
            int(claim.get("points", 0))
            for claim in self.list_claims(guild_id, rrpp_id)
            if claim.get("status") == CLAIM_ACCEPTED
        )

    def get_points_by_rrpp(self, guild_id: int) -> dict[int, int]:
        points: dict[int, int] = {}
        for claim in self.list_claims(guild_id):
            try:
                rrpp_id = int(claim.get("rrpp_id", 0))
            except (TypeError, ValueError):
                continue
            gained = int(claim.get("points", 0)) if claim.get("status") == CLAIM_ACCEPTED else 0
            points[rrpp_id] = points.get(rrpp_id, 0) + gained
        return points

    def raise_announced_level(self, guild_id: int, rrpp_id: int, level: int) -> bool:
        """Guarda el nivel máximo anunciado. Devuelve True si `level` es nuevo."""
        announced = self._guild_data(guild_id)["announced_levels"]
        if level <= int(announced.get(str(rrpp_id), 0)):
            return False
        announced[str(rrpp_id)] = level
        self._save()
        return True
