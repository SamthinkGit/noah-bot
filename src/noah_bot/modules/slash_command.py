"""Trigger Discord application (slash) commands with a synced Autogami token.

Discord clients do not "type" a slash command: they look the command up in the
channel and then post an interaction to `/api/v9/interactions`. Both steps are
reproduced here so Autogami can fire commands such as Disboard's `/bump`.
"""

import base64
import json
import secrets
import time
from typing import Any
from urllib.parse import urlencode

from noah_bot.modules.send_message import _request_with_redirects


DISCORD_API_HOST = "discord.com"
DISCORD_API_PREFIX = "/api/v9"
DISCORD_EPOCH_MS = 1_420_070_400_000
CHROME_VERSION = "128.0.0.0"
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    f"(KHTML, like Gecko) Chrome/{CHROME_VERSION} Safari/537.36"
)
CHAT_INPUT_COMMAND_TYPE = 1
APPLICATION_COMMAND_INTERACTION_TYPE = 2


class SlashCommandError(RuntimeError):
    """Raised when a slash command cannot be resolved or dispatched."""


def _super_properties() -> str:
    properties = {
        "os": "Windows",
        "browser": "Chrome",
        "device": "",
        "system_locale": "en-US",
        "browser_user_agent": BROWSER_USER_AGENT,
        "browser_version": CHROME_VERSION,
        "os_version": "10",
        "referrer": "",
        "referring_domain": "",
        "referrer_current": "",
        "referring_domain_current": "",
        "release_channel": "stable",
        "client_build_number": 333_000,
        "client_event_source": None,
    }
    encoded = json.dumps(properties, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(encoded).decode("ascii")


def _build_headers(
    discord_token: str,
    server_id: str,
    channel_id: str,
    content_type: str,
) -> dict[str, str]:
    return {
        "authorization": discord_token,
        "content-type": content_type,
        "host": DISCORD_API_HOST,
        "origin": "https://discord.com",
        "referer": f"https://discord.com/channels/{server_id}/{channel_id}",
        "user-agent": BROWSER_USER_AGENT,
        "x-discord-locale": "en-US",
        "x-super-properties": _super_properties(),
    }


def _generate_nonce() -> str:
    return str((int(time.time() * 1000) - DISCORD_EPOCH_MS) << 22)


def _generate_session_id() -> str:
    return secrets.token_hex(16)


def _build_multipart_body(payload: dict[str, Any]) -> tuple[str, str]:
    boundary = f"----NoahAutogamiBoundary{secrets.token_hex(8)}"
    payload_json = json.dumps(payload, separators=(",", ":"), ensure_ascii=True)
    body = "".join(
        [
            f"--{boundary}\r\n",
            'Content-Disposition: form-data; name="payload_json"\r\n\r\n',
            f"{payload_json}\r\n",
            f"--{boundary}--\r\n",
        ]
    )
    return f"multipart/form-data; boundary={boundary}", body


def search_application_command(
    command_name: str,
    application_id: str,
    discord_token: str,
    server_id: str,
    channel_id: str,
) -> dict[str, Any]:
    """Resolve a slash command into the payload Discord expects back."""

    query = urlencode(
        {
            "type": CHAT_INPUT_COMMAND_TYPE,
            "query": command_name,
            "limit": 25,
            "include_applications": "false",
            "application_id": application_id,
        }
    )
    headers = _build_headers(
        discord_token,
        server_id,
        channel_id,
        "application/json",
    )
    path = f"{DISCORD_API_PREFIX}/channels/{channel_id}/application-commands/search?{query}"
    status, body = _request_with_redirects(
        "GET",
        DISCORD_API_HOST,
        path,
        "",
        headers,
    )
    if not 200 <= status < 300:
        raise SlashCommandError(
            f"No he podido listar los comandos del canal (HTTP {status}): {body[:200]}"
        )

    try:
        found_commands = json.loads(body).get("application_commands") or []
    except (json.JSONDecodeError, AttributeError) as exc:
        raise SlashCommandError("La respuesta de Discord no era JSON válido.") from exc

    for command in found_commands:
        if not isinstance(command, dict):
            continue
        if command.get("name") != command_name:
            continue
        if str(command.get("application_id")) != str(application_id):
            continue
        return command

    raise SlashCommandError(
        f"El comando `/{command_name}` no está disponible en este canal."
    )


def trigger_slash_command(
    command_name: str,
    application_id: str,
    discord_token: str,
    server_id: str,
    channel_id: str,
    options: list[dict[str, Any]] | None = None,
) -> tuple[int, str]:
    """Fire a slash command as the token owner and return the HTTP result."""

    command = search_application_command(
        command_name,
        application_id,
        discord_token,
        server_id,
        channel_id,
    )
    payload = {
        "type": APPLICATION_COMMAND_INTERACTION_TYPE,
        "application_id": str(application_id),
        "guild_id": str(server_id),
        "channel_id": str(channel_id),
        "session_id": _generate_session_id(),
        "nonce": _generate_nonce(),
        "data": {
            "version": command["version"],
            "id": command["id"],
            "name": command["name"],
            "type": command.get("type", CHAT_INPUT_COMMAND_TYPE),
            "options": options or [],
            "application_command": command,
            "attachments": [],
        },
    }
    content_type, body = _build_multipart_body(payload)
    headers = _build_headers(discord_token, server_id, channel_id, content_type)
    return _request_with_redirects(
        "POST",
        DISCORD_API_HOST,
        f"{DISCORD_API_PREFIX}/interactions",
        body,
        headers,
    )
