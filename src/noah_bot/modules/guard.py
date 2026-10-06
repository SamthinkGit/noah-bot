import json
import re
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import discord


VIEW_CHANNEL = discord.Permissions(view_channel=True).value
ADMINISTRATOR = discord.Permissions(administrator=True).value
OVERWRITE_ROLE = 0
OVERWRITE_MEMBER = 1

DEFAULT_NEW_ACCOUNT_DAYS = 7
DEFAULT_DRILL_MINUTES = 10
STATS_SAVE_INTERVAL_SECONDS = 5.0
RATE_WINDOW_SECONDS = 60.0

MASS_MENTION_PATTERN = re.compile(r"@(everyone|here)\b", re.IGNORECASE)
LINK_PATTERN = re.compile(r"https?://\S+", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Permisos
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Overwrite:
    target_type: int
    allow: int
    deny: int


@dataclass(frozen=True, slots=True)
class OverwriteChange:
    channel_id: int
    target_id: int
    target_type: int
    before: tuple[int, int] | None
    after: tuple[int, int]
    kind: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "channel_id": self.channel_id,
            "target_id": self.target_id,
            "target_type": self.target_type,
            "before": list(self.before) if self.before is not None else None,
            "after": list(self.after),
            "kind": self.kind,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "OverwriteChange":
        before = payload.get("before")
        return cls(
            channel_id=int(payload["channel_id"]),
            target_id=int(payload["target_id"]),
            target_type=int(payload["target_type"]),
            before=(int(before[0]), int(before[1])) if before is not None else None,
            after=(int(payload["after"][0]), int(payload["after"][1])),
            kind=str(payload.get("kind", "")),
        )


def read_overwrites(channel: discord.abc.GuildChannel) -> dict[int, Overwrite]:
    return {
        int(raw.id): Overwrite(int(raw.type), int(raw.allow), int(raw.deny))
        for raw in getattr(channel, "_overwrites", [])
    }


def _member_can_view(
    member: discord.Member,
    overwrites: dict[int, Overwrite],
) -> bool:
    guild = member.guild
    if member.id == guild.owner_id:
        return True

    base = 0
    for role in member.roles:
        base |= role.permissions.value
    if base & ADMINISTRATOR:
        return True

    perms = base
    everyone = overwrites.get(guild.id)
    if everyone is not None:
        perms = (perms & ~everyone.deny) | everyone.allow

    allow = deny = 0
    for role in member.roles:
        if role.id == guild.id:
            continue
        overwrite = overwrites.get(role.id)
        if overwrite is not None and overwrite.target_type == OVERWRITE_ROLE:
            allow |= overwrite.allow
            deny |= overwrite.deny
    perms = (perms & ~deny) | allow

    own = overwrites.get(member.id)
    if own is not None and own.target_type == OVERWRITE_MEMBER:
        perms = (perms & ~own.deny) | own.allow

    return bool(perms & VIEW_CHANNEL)


def _role_alone_can_view(
    role: discord.Role,
    overwrites: dict[int, Overwrite],
) -> bool:
    guild = role.guild
    base = guild.default_role.permissions.value | role.permissions.value
    if base & ADMINISTRATOR:
        return True

    perms = base
    everyone = overwrites.get(guild.id)
    if everyone is not None:
        perms = (perms & ~everyone.deny) | everyone.allow

    overwrite = overwrites.get(role.id)
    if overwrite is not None:
        perms = (perms & ~overwrite.deny) | overwrite.allow

    return bool(perms & VIEW_CHANNEL)


def _grant_view(overwrite: Overwrite | None, target_type: int) -> Overwrite:
    allow, deny = (overwrite.allow, overwrite.deny) if overwrite else (0, 0)
    return Overwrite(target_type, allow | VIEW_CHANNEL, deny & ~VIEW_CHANNEL)


def plan_channel_lockdown(
    channel: discord.abc.GuildChannel,
    exempt_members: list[discord.Member],
    exempt_member_ids: set[int],
    exempt_roles: list[discord.Role],
) -> list[OverwriteChange]:
    """Calcula los cambios de permisos para ocultar un canal.

    Solo se toca el bit de "Ver canal". El orden devuelto es el de aplicación:
    primero se garantiza el acceso del staff, después se retira el acceso
    concedido a otros y, por último, se deniega a @everyone.
    """
    guild = channel.guild
    original = read_overwrites(channel)
    updated = dict(original)
    exempt_role_ids = {role.id for role in exempt_roles}

    strips: list[OverwriteChange] = []
    for target_id, overwrite in original.items():
        if target_id == guild.id or not overwrite.allow & VIEW_CHANNEL:
            continue
        if overwrite.target_type == OVERWRITE_ROLE and target_id in exempt_role_ids:
            continue
        if overwrite.target_type == OVERWRITE_MEMBER and target_id in exempt_member_ids:
            continue

        stripped = Overwrite(
            overwrite.target_type,
            overwrite.allow & ~VIEW_CHANNEL,
            overwrite.deny,
        )
        updated[target_id] = stripped
        strips.append(
            OverwriteChange(
                channel.id,
                target_id,
                overwrite.target_type,
                (overwrite.allow, overwrite.deny),
                (stripped.allow, stripped.deny),
                "strip",
            )
        )

    everyone_before = original.get(guild.id)
    everyone_after = Overwrite(
        OVERWRITE_ROLE,
        (everyone_before.allow if everyone_before else 0) & ~VIEW_CHANNEL,
        (everyone_before.deny if everyone_before else 0) | VIEW_CHANNEL,
    )
    everyone_changes: list[OverwriteChange] = []
    if everyone_before != everyone_after:
        updated[guild.id] = everyone_after
        everyone_changes.append(
            OverwriteChange(
                channel.id,
                guild.id,
                OVERWRITE_ROLE,
                (everyone_before.allow, everyone_before.deny) if everyone_before else None,
                (everyone_after.allow, everyone_after.deny),
                "everyone",
            )
        )

    grants: list[OverwriteChange] = []
    for role in exempt_roles:
        if not _role_alone_can_view(role, original) or _role_alone_can_view(role, updated):
            continue
        before = updated.get(role.id)
        granted = _grant_view(before, OVERWRITE_ROLE)
        updated[role.id] = granted
        grants.append(
            OverwriteChange(
                channel.id,
                role.id,
                OVERWRITE_ROLE,
                (before.allow, before.deny) if before else None,
                (granted.allow, granted.deny),
                "grant",
            )
        )

    for member in exempt_members:
        if not _member_can_view(member, original) or _member_can_view(member, updated):
            continue
        before = original.get(member.id)
        granted = _grant_view(before, OVERWRITE_MEMBER)
        updated[member.id] = granted
        grants.append(
            OverwriteChange(
                channel.id,
                member.id,
                OVERWRITE_MEMBER,
                (before.allow, before.deny) if before else None,
                (granted.allow, granted.deny),
                "grant",
            )
        )

    return grants + strips + everyone_changes


def is_channel_exposed(
    overwrites: dict[int, Overwrite],
    guild_id: int,
    exempt_role_ids: set[int],
    exempt_member_ids: set[int],
) -> bool:
    """Indica si un usuario sin privilegios podría ver el canal.

    Un canal está oculto si @everyone tiene denegado "Ver canal" y ningún rol o
    usuario fuera del staff exento lo tiene concedido.
    """
    everyone = overwrites.get(guild_id)
    if everyone is None or not everyone.deny & VIEW_CHANNEL or everyone.allow & VIEW_CHANNEL:
        return True

    for target_id, overwrite in overwrites.items():
        if target_id == guild_id or not overwrite.allow & VIEW_CHANNEL:
            continue
        exempt_ids = (
            exempt_role_ids if overwrite.target_type == OVERWRITE_ROLE else exempt_member_ids
        )
        if target_id not in exempt_ids:
            return True
    return False


def plan_restore(
    channel: discord.abc.GuildChannel,
    change: OverwriteChange,
) -> tuple[str, tuple[int, int] | None]:
    """Decide cómo deshacer un cambio sin pisar lo que el staff haya tocado.

    Devuelve ("restore", (allow, deny)), ("delete", None) o ("skip", None).
    Solo se revierte el bit de "Ver canal" y únicamente si sigue tal y como lo
    dejó el bot; el resto de bits conserva su valor actual.
    """
    current = read_overwrites(channel).get(change.target_id)
    if current is None:
        return "skip", None

    after_allow, after_deny = change.after
    if (
        current.allow & VIEW_CHANNEL != after_allow & VIEW_CHANNEL
        or current.deny & VIEW_CHANNEL != after_deny & VIEW_CHANNEL
    ):
        return "skip", None

    before_allow, before_deny = change.before or (0, 0)
    restored_allow = (current.allow & ~VIEW_CHANNEL) | (before_allow & VIEW_CHANNEL)
    restored_deny = (current.deny & ~VIEW_CHANNEL) | (before_deny & VIEW_CHANNEL)

    if change.before is None and restored_allow == 0 and restored_deny == 0:
        return "delete", None
    if (restored_allow, restored_deny) == (current.allow, current.deny):
        return "skip", None
    return "restore", (restored_allow, restored_deny)


# ---------------------------------------------------------------------------
# Puntuación de sospecha
# ---------------------------------------------------------------------------


def account_age_days(user_id: int, now: float | None = None) -> float:
    created = discord.utils.snowflake_time(user_id).timestamp()
    return max(0.0, ((now or time.time()) - created) / 86400)


def score_suspect(
    user_id: int,
    stats: dict[str, Any] | None,
    join: dict[str, Any] | None,
    new_account_days: int,
) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []

    if join is not None:
        score += 3
        reasons.append("entró durante la contingencia")

    age = account_age_days(user_id)
    if age < 1:
        score += 4
        reasons.append("cuenta creada hace menos de un día")
    elif age < new_account_days:
        score += 3
        reasons.append(f"cuenta de {int(age)} días")

    default_avatar = (join or {}).get("default_avatar") or (stats or {}).get("default_avatar")
    if default_avatar:
        score += 1
        reasons.append("sin avatar")

    if stats:
        peak = int(stats.get("peak_per_minute", 0))
        if peak >= 20:
            score += 3
            reasons.append(f"pico de {peak} mensajes/min")
        elif peak >= 10:
            score += 2
            reasons.append(f"pico de {peak} mensajes/min")

        duplicates = int(stats.get("duplicates", 0))
        if duplicates >= 3:
            score += 2
            reasons.append(f"{duplicates} mensajes repetidos")

        links = int(stats.get("links", 0))
        if links >= 3:
            score += 1
            reasons.append(f"{links} enlaces")

        mentions = int(stats.get("mentions", 0))
        if mentions >= 10:
            score += 2
            reasons.append(f"{mentions} menciones")

        if int(stats.get("mass_mentions", 0)) > 0:
            score += 3
            reasons.append("usó @everyone o @here")

    return score, reasons


# ---------------------------------------------------------------------------
# Persistencia
# ---------------------------------------------------------------------------


class GuardStore:
    def __init__(self, json_path: str = "guard.json") -> None:
        self.json_path = Path(json_path)
        self._guilds: dict[str, dict[str, Any]] = self._load()
        self._dirty = False
        self._last_save = 0.0
        self._rate_windows: dict[tuple[int, int], deque[float]] = {}
        self._notice_counters: dict[int, int] = {}

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

    def save(self) -> None:
        temp_path = self.json_path.with_suffix(".json.tmp")
        with temp_path.open("w", encoding="utf-8") as file:
            json.dump({"guilds": self._guilds}, file, indent=2, ensure_ascii=False)
        temp_path.replace(self.json_path)
        self._dirty = False
        self._last_save = time.monotonic()

    def _save_stats_soon(self) -> None:
        self._dirty = True
        if time.monotonic() - self._last_save >= STATS_SAVE_INTERVAL_SECONDS:
            self.save()

    def flush(self) -> None:
        if self._dirty:
            self.save()

    def _guild_data(self, guild_id: int) -> dict[str, Any]:
        return self._guilds.setdefault(str(guild_id), {})

    # -- Configuración -----------------------------------------------------

    def get_config(self, guild_id: int) -> dict[str, Any]:
        config = self._guilds.get(str(guild_id), {}).get("config", {})
        return {
            "users": [int(user_id) for user_id in config.get("users", [])],
            "roles": [int(role_id) for role_id in config.get("roles", [])],
            "new_account_days": int(
                config.get("new_account_days", DEFAULT_NEW_ACCOUNT_DAYS)
            ),
            "drill_minutes": int(config.get("drill_minutes", DEFAULT_DRILL_MINUTES)),
        }

    def _update_config(self, guild_id: int, **fields: Any) -> dict[str, Any]:
        config = self.get_config(guild_id)
        config.update(fields)
        self._guild_data(guild_id)["config"] = config
        self.save()
        return config

    def set_users(self, guild_id: int, user_ids: list[int]) -> None:
        self._update_config(guild_id, users=sorted(set(user_ids)))

    def set_roles(self, guild_id: int, role_ids: list[int]) -> None:
        self._update_config(guild_id, roles=sorted(set(role_ids)))

    def add_user(self, guild_id: int, user_id: int) -> bool:
        users = self.get_config(guild_id)["users"]
        if user_id in users:
            return False
        self.set_users(guild_id, [*users, user_id])
        return True

    def remove_user(self, guild_id: int, user_id: int) -> bool:
        users = self.get_config(guild_id)["users"]
        if user_id not in users:
            return False
        self.set_users(guild_id, [uid for uid in users if uid != user_id])
        return True

    def add_role(self, guild_id: int, role_id: int) -> bool:
        roles = self.get_config(guild_id)["roles"]
        if role_id in roles:
            return False
        self.set_roles(guild_id, [*roles, role_id])
        return True

    def remove_role(self, guild_id: int, role_id: int) -> bool:
        roles = self.get_config(guild_id)["roles"]
        if role_id not in roles:
            return False
        self.set_roles(guild_id, [rid for rid in roles if rid != role_id])
        return True

    def set_new_account_days(self, guild_id: int, days: int) -> None:
        self._update_config(guild_id, new_account_days=days)

    def set_drill_minutes(self, guild_id: int, minutes: int) -> None:
        self._update_config(guild_id, drill_minutes=minutes)

    # -- Sesión ------------------------------------------------------------

    def get_session(self, guild_id: int) -> dict[str, Any] | None:
        session = self._guilds.get(str(guild_id), {}).get("session")
        return session if isinstance(session, dict) else None

    def is_active(self, guild_id: int) -> bool:
        session = self.get_session(guild_id)
        return session is not None and session.get("active") is True

    def active_guild_ids(self) -> list[int]:
        return [
            int(guild_key)
            for guild_key, guild_data in self._guilds.items()
            if isinstance(guild_data.get("session"), dict)
            and guild_data["session"].get("active") is True
        ]

    def begin_session(
        self,
        guild_id: int,
        *,
        started_by: int,
        origin_channel_id: int,
        drill: bool,
        exempt_bot_ids: list[int],
        drill_ends_at: float | None,
    ) -> dict[str, Any]:
        session = {
            "active": True,
            "drill": drill,
            "started_by": started_by,
            "started_at": time.time(),
            "drill_ends_at": drill_ends_at,
            "ended_at": None,
            "ended_by": None,
            "origin_channel_id": origin_channel_id,
            "text_channel_id": None,
            "voice_channel_id": None,
            "exempt_bot_ids": exempt_bot_ids,
            "exempt_role_ids": [],
            "exempt_member_ids": [],
            "onboarding": None,
            "onboarding_pending": None,
            "unverified_channels": [],
            "reexposed": [],
            "changes": [],
            "failed_channels": [],
            "moved_members": {},
            "users": {},
            "joins": {},
            "actions": [],
        }
        self._guild_data(guild_id)["session"] = session
        self._notice_counters[guild_id] = 0
        self._rate_windows = {
            key: window for key, window in self._rate_windows.items() if key[0] != guild_id
        }
        self.save()
        return session

    def update_session(self, guild_id: int, **fields: Any) -> None:
        session = self.get_session(guild_id)
        if session is None:
            return
        session.update(fields)
        self.save()

    def record_changes(self, guild_id: int, changes: list[OverwriteChange]) -> None:
        session = self.get_session(guild_id)
        if session is None or not changes:
            return
        session["changes"].extend(change.to_payload() for change in changes)
        self.save()

    def get_changes(self, guild_id: int) -> list[OverwriteChange]:
        session = self.get_session(guild_id) or {}
        return [
            OverwriteChange.from_payload(payload)
            for payload in session.get("changes", [])
        ]

    def record_failed_channel(self, guild_id: int, channel_id: int) -> None:
        session = self.get_session(guild_id)
        if session is None:
            return
        session["failed_channels"].append(channel_id)
        self.save()

    def record_reexposure(
        self,
        guild_id: int,
        channel_id: int,
        actor_id: int | None,
        actor_name: str | None,
    ) -> None:
        session = self.get_session(guild_id)
        if session is None:
            return
        session.setdefault("reexposed", []).append(
            {
                "channel_id": channel_id,
                "actor_id": actor_id,
                "actor_name": actor_name,
                "at": time.time(),
            }
        )
        self.save()

    def record_moved_member(self, guild_id: int, member_id: int, channel_id: int) -> None:
        session = self.get_session(guild_id)
        if session is None:
            return
        session["moved_members"][str(member_id)] = channel_id
        self.save()

    def end_session(self, guild_id: int, ended_by: int | None) -> None:
        session = self.get_session(guild_id)
        if session is None:
            return
        session["active"] = False
        session["ended_at"] = time.time()
        session["ended_by"] = ended_by
        self._notice_counters.pop(guild_id, None)
        self.save()

    def bump_notice_counter(self, guild_id: int, every: int) -> bool:
        """Suma un mensaje y devuelve True cuando toca repetir el aviso."""
        count = self._notice_counters.get(guild_id, 0) + 1
        if count >= every:
            self._notice_counters[guild_id] = 0
            return True
        self._notice_counters[guild_id] = count
        return False

    # -- Estadísticas ------------------------------------------------------

    def record_message(self, message: discord.Message, mass_mention: bool) -> None:
        guild = message.guild
        session = self.get_session(guild.id) if guild else None
        if session is None or not session.get("active"):
            return

        author = message.author
        now = time.time()
        entry = session["users"].setdefault(
            str(author.id),
            {
                "name": str(author),
                "messages": 0,
                "first_at": now,
                "last_at": now,
                "mentions": 0,
                "links": 0,
                "attachments": 0,
                "duplicates": 0,
                "mass_mentions": 0,
                "peak_per_minute": 0,
                "channels": [],
                "default_avatar": author.avatar is None,
                "last_content": None,
            },
        )

        content = message.content.strip()
        entry["name"] = str(author)
        entry["messages"] += 1
        entry["last_at"] = now
        entry["mentions"] += len(message.mentions) + len(message.role_mentions)
        entry["links"] += len(LINK_PATTERN.findall(content))
        entry["attachments"] += len(message.attachments)
        if mass_mention:
            entry["mass_mentions"] += 1
        if content and content.casefold() == entry.get("last_content"):
            entry["duplicates"] += 1
        entry["last_content"] = content.casefold()[:300] if content else None
        if message.channel.id not in entry["channels"]:
            entry["channels"].append(message.channel.id)

        window = self._rate_windows.setdefault((guild.id, author.id), deque())
        window.append(now)
        while window and now - window[0] > RATE_WINDOW_SECONDS:
            window.popleft()
        entry["peak_per_minute"] = max(int(entry["peak_per_minute"]), len(window))

        self._save_stats_soon()

    def record_join(self, member: discord.Member) -> None:
        session = self.get_session(member.guild.id)
        if session is None or not session.get("active"):
            return

        session["joins"][str(member.id)] = {
            "name": str(member),
            "joined_at": time.time(),
            "default_avatar": member.avatar is None,
            "bot": member.bot,
            "left_at": None,
        }
        self._save_stats_soon()

    def record_leave(self, member: discord.Member) -> None:
        session = self.get_session(member.guild.id)
        if session is None or not session.get("active"):
            return

        join = session["joins"].get(str(member.id))
        if join is not None:
            join["left_at"] = time.time()
            self._save_stats_soon()

    def record_action(
        self,
        guild_id: int,
        action: str,
        user_id: int,
        name: str,
        by: int | None,
    ) -> None:
        session = self.get_session(guild_id)
        if session is None:
            return

        session["actions"].append(
            {
                "action": action,
                "user_id": user_id,
                "name": name,
                "by": by,
                "at": time.time(),
            }
        )
        self.save()
