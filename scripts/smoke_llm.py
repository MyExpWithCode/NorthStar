"""Prove the configured LLM answers and can call tools.

    python scripts/smoke_llm.py

Tool calling is the part worth checking: the whole design depends on the model
choosing between the knowledge-base tool and the MCP tools, so a model that
cannot emit a tool call would break everything downstream of here.
"""

from __future__ import annotations

import sys as _sys

# The assistant's provenance labels are emoji; a Windows cp1252 console would
# raise UnicodeEncodeError when printing them.
for _stream in (_sys.stdout, _sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

import logging
import sys

from langchain_core.tools import tool

from app import llm
from app.config import settings


@tool
def add_two_numbers(a: float, b: float) -> float:
    """Add two numbers together and return the sum."""
    return a + b


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    failures: list[str] = []

    print("--- configuration ---")
    status = llm.describe()
    for key, value in status.items():
        print(f"  {key}: {value}")
    if not status["key_configured"]:
        print(f"\n{settings.llm_api_key_env} is not set; cannot run.")
        return 1
    if status.get("error"):
        failures.append(status["error"])

    print()
    print("--- Groq catalogue (why the model id is resolved, not pinned) ---")
    if settings.llm_provider == "groq":
        try:
            available = llm.list_groq_models()
            print(f"  {len(available)} models offered")
            print(f"  preferred order : {list(llm.PREFERRED_GROQ_MODELS)}")
            print(f"  resolved to     : {llm.resolve_groq_model()}")
            stale = [m for m in ("llama-3.3-70b-versatile", "mixtral-8x7b-32768")
                     if m in available]
            print(f"  commonly-cited ids still present: {stale or 'none'}")
        except Exception as exc:
            failures.append(f"catalogue read failed: {exc}")

    print()
    print("--- plain completion ---")
    model = llm.get_chat_model()
    try:
        reply = model.invoke(
            "Reply with exactly one word: the capital city of Singapore."
        )
        text = (reply.content if isinstance(reply.content, str)
                else str(reply.content)).strip()
        print(f"  model: {llm.active_model_name()}")
        print(f"  reply: {text[:120]}")
        if not text:
            failures.append("empty completion")
    except Exception as exc:
        failures.append(f"completion failed: {type(exc).__name__}: {exc}")
        print(f"  FAILED: {exc}")

    print()
    print("--- tool calling ---")
    try:
        bound = model.bind_tools([add_two_numbers])
        response = bound.invoke("What is 17 plus 25? Use the tool to work it out.")
        calls = response.tool_calls or []
        print(f"  tool_calls: {calls}")
        if not calls:
            failures.append("model did not emit a tool call")
        else:
            call = calls[0]
            if call["name"] != "add_two_numbers":
                failures.append(f"wrong tool called: {call['name']}")
            args = call["args"]
            if {float(args.get("a", 0)), float(args.get("b", 0))} != {17.0, 25.0}:
                failures.append(f"wrong arguments passed: {args}")
            else:
                print(f"  arguments parsed correctly: {args}")
                print(f"  executing tool -> {add_two_numbers.invoke(args)}")
    except Exception as exc:
        failures.append(f"tool calling failed: {type(exc).__name__}: {exc}")
        print(f"  FAILED: {exc}")

    print()
    print("--- missing key must fail fast with the variable named ---")
    try:
        import app.config as config_module

        saved = config_module.settings.groq_api_key
        config_module.settings.groq_api_key = None
        try:
            llm.get_chat_model()
            failures.append("missing key did not raise")
        except llm.LlmNotConfigured as exc:
            print(f"  {exc}")
            if settings.llm_api_key_env not in str(exc):
                failures.append("error did not name the env var")
        finally:
            config_module.settings.groq_api_key = saved
    except Exception as exc:
        failures.append(f"missing-key check errored: {exc}")

    print()
    if failures:
        print(f"{len(failures)} FAILURE(S):")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("All LLM checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
