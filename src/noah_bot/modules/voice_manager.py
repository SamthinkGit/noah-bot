import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _from_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


class VoiceManager:
    def __init__(self, json_path: str = "voice_stats.json"):
        self.json_path = Path(json_path)
        self._state = self._load()
        self._users: dict[str, dict[str, Any]] = self._state["users"]
        self._active_sessions: dict[str, dict[str, Any]] = self._state["active_sessions"]
        self._leveling: dict[str, Any] = self._state["leveling"]
        self._banned_channels: dict[str, dict[str, Any]] = self._state["banned_channels"]
        self._alert_channels: dict[str, dict[str, Any]] = self._state["alert_channels"]

    def _load(self) -> dict[str, Any]:
        if not self.json_path.exists():
            return self._default_state()

        try:
            with self.json_path.open("r", encoding="utf-8") as file:
                data = json.load(file)
        except (json.JSONDecodeError, OSError):
            return self._default_state()

        if not isinstance(data, dict):
            return self._default_state()

        users = data.get("users")
        active_sessions = data.get("active_sessions")
        leveling = data.get("leveling")
        banned_channels = data.get("banned_channels")
        alert_channels = data.get("alert_channels")
        default_leveling = self._default_leveling()

        if isinstance(leveling, dict):
            default_leveling.update(leveling)

        return {
            "users": users if isinstance(users, dict) else {},
            "active_sessions": active_sessions if isinstance(active_sessions, dict) else {},
            "leveling": default_leveling,
            "banned_channels": (
                banned_channels if isinstance(banned_channels, dict) else {}
            ),
            "alert_channels": (
                alert_channels if isinstance(alert_channels, dict) else {}
            ),
        }

    def _default_state(self) -> dict[str, dict[str, Any]]:
        return {
            "users": {},
            "active_sessions": {},
            "leveling": self._default_leveling(),
            "banned_channels": {},
            "alert_channels": {},
        }

    def _default_leveling(self) -> dict[str, Any]:
        return {
            "enabled": False,
            "hours_per_level": None,
            "minutes_per_level": None,
            "updated_at": None,
        }

    def _save(self) -> None:
        with self.json_path.open("w", encoding="utf-8") as file:
            json.dump(self._state, file, indent=2, ensure_ascii=False)

    def _user_key(self, user_id: int) -> str:
        return str(user_id)

    def _channel_key(self, channel_id: int) -> str:
        return str(channel_id)

    def _guild_key(self, guild_id: int) -> str:
        return str(guild_id)

    def _extract_level(self, total_minutes: int) -> int | None:
        level_data = self._build_level_data(total_minutes)
        if level_data is None:
            return None
        return int(level_data["level"])

    def _current_session_seconds(self, user_id: int) -> int:
        session = self._active_sessions.get(self._user_key(user_id))
        if session is None:
            return 0

        started_at = _from_iso(session.get("started_at"))
        if started_at is None:
            return 0

        return max(0, int((_utc_now() - started_at).total_seconds()))

    def _build_level_data(self, total_minutes: int) -> dict[str, Any] | None:
        if not self._leveling.get("enabled"):
            return None

        minutes_per_level = int(self._leveling.get("minutes_per_level") or 0)
        if minutes_per_level <= 0:
            return None

        sanitized_total = max(total_minutes, 0)
        level = 1 + (sanitized_total // minutes_per_level)
        progress_minutes = sanitized_total % minutes_per_level
        progress_percent = int((progress_minutes / minutes_per_level) * 100)

        return {
            "enabled": True,
            "level": level,
            "next_level": level + 1,
            "hours_per_level": self._leveling.get("hours_per_level"),
            "minutes_per_level": minutes_per_level,
            "progress_minutes": progress_minutes,
            "remaining_minutes": minutes_per_level - progress_minutes,
            "progress_percent": progress_percent,
        }

    def _ensure_user(
        self,
        user_id: int,
        display_name: str,
        guild_id: int | None = None,
        guild_name: str | None = None,
    ) -> dict[str, Any]:
        key = self._user_key(user_id)
        user = self._users.setdefault(
            key,
            {
                "user_id": user_id,
                "display_name": display_name,
                "total_minutes": 0,
                "total_seconds": 0,
                "sessions": 0,
                "last_joined_at": None,
                "last_left_at": None,
                "last_channel_id": None,
                "last_channel_name": None,
                "last_guild_id": guild_id,
                "last_guild_name": guild_name,
                "alert_type": "montadito",
                "updated_at": None,
            },
        )

        user["display_name"] = display_name
        user["alert_type"] = user.get("alert_type") or "montadito"

        if guild_id is not None:
            user["last_guild_id"] = guild_id

        if guild_name is not None:
            user["last_guild_name"] = guild_name

        return user

    def start_session(
        self,
        user_id: int,
        display_name: str,
        *,
        channel_id: int | None,
        channel_name: str | None,
        guild_id: int | None = None,
        guild_name: str | None = None,
        started_at: datetime | None = None,
    ) -> bool:
        key = self._user_key(user_id)
        timestamp = started_at or _utc_now()
        user = self._ensure_user(user_id, display_name, guild_id, guild_name)

        user["last_joined_at"] = _to_iso(timestamp)
        user["last_channel_id"] = channel_id
        user["last_channel_name"] = channel_name
        user["updated_at"] = _to_iso(timestamp)

        existing_session = self._active_sessions.get(key)
        if existing_session is not None:
            existing_session["display_name"] = display_name
            existing_session["channel_id"] = channel_id
            existing_session["channel_name"] = channel_name
            existing_session["guild_id"] = guild_id
            existing_session["guild_name"] = guild_name
            self._save()
            return False

        self._active_sessions[key] = {
            "user_id": user_id,
            "display_name": display_name,
            "channel_id": channel_id,
            "channel_name": channel_name,
            "guild_id": guild_id,
            "guild_name": guild_name,
            "started_at": _to_iso(timestamp),
        }
        self._save()
        return True

    def move_session(
        self,
        user_id: int,
        display_name: str,
        *,
        channel_id: int | None,
        channel_name: str | None,
        guild_id: int | None = None,
        guild_name: str | None = None,
    ) -> None:
        key = self._user_key(user_id)
        session = self._active_sessions.get(key)

        if session is None:
            self.start_session(
                user_id,
                display_name,
                channel_id=channel_id,
                channel_name=channel_name,
                guild_id=guild_id,
                guild_name=guild_name,
            )
            return

        session["display_name"] = display_name
        session["channel_id"] = channel_id
        session["channel_name"] = channel_name
        session["guild_id"] = guild_id
        session["guild_name"] = guild_name

        user = self._ensure_user(user_id, display_name, guild_id, guild_name)
        user["last_channel_id"] = channel_id
        user["last_channel_name"] = channel_name
        user["updated_at"] = _to_iso(_utc_now())
        self._save()

    def end_session(
        self,
        user_id: int,
        display_name: str,
        *,
        guild_id: int | None = None,
        guild_name: str | None = None,
        ended_at: datetime | None = None,
    ) -> dict[str, Any]:
        key = self._user_key(user_id)
        session = self._active_sessions.pop(key, None)

        if session is None:
            return {
                "tracked": False,
                "minutes_added": 0,
                "elapsed_seconds": 0,
            }

        finished_at = ended_at or _utc_now()
        started_at = _from_iso(session.get("started_at")) or finished_at
        elapsed_seconds = max(0, int((finished_at - started_at).total_seconds()))
        minutes_added = elapsed_seconds // 60

        user = self._ensure_user(user_id, display_name, guild_id, guild_name)
        user["display_name"] = display_name
        total_minutes_before = int(user.get("total_minutes", 0))
        previous_level = self._extract_level(total_minutes_before)
        user["total_seconds"] = int(user.get("total_seconds", 0)) + elapsed_seconds
        user["total_minutes"] = int(user.get("total_minutes", 0)) + minutes_added
        user["sessions"] = int(user.get("sessions", 0)) + 1
        user["last_left_at"] = _to_iso(finished_at)
        user["last_channel_id"] = session.get("channel_id")
        user["last_channel_name"] = session.get("channel_name")
        user["updated_at"] = _to_iso(finished_at)

        if guild_id is not None:
            user["last_guild_id"] = guild_id

        if guild_name is not None:
            user["last_guild_name"] = guild_name

        self._save()

        current_total_minutes = int(user.get("total_minutes", 0))
        new_level = self._extract_level(current_total_minutes)
        leveled_up = (
            previous_level is not None
            and new_level is not None
            and new_level > previous_level
        )

        return {
            "tracked": True,
            "minutes_added": minutes_added,
            "elapsed_seconds": elapsed_seconds,
            "started_at": session.get("started_at"),
            "ended_at": _to_iso(finished_at),
            "channel_name": session.get("channel_name"),
            "previous_level": previous_level,
            "new_level": new_level,
            "leveled_up": leveled_up,
        }

    def set_total_minutes(
        self,
        user_id: int,
        display_name: str,
        total_minutes: int,
        *,
        guild_id: int | None = None,
        guild_name: str | None = None,
    ) -> dict[str, Any]:
        sanitized_minutes = max(0, int(total_minutes))
        user = self._ensure_user(user_id, display_name, guild_id, guild_name)
        updated_at = _utc_now()
        previous_level = self._extract_level(int(user.get("total_minutes", 0)))

        user["total_minutes"] = sanitized_minutes
        user["total_seconds"] = sanitized_minutes * 60
        user["updated_at"] = _to_iso(updated_at)

        if guild_id is not None:
            user["last_guild_id"] = guild_id

        if guild_name is not None:
            user["last_guild_name"] = guild_name

        self._save()

        new_level = self._extract_level(sanitized_minutes)
        return {
            "user_id": user_id,
            "display_name": display_name,
            "total_minutes": sanitized_minutes,
            "level_data": self._build_level_data(sanitized_minutes),
            "previous_level": previous_level,
            "new_level": new_level,
        }

    def set_user_alert_type(
        self,
        user_id: int,
        display_name: str,
        alert_type: str,
        *,
        guild_id: int | None = None,
        guild_name: str | None = None,
    ) -> dict[str, Any]:
        sanitized_alert_type = alert_type.strip().lower()
        if sanitized_alert_type not in {"montadito", "noah"}:
            raise ValueError("Invalid alert type.")

        user = self._ensure_user(user_id, display_name, guild_id, guild_name)
        user["alert_type"] = sanitized_alert_type
        user["updated_at"] = _to_iso(_utc_now())
        self._save()

        return {
            "user_id": user_id,
            "display_name": display_name,
            "alert_type": sanitized_alert_type,
        }

    def get_user_alert_type(self, user_id: int) -> str:
        user = self._users.get(self._user_key(user_id))
        if user is None:
            return "montadito"
        return str(user.get("alert_type") or "montadito")

    def configure_leveling(self, hours_per_level: float) -> dict[str, Any]:
        sanitized_hours = float(hours_per_level)
        if sanitized_hours <= 0:
            raise ValueError("hours_per_level must be greater than zero.")

        minutes_per_level = max(1, int(round(sanitized_hours * 60)))
        updated_at = _utc_now()

        self._leveling["enabled"] = True
        self._leveling["hours_per_level"] = sanitized_hours
        self._leveling["minutes_per_level"] = minutes_per_level
        self._leveling["updated_at"] = _to_iso(updated_at)
        self._save()

        return dict(self._leveling)

    def get_leveling_config(self) -> dict[str, Any]:
        return dict(self._leveling)

    def ban_channel(
        self,
        channel_id: int,
        channel_name: str,
        *,
        guild_id: int | None = None,
        guild_name: str | None = None,
    ) -> dict[str, Any]:
        channel_key = self._channel_key(channel_id)
        self._banned_channels[channel_key] = {
            "channel_id": channel_id,
            "channel_name": channel_name,
            "guild_id": guild_id,
            "guild_name": guild_name,
            "updated_at": _to_iso(_utc_now()),
        }
        self._save()
        return dict(self._banned_channels[channel_key])

    def is_channel_banned(self, channel_id: int | None) -> bool:
        if channel_id is None:
            return False
        return self._channel_key(channel_id) in self._banned_channels

    def set_alert_channel(
        self,
        guild_id: int,
        guild_name: str,
        channel_id: int,
        channel_name: str,
    ) -> dict[str, Any]:
        guild_key = self._guild_key(guild_id)
        self._alert_channels[guild_key] = {
            "guild_id": guild_id,
            "guild_name": guild_name,
            "channel_id": channel_id,
            "channel_name": channel_name,
            "updated_at": _to_iso(_utc_now()),
        }
        self._save()
        return dict(self._alert_channels[guild_key])

    def get_alert_channel(self, guild_id: int) -> dict[str, Any] | None:
        alert_channel = self._alert_channels.get(self._guild_key(guild_id))
        if alert_channel is None:
            return None
        return dict(alert_channel)

    def handle_voice_state_change(
        self,
        user_id: int,
        display_name: str,
        *,
        before_channel_id: int | None,
        before_channel_name: str | None,
        after_channel_id: int | None,
        after_channel_name: str | None,
        guild_id: int | None = None,
        guild_name: str | None = None,
    ) -> dict[str, Any] | None:
        if before_channel_id == after_channel_id:
            return None

        if before_channel_id is None and after_channel_id is not None:
            self.start_session(
                user_id,
                display_name,
                channel_id=after_channel_id,
                channel_name=after_channel_name,
                guild_id=guild_id,
                guild_name=guild_name,
            )
            return {"event": "join"}

        if before_channel_id is not None and after_channel_id is None:
            result = self.end_session(
                user_id,
                display_name,
                guild_id=guild_id,
                guild_name=guild_name,
            )
            result["event"] = "leave"
            result["previous_channel_name"] = before_channel_name
            return result

        self.move_session(
            user_id,
            display_name,
            channel_id=after_channel_id,
            channel_name=after_channel_name,
            guild_id=guild_id,
            guild_name=guild_name,
        )
        return {"event": "move"}

    def sync_connected_members(self, connected_members: list[dict[str, Any]]) -> dict[str, int]:
        now = _utc_now()
        connected_lookup = {
            self._user_key(member["user_id"]): member for member in connected_members
        }
        added = 0
        removed = 0
        updated = 0
        changed = False

        for user_key in list(self._active_sessions.keys()):
            if user_key in connected_lookup:
                continue

            self._active_sessions.pop(user_key, None)
            removed += 1
            changed = True

        for user_key, member in connected_lookup.items():
            user = self._ensure_user(
                member["user_id"],
                member["display_name"],
                member.get("guild_id"),
                member.get("guild_name"),
            )

            session = self._active_sessions.get(user_key)
            if session is None:
                user["last_joined_at"] = _to_iso(now)
                user["last_channel_id"] = member.get("channel_id")
                user["last_channel_name"] = member.get("channel_name")
                user["updated_at"] = _to_iso(now)
                self._active_sessions[user_key] = {
                    "user_id": member["user_id"],
                    "display_name": member["display_name"],
                    "channel_id": member.get("channel_id"),
                    "channel_name": member.get("channel_name"),
                    "guild_id": member.get("guild_id"),
                    "guild_name": member.get("guild_name"),
                    "started_at": _to_iso(now),
                }
                added += 1
                changed = True
                continue

            updated += 1
            session["display_name"] = member["display_name"]
            session["channel_id"] = member.get("channel_id")
            session["channel_name"] = member.get("channel_name")
            session["guild_id"] = member.get("guild_id")
            session["guild_name"] = member.get("guild_name")
            user["display_name"] = member["display_name"]
            user["last_channel_id"] = member.get("channel_id")
            user["last_channel_name"] = member.get("channel_name")

        if changed:
            self._save()

        return {
            "added": added,
            "removed": removed,
            "updated": updated,
        }

    def get_user_stats(self, user_id: int) -> dict[str, Any] | None:
        key = self._user_key(user_id)
        user = self._users.get(key)
        session = self._active_sessions.get(key)

        if user is None and session is None:
            return None

        data = dict(user or {})
        data["user_id"] = user_id
        data["is_connected"] = session is not None
        data["active_session"] = dict(session) if session else None

        if session:
            started_at = _from_iso(session.get("started_at"))
            if started_at is not None:
                elapsed_seconds = max(0, int((_utc_now() - started_at).total_seconds()))
            else:
                elapsed_seconds = 0

            data["current_session_seconds"] = elapsed_seconds
            data["current_session_minutes"] = elapsed_seconds // 60
        else:
            data["current_session_seconds"] = 0
            data["current_session_minutes"] = 0

        stored_total_seconds = int(data.get("total_seconds", 0))
        data["effective_total_seconds"] = (
            stored_total_seconds + int(data["current_session_seconds"])
        )
        data["effective_total_minutes"] = data["effective_total_seconds"] // 60
        data["level_data"] = self._build_level_data(data["effective_total_minutes"])

        return data

    def get_top_users(
        self,
        limit: int = 10,
        *,
        guild_member_ids: set[int] | None = None,
    ) -> list[dict[str, Any]]:
        records = []

        for user in self._users.values():
            user_id = int(user["user_id"])
            if guild_member_ids is not None and user_id not in guild_member_ids:
                continue
            record = dict(user)
            current_session_seconds = self._current_session_seconds(user_id)
            record["effective_total_seconds"] = int(record.get("total_seconds", 0)) + current_session_seconds
            record["effective_total_minutes"] = record["effective_total_seconds"] // 60
            record["level_data"] = self._build_level_data(record["effective_total_minutes"])
            records.append(record)

        records.sort(
            key=lambda record: (
                int(record.get("effective_total_minutes", record.get("total_minutes", 0))),
                int(record.get("effective_total_seconds", record.get("total_seconds", 0))),
                -int(record.get("user_id", 0)),
            ),
            reverse=True,
        )
        return records[:limit]

    def get_active_sessions(self, *, guild_id: int | None = None) -> list[dict[str, Any]]:
        sessions: list[dict[str, Any]] = []

        for session in self._active_sessions.values():
            if guild_id is not None and session.get("guild_id") != guild_id:
                continue

            data = dict(session)
            started_at = _from_iso(session.get("started_at"))
            if started_at is not None:
                elapsed_seconds = max(0, int((_utc_now() - started_at).total_seconds()))
            else:
                elapsed_seconds = 0

            data["elapsed_seconds"] = elapsed_seconds
            data["elapsed_minutes"] = elapsed_seconds // 60
            sessions.append(data)

        sessions.sort(key=lambda session: session.get("started_at") or "")
        return sessions
