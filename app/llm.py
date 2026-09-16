"""The chat model, selected by configuration.

Two providers are supported so the app runs wherever a key happens to exist:
Groq by default (fast and free-tier friendly) and Anthropic when a key is set.

The Groq model id is **resolved from the live catalogue at startup**, not
hardcoded. Groq retires models regularly -- at the time of writing its
catalogue contains 13 models and none of the `llama-3.3-*` ids that most
tutorials still name -- so a pinned id is a time bomb. Set `GROQ_MODEL` in
`.env` to override the automatic choice.
"""

from __future__ import annotations

import logging

from langchain_core.language_models import BaseChatModel

from app.config import settings

logger = logging.getLogger(__name__)

#: Groq chat models that reliably support tool calling, best first. The first
#: id present in the live catalogue wins.
PREFERRED_GROQ_MODELS: tuple[str, ...] = (
    "openai/gpt-oss-120b",
    "qwen/qwen3.8-27b",
    "openai/gpt-oss-20b",
)

#: Substrings marking catalogue entries that are not general chat models:
#: speech-to-text, text-to-speech and safety classifiers.
_NOT_CHAT_MODELS: tuple[str, ...] = (
    "whisper",
    "orpheus",
    "prompt-guard",
    "safeguard",
    "tts",
    "embed",
    "guard",
)

#: Groq's "compound" entries are agentic systems with their own built-in tools.
#: This application supplies its own tools and does its own tool selection, so
#: mixing the two would muddy which tool produced which fact.
_EXCLUDED_GROQ_PREFIXES: tuple[str, ...] = ("groq/compound",)

_resolved_groq_model: str | None = None


class LlmNotConfigured(RuntimeError):
    """Raised when the selected provider has no usable credentials."""


def _require_key() -> str:
    key = settings.llm_api_key
    if not key:
        raise LlmNotConfigured(
            f"LLM_PROVIDER is {settings.llm_provider!r} but "
            f"{settings.llm_api_key_env} is not set. Add it to .env "
            f"(copy .env.example) or switch LLM_PROVIDER."
        )
    return key


def _looks_like_chat_model(model_id: str) -> bool:
    lowered = model_id.lower()
    if any(marker in lowered for marker in _NOT_CHAT_MODELS):
        return False
    return not lowered.startswith(_EXCLUDED_GROQ_PREFIXES)


def list_groq_models() -> list[str]:
    """Ids currently offered by the Groq API."""
    from groq import Groq

    client = Groq(api_key=_require_key())
    return sorted(model.id for model in client.models.list().data)


def resolve_groq_model(refresh: bool = False) -> str:
    """Pick a tool-calling Groq model, preferring the configured one.

    Falls back to the first preference if the catalogue cannot be reached, so a
    transient network failure degrades to a sensible default rather than
    preventing startup.
    """
    global _resolved_groq_model
    if _resolved_groq_model and not refresh:
        return _resolved_groq_model

    configured = (settings.groq_model or "").strip()

    try:
        available = list_groq_models()
    except LlmNotConfigured:
        raise
    except Exception as exc:
        fallback = configured or PREFERRED_GROQ_MODELS[0]
        logger.warning(
            "Could not read the Groq model catalogue (%s); falling back to %r.",
            exc,
            fallback,
        )
        _resolved_groq_model = fallback
        return fallback

    if configured:
        if configured not in available:
            logger.warning(
                "GROQ_MODEL=%r is not in the Groq catalogue. Available chat "
                "models: %s. Using it anyway; unset GROQ_MODEL to auto-select.",
                configured,
                ", ".join(m for m in available if _looks_like_chat_model(m)),
            )
        _resolved_groq_model = configured
        return configured

    for candidate in PREFERRED_GROQ_MODELS:
        if candidate in available:
            logger.info("Resolved Groq model %r from the live catalogue.", candidate)
            _resolved_groq_model = candidate
            return candidate

    for candidate in available:
        if _looks_like_chat_model(candidate):
            logger.info(
                "None of the preferred Groq models are available; using %r.",
                candidate,
            )
            _resolved_groq_model = candidate
            return candidate

    raise LlmNotConfigured(
        "The Groq catalogue contains no usable chat model. "
        f"It returned: {', '.join(available) or '(nothing)'}"
    )


def active_model_name() -> str:
    """The model id that `get_chat_model()` will use."""
    if settings.llm_provider == "groq":
        return resolve_groq_model()
    return settings.anthropic_model


def get_chat_model(**overrides) -> BaseChatModel:
    """Build the chat model for the configured provider.

    `temperature=0` by default: this assistant reports retrieved facts and
    tool results, so run-to-run variation in what it claims is a liability
    rather than a feature.
    """
    key = _require_key()
    options = {"temperature": 0, **overrides}

    if settings.llm_provider == "groq":
        from langchain_groq import ChatGroq

        # Groq's free tier meters tokens per minute; a burst of retrieval-heavy
        # turns hits 429. The SDK backs off and retries, which turns a
        # transient limit into a slower answer rather than a failed one.
        options.setdefault("max_retries", 5)
        return ChatGroq(model=resolve_groq_model(), api_key=key, **options)

    from langchain_anthropic import ChatAnthropic

    return ChatAnthropic(
        model=settings.anthropic_model,
        api_key=key,
        max_tokens=options.pop("max_tokens", 8192),
        **options,
    )


def describe() -> dict:
    """LLM status for GET /health. Never includes the key."""
    configured = bool(settings.llm_api_key)
    info: dict = {
        "provider": settings.llm_provider,
        "key_env_var": settings.llm_api_key_env,
        "key_configured": configured,
    }
    if not configured:
        info["model"] = None
        info["error"] = f"{settings.llm_api_key_env} is not set"
        return info
    try:
        info["model"] = active_model_name()
    except Exception as exc:
        info["model"] = None
        info["error"] = str(exc)
    return info
