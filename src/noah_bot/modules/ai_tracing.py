"""Registro de las llamadas de IA en LangSmith (opcional).

Se activa poniendo la API key de LangSmith en `.noah ai config` (o con
LANGSMITH_API_KEY / LANGCHAIN_API_KEY en el .env). Ojo: se envían a LangSmith las
preguntas, los mensajes que lee Noah y sus respuestas.
"""

from __future__ import annotations

import logging
from typing import Any, ContextManager

from langsmith import Client, tracing_context

from noah_bot.modules.ai_settings import AiSettingsStore

_log = logging.getLogger(__name__)
_clients: dict[str, Client] = {}


def _client(api_key: str) -> Client:
    client = _clients.get(api_key)
    if client is None:
        client = Client(api_key=api_key)
        _clients.clear()
        _clients[api_key] = client
    return client


def tracing(
    settings: AiSettingsStore | None,
    *,
    tags: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> ContextManager[None]:
    """Contexto de trazado: activo solo si hay key de LangSmith y está encendido."""
    if settings is None or not settings.langsmith_enabled:
        return tracing_context(enabled=False)
    try:
        client = _client(settings.langsmith_api_key or "")
    except Exception as error:  # noqa: BLE001 - el registro nunca debe romper a Noah
        _log.warning("No he podido preparar LangSmith: %s", error)
        return tracing_context(enabled=False)
    return tracing_context(
        enabled=True,
        client=client,
        project_name=settings.langsmith_project,
        tags=tags or [],
        metadata=metadata or {},
    )
