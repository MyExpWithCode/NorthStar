"""LangSmith tracing.

Worth having here for a specific reason: the interesting question about this
application is never "what did it say" but **"which tools did it choose, what
did they return, and did the answer actually follow from that"**. A trace shows
the whole agent loop -- each tool call with its arguments and its result, the
prompts as sent, and the token counts -- which is exactly the evidence needed to
tell a grounded answer from a plausible one.

LangChain reads its tracing configuration from `os.environ`, not from our
settings object, so `configure()` copies the values across. Without that, the
documented setup of putting keys in `.env` would silently not enable tracing.
"""

from __future__ import annotations

import logging
import os

from app.config import settings

logger = logging.getLogger(__name__)

#: Populated by `configure()` so `/health` can report the truth rather than the
#: intent.
_state: dict[str, object] = {"enabled": False, "reason": "not configured yet"}


def configure() -> dict:
    """Export LangSmith settings into the environment LangChain reads.

    Returns the resulting state. Never raises: tracing is an aid, and failing
    to set it up must not stop the assistant from answering.
    """
    global _state

    if not settings.langsmith_tracing:
        _state = {"enabled": False, "reason": "LANGSMITH_TRACING is not true"}
        return dict(_state)

    key = settings.langsmith_api_key
    secret = key.get_secret_value().strip() if key else ""
    if not secret:
        _state = {
            "enabled": False,
            "reason": "LANGSMITH_TRACING is true but LANGSMITH_API_KEY is empty",
        }
        logger.warning(_state["reason"])
        return dict(_state)

    os.environ["LANGSMITH_TRACING"] = "true"
    os.environ["LANGSMITH_API_KEY"] = secret
    os.environ["LANGSMITH_PROJECT"] = settings.langsmith_project
    os.environ["LANGSMITH_ENDPOINT"] = settings.langsmith_endpoint
    # LangChain still honours the older names in places; set both so the
    # behaviour does not depend on which one a given version reads.
    os.environ["LANGCHAIN_TRACING_V2"] = "true"
    os.environ["LANGCHAIN_PROJECT"] = settings.langsmith_project

    _state = {
        "enabled": True,
        "project": settings.langsmith_project,
        "endpoint": settings.langsmith_endpoint,
        "reason": None,
    }
    logger.info(
        "LangSmith tracing enabled, project %r", settings.langsmith_project
    )
    return dict(_state)


def describe() -> dict:
    """Tracing status for `GET /health`. Never includes the key."""
    return dict(_state)


def run_url(run) -> str | None:
    """A clickable LangSmith URL for one traced run.

    Returns None when tracing is off or the URL cannot be built -- a missing
    link is a cosmetic loss, so this never raises into a chat turn.
    """
    if not _state.get("enabled") or run is None:
        return None
    try:
        from langsmith import Client

        return Client().get_run_url(run=run)
    except Exception as exc:
        logger.debug("Could not build a LangSmith run URL: %s", exc)
        return None
