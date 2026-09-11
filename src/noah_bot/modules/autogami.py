import base64
import hashlib
import hmac
import json
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


AUTOGAMI_PASSWORD = "autogami"
PBKDF2_ITERATIONS = 200_000


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _xor_bytes(left: bytes, right: bytes) -> bytes:
    return bytes(a ^ b for a, b in zip(left, right))


def _derive_key(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        PBKDF2_ITERATIONS,
        dklen=32,
    )


def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    output = bytearray()
    counter = 0

    while len(output) < length:
        block = hashlib.sha256(
            key + nonce + counter.to_bytes(8, byteorder="big")
        ).digest()
        output.extend(block)
        counter += 1

    return bytes(output[:length])


def _encrypt_payload(data: dict[str, Any], password: str) -> dict[str, str | int]:
    plaintext = json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    salt = secrets.token_bytes(16)
    nonce = secrets.token_bytes(16)
    key = _derive_key(password, salt)
    ciphertext = _xor_bytes(plaintext, _keystream(key, nonce, len(plaintext)))
    mac = hmac.new(key, nonce + ciphertext, hashlib.sha256).digest()

    return {
        "version": 1,
        "salt": base64.b64encode(salt).decode("ascii"),
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
        "mac": base64.b64encode(mac).decode("ascii"),
    }


def _decrypt_payload(payload: dict[str, Any], password: str) -> dict[str, Any]:
    salt = base64.b64decode(payload["salt"])
    nonce = base64.b64decode(payload["nonce"])
    ciphertext = base64.b64decode(payload["ciphertext"])
    received_mac = base64.b64decode(payload["mac"])
    key = _derive_key(password, salt)
    expected_mac = hmac.new(key, nonce + ciphertext, hashlib.sha256).digest()

    if not hmac.compare_digest(received_mac, expected_mac):
        raise ValueError("Autogami token store integrity check failed.")

    plaintext = _xor_bytes(ciphertext, _keystream(key, nonce, len(ciphertext)))
    data = json.loads(plaintext.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Autogami token store has an invalid format.")
    return data


class AutogamiTokenStore:
    def __init__(
        self,
        json_path: str = "autogami_tokens.json",
        password: str = AUTOGAMI_PASSWORD,
    ):
        self.json_path = Path(json_path)
        self.password = password
        self._state = self._load()
        self._users: dict[str, dict[str, Any]] = self._state["users"]
        self._guilds: dict[str, dict[str, Any]] = self._state["guilds"]

    def _default_state(self) -> dict[str, dict[str, Any]]:
        return {"users": {}, "guilds": {}}

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self.json_path.exists():
            return self._default_state()

        try:
            with self.json_path.open("r", encoding="utf-8") as file:
                payload = json.load(file)
        except (OSError, json.JSONDecodeError):
            return self._default_state()

        try:
            state = _decrypt_payload(payload, self.password)
        except (KeyError, ValueError, TypeError):
            return self._default_state()

        users = state.get("users")
        guilds = state.get("guilds")
        return {
            "users": users if isinstance(users, dict) else {},
            "guilds": guilds if isinstance(guilds, dict) else {},
        }

    def _save(self) -> None:
        payload = _encrypt_payload(self._state, self.password)
        with self.json_path.open("w", encoding="utf-8") as file:
            json.dump(payload, file, indent=2, ensure_ascii=False)

    def _user_key(self, user_id: int) -> str:
        return str(user_id)

    def _guild_key(self, guild_id: int) -> str:
        return str(guild_id)

    def set_token(self, user_id: int, token: str, username: str | None = None) -> None:
        user_data = self._users.get(self._user_key(user_id), {})
        user_data["token"] = token
        user_data["updated_at"] = _utc_now_iso()
        if username:
            user_data["username"] = username
        self._users[self._user_key(user_id)] = user_data
        self._save()

    def get_token(self, user_id: int) -> str | None:
        user_data = self._users.get(self._user_key(user_id))
        if not isinstance(user_data, dict):
            return None

        token = user_data.get("token")
        if not isinstance(token, str) or not token.strip():
            return None
        return token

    def add_favorite_emoji(self, user_id: int, emoji: str) -> bool:
        sanitized_emoji = emoji.strip()
        if not sanitized_emoji:
            return False

        user_data = self._users.get(self._user_key(user_id), {})
        favorites = user_data.get("favorite_emojis")
        if not isinstance(favorites, list):
            favorites = []

        if sanitized_emoji in favorites:
            return False

        favorites.append(sanitized_emoji)
        user_data["favorite_emojis"] = favorites
        user_data["updated_at"] = _utc_now_iso()
        self._users[self._user_key(user_id)] = user_data
        self._save()
        return True

    def get_favorite_emojis(self, user_id: int) -> list[str]:
        user_data = self._users.get(self._user_key(user_id))
        if not isinstance(user_data, dict):
            return []

        favorites = user_data.get("favorite_emojis")
        if not isinstance(favorites, list):
            return []

        return [emoji for emoji in favorites if isinstance(emoji, str) and emoji.strip()]

    def remove_favorite_emoji(self, user_id: int, emoji: str) -> bool:
        sanitized_emoji = emoji.strip()
        if not sanitized_emoji:
            return False

        user_data = self._users.get(self._user_key(user_id))
        if not isinstance(user_data, dict):
            return False

        favorites = user_data.get("favorite_emojis")
        if not isinstance(favorites, list):
            return False

        filtered_favorites = [
            favorite
            for favorite in favorites
            if isinstance(favorite, str) and favorite.strip() != sanitized_emoji
        ]
        if len(filtered_favorites) == len(favorites):
            return False

        user_data["favorite_emojis"] = filtered_favorites
        user_data["updated_at"] = _utc_now_iso()
        self._users[self._user_key(user_id)] = user_data
        self._save()
        return True

    def set_chest_emoji(self, guild_id: int, emoji: str) -> bool:
        sanitized_emoji = emoji.strip()
        if not sanitized_emoji:
            return False

        guild_data = self._guilds.get(self._guild_key(guild_id), {})
        guild_data["chest_emoji"] = sanitized_emoji
        guild_data["updated_at"] = _utc_now_iso()
        self._guilds[self._guild_key(guild_id)] = guild_data
        self._save()
        return True

    def get_chest_emoji(self, guild_id: int) -> str | None:
        guild_data = self._guilds.get(self._guild_key(guild_id))
        if not isinstance(guild_data, dict):
            return None

        emoji = guild_data.get("chest_emoji")
        if not isinstance(emoji, str) or not emoji.strip():
            return None
        return emoji.strip()

    def set_autobump(self, guild_id: int, channel_id: int, user_id: int) -> None:
        guild_data = self._guilds.get(self._guild_key(guild_id), {})
        guild_data["autobump"] = {
            "channel_id": str(channel_id),
            "user_id": str(user_id),
            "next_bump_at": None,
            "started_at": _utc_now_iso(),
        }
        guild_data["updated_at"] = _utc_now_iso()
        self._guilds[self._guild_key(guild_id)] = guild_data
        self._save()

    def set_autobump_next_bump(self, guild_id: int, next_bump_at: str | None) -> None:
        guild_data = self._guilds.get(self._guild_key(guild_id))
        if not isinstance(guild_data, dict):
            return

        autobump = guild_data.get("autobump")
        if not isinstance(autobump, dict):
            return

        autobump["next_bump_at"] = next_bump_at
        guild_data["updated_at"] = _utc_now_iso()
        self._save()

    def get_autobump(self, guild_id: int) -> dict[str, Any] | None:
        guild_data = self._guilds.get(self._guild_key(guild_id))
        if not isinstance(guild_data, dict):
            return None

        autobump = guild_data.get("autobump")
        if not isinstance(autobump, dict):
            return None

        try:
            channel_id = int(autobump["channel_id"])
            user_id = int(autobump["user_id"])
        except (KeyError, TypeError, ValueError):
            return None

        next_bump_at = autobump.get("next_bump_at")
        return {
            "channel_id": channel_id,
            "user_id": user_id,
            "next_bump_at": next_bump_at if isinstance(next_bump_at, str) else None,
        }

    def clear_autobump(self, guild_id: int) -> bool:
        guild_data = self._guilds.get(self._guild_key(guild_id))
        if not isinstance(guild_data, dict) or "autobump" not in guild_data:
            return False

        del guild_data["autobump"]
        guild_data["updated_at"] = _utc_now_iso()
        self._save()
        return True

    def get_autobump_guild_ids(self) -> list[int]:
        guild_ids = []
        for guild_key, guild_data in self._guilds.items():
            if not isinstance(guild_data, dict):
                continue
            if not isinstance(guild_data.get("autobump"), dict):
                continue
            try:
                guild_ids.append(int(guild_key))
            except (TypeError, ValueError):
                continue
        return guild_ids
