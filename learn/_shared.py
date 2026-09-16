"""The only shared code in learn/. Deliberately tiny.

Lessons 07, 08, 10 and 13 all need the API key and all need to talk to
Groq over raw HTTP. Rather than copy 30 lines into each, they import this.

Nothing here imports app/.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

# Windows consoles default to cp1252, and models happily emit narrow
# no-break spaces, em-dashes and emoji. Without this, printing a model's
# own answer crashes with UnicodeEncodeError - which is a silly way to
# lose a lesson.
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

ROOT = Path(__file__).resolve().parents[1]

GROQ_BASE = "https://api.groq.com/openai/v1"

#: Groq's edge rejects the default python-urllib User-Agent with HTTP 403.
#: See learn/07-call-an-llm/README.md - this cost real debugging time.
USER_AGENT = "northstar-learn/0.1"


def parse_dotenv(path: Path | None = None) -> dict[str, str]:
    """The 10-line .env parser from lesson 01."""
    path = path or (ROOT / ".env")
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def groq_key() -> str:
    """The key from .env, falling back to the shell environment.

    Note the ORDER, which is the opposite of pydantic-settings' precedence
    (lesson 01). Here .env wins on purpose: it is the file the setup
    instructions tell you to edit, so for a teaching script it should be
    the one that takes effect.
    """
    import os

    key = parse_dotenv().get("GROQ_API_KEY") or os.environ.get("GROQ_API_KEY")
    if not key:
        raise SystemExit(
            "GROQ_API_KEY is not set.\n"
            "Add it to .env (copy .env.example) and re-run.\n"
            "Lessons 01-06 need no key if you want to continue without one."
        )
    return key


def post_json(url: str, payload: dict, key: str, timeout: float = 90.0) -> dict:
    """One HTTP POST. No SDK, no retries, no streaming. 8 lines."""
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def get_json(url: str, key: str, timeout: float = 30.0) -> dict:
    request = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {key}", "User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


#: Substrings marking catalogue entries that are not general chat models.
#: Mirrors app/llm.py's _NOT_CHAT_MODELS.
NOT_CHAT = ("whisper", "orpheus", "prompt-guard", "safeguard", "tts",
            "embed", "guard")
#: Groq's "compound" entries are agentic systems with their own built-in
#: tools. We supply our own tools, so mixing the two muddies provenance.
EXCLUDED_PREFIXES = ("groq/compound",)

PREFERRED = ("openai/gpt-oss-120b", "qwen/qwen3.8-27b", "openai/gpt-oss-20b")


def resolve_model(key: str) -> str:
    """Pick a tool-calling model from the LIVE catalogue.

    Same approach as app/llm.py, and for the same reason: Groq retires
    models often enough that a hardcoded id is a time bomb. At the time of
    writing its catalogue holds 13 models and none of the `llama-3.3-*`
    ids most tutorials still name.
    """
    available = {
        m["id"] for m in get_json(f"{GROQ_BASE}/models", key).get("data", [])
    }
    for candidate in PREFERRED:
        if candidate in available:
            return candidate
    for candidate in sorted(available):
        low = candidate.lower()
        if any(m in low for m in NOT_CHAT):
            continue
        if low.startswith(EXCLUDED_PREFIXES):
            continue
        return candidate
    raise SystemExit(f"no usable chat model in: {sorted(available)}")


def rule(title: str) -> None:
    print()
    print("=" * 70)
    print(title)
    print("=" * 70)


def show_json(label: str, obj, indent: int = 2) -> None:
    print(f"{label}:")
    text = json.dumps(obj, indent=indent)
    for line in text.splitlines():
        print("    " + line)
