"""Trigger Discord application (slash) commands with a synced Autogami token.

Discord clients do not "type" a slash command: they look the command up in the
channel and then post an interaction to `/api/v9/interactions`. Both steps are
reproduced here so Autogami can fire commands such as Disboard's `/bump`.

Every request travels with the *user* token passed in, never with the bot one,
so Discord sees the interaction exactly as if the token owner had typed it.
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
    content_type: str | None = None,
) -> dict[str, str]:
    headers = {
        "authorization": discord_token,
        "host": DISCORD_API_HOST,
        "origin": "https://discord.com",
        "referer": f"https://discord.com/channels/{server_id}/{channel_id}",
        "user-agent": BROWSER_USER_AGENT,
        "x-discord-locale": "en-US",
        "x-super-properties": _super_properties(),
    }
    if content_type:
        headers["content-type"] = content_type
    return headers


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


def _snippet(body: str) -> str:
    collapsed = " ".join(body.split())
    return collapsed[:160] if collapsed else "sin cuerpo"


def _extract_commands(body: str) -> list[dict[str, Any]] | None:
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return None

    if not isinstance(parsed, dict):
        return None

    found = parsed.get("application_commands")
    if not isinstance(found, list):
        return None

    return [command for command in found if isinstance(command, dict)]


def _match_command(
    found_commands: list[dict[str, Any]],
    command_name: str,
    application_id: str,
) -> dict[str, Any] | None:
    for command in found_commands:
        if command.get("name") != command_name:
            continue
        if str(command.get("application_id")) != str(application_id):
            continue
        if "id" not in command or "version" not in command:
            continue
        return command
    return None


def _command_sources(
    command_name: str,
    application_id: str,
    server_id: str,
    channel_id: str,
) -> list[tuple[str, str]]:
    """Endpoints a real client uses to discover slash commands, best first.

    The channel search can legitimately come back empty (it is scoped by what
    the client has already paged in), so the guild command index is kept as a
    last resort: it lists everything installed in the guild.
    """

    search_path = f"{DISCORD_API_PREFIX}/channels/{channel_id}/application-commands/search"
    shared_query = {
        "type": CHAT_INPUT_COMMAND_TYPE,
        "include_applications": "true",
        "limit": 25,
    }
    return [
        (
            "busqueda en el canal filtrando por la app",
            f"{search_path}?{urlencode({**shared_query, 'query': command_name, 'application_id': application_id})}",
        ),
        (
            "busqueda en el canal sin filtro de app",
            f"{search_path}?{urlencode({**shared_query, 'query': command_name})}",
        ),
        (
            "indice de comandos del servidor",
            f"{DISCORD_API_PREFIX}/guilds/{server_id}/application-command-index",
        ),
    ]


def resolve_application_command(
    command_name: str,
    application_id: str,
    discord_token: str,
    server_id: str,
    channel_id: str,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Look the command up, reporting what every endpoint answered."""

    attempts: list[str] = []
    sources = _command_sources(command_name, application_id, server_id, channel_id)

    for label, path in sources:
        headers = _build_headers(discord_token, server_id, channel_id)
        status, body = _request_with_redirects(
            "GET",
            DISCORD_API_HOST,
            path,
            "",
            headers,
        )

        if not 200 <= status < 300:
            attempts.append(f"{label}: HTTP {status} - {_snippet(body)}")
            continue

        found_commands = _extract_commands(body)
        if found_commands is None:
            attempts.append(f"{label}: respuesta inesperada - {_snippet(body)}")
            continue

        command = _match_command(found_commands, command_name, application_id)
        if command is not None:
            attempts.append(f"{label}: encontrado (id {command['id']})")
            return command, attempts

        visible_names = sorted({str(item.get("name")) for item in found_commands})
        names_hint = ", ".join(visible_names[:10]) if visible_names else "ninguno"
        attempts.append(
            f"{label}: {len(found_commands)} comandos y ninguno es "
            f"/{command_name} de la app {application_id} (visibles: {names_hint})"
        )

    return None, attempts


def search_application_command(
    command_name: str,
    application_id: str,
    discord_token: str,
    server_id: str,
    channel_id: str,
) -> dict[str, Any]:
    """Resolve a slash command into the payload Discord expects back."""

    command, attempts = resolve_application_command(
        command_name,
        application_id,
        discord_token,
        server_id,
        channel_id,
    )
    if command is not None:
        return command

    detail = "\n".join(f"- {attempt}" for attempt in attempts)
    raise SlashCommandError(
        f"No he podido resolver `/{command_name}` con tu token de usuario.\n{detail}"
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
