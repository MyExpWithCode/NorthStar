"""The failure matrix: every way this system can fail, and what it says.

    python scripts/smoke_failures.py

The brief requires clear handling of missing knowledge and tool failures. The
standard this checks is stricter than "does not crash": every failure must
produce a statement a user can act on, and **must not produce a fabricated
fact**. A made-up forecast is worse than an error message.

Deliberately cheap on LLM calls (one, at the end) so it can be run often.
"""

from __future__ import annotations

import sys as _sys

for _stream in (_sys.stdout, _sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

import asyncio
import shutil
import sys

import httpx

from app.config import settings

results: list[tuple[str, bool, str]] = []


def record(label: str, passed: bool, detail: str) -> None:
    results.append((label, passed, detail))
    print(f"  {'OK  ' if passed else 'FAIL'} {label}")
    print(f"       {detail[:190]}")


# ---------------------------------------------------------------------------
# 1. Knowledge base
# ---------------------------------------------------------------------------
def check_knowledge_base() -> None:
    print()
    print("=" * 78)
    print("KNOWLEDGE BASE")
    print("=" * 78)
    from app.rag import retriever
    from app.rag.kb_tool import search_travel_knowledge_base as kb

    def call(**args):
        message = kb.invoke(
            {"name": kb.name, "args": args, "id": "f", "type": "tool_call"}
        )
        return message.content, (message.artifact or {})

    content, artifact = call(query="Explain the Riemann hypothesis")
    record(
        "wholly unrelated question -> NO_RELEVANT_CONTENT, no sources",
        "NO_RELEVANT_CONTENT" in content and not artifact.get("sources"),
        content.splitlines()[0],
    )

    content, artifact = call(query="hawker food", categories=["nonsense-tag"])
    record(
        "invented category filter -> dropped with a note, search still runs",
        artifact.get("ignored_categories") == ["nonsense-tag"]
        and bool(artifact.get("sources")),
        f"ignored={artifact.get('ignored_categories')} "
        f"sources={len(artifact.get('sources', []))}",
    )

    # Index missing entirely: the fresh-clone / never-built case.
    moved = settings.index_dir.with_name("index.failtest")
    had_index = settings.index_dir.exists()
    if had_index:
        shutil.move(settings.index_dir, moved)
    try:
        retriever.reload()
        content, artifact = call(query="must-visit attractions")
        record(
            "index not built -> KNOWLEDGE_BASE_UNAVAILABLE, told not to guess",
            "KNOWLEDGE_BASE_UNAVAILABLE" in content
            and "do not answer" in content.lower(),
            content.splitlines()[0]
            + f" | artifact error={artifact.get('error')}",
        )
    finally:
        if had_index:
            shutil.move(moved, settings.index_dir)
        retriever.reload()

    # Index present but unreadable.
    original = retriever.get_store
    retriever.get_store = lambda: (_ for _ in ()).throw(
        RuntimeError("index file is corrupt")
    )
    try:
        content, _ = call(query="must-visit attractions")
        record(
            "search raises -> KNOWLEDGE_BASE_ERROR, told not to invent",
            "KNOWLEDGE_BASE_ERROR" in content and "not invent" in content.lower(),
            content.splitlines()[0],
        )
    finally:
        retriever.get_store = original
        retriever.reload()


# ---------------------------------------------------------------------------
# 2. MCP tools -- upstream services unreachable or misused
# ---------------------------------------------------------------------------
def check_mcp_tools() -> None:
    print()
    print("=" * 78)
    print("MCP TOOLS")
    print("=" * 78)
    from app.mcp_servers import currency_server as cs
    from app.mcp_servers import weather_server as ws

    # Point each server at an unroutable host to simulate an outage.
    dead = "https://127.0.0.1:9/dead"

    ws_forecast, ws_geo = ws.FORECAST_URL, ws.GEOCODING_URL
    ws.FORECAST_URL = ws.GEOCODING_URL = dead
    try:
        payload = ws.get_weather_forecast(days=3)
        ok = payload["ok"] is False and "error" in payload and "data" not in payload
        record(
            "weather service unreachable -> ok:false with a reason, no forecast",
            ok,
            str(payload.get("error"))[:170],
        )
        payload = ws.get_current_weather()
        record(
            "current weather unreachable -> ok:false, no temperature invented",
            payload["ok"] is False and "temperature_c" not in payload,
            str(payload.get("error"))[:170],
        )
    finally:
        ws.FORECAST_URL, ws.GEOCODING_URL = ws_forecast, ws_geo

    # Warm the supported-currency cache first. The code-validation test below
    # is about validation logic, not network availability, and without this it
    # would depend on a live fetch immediately after a simulated outage.
    try:
        cs._supported_currencies()
    except Exception as exc:
        print(f"       (currency metadata unavailable: {exc}; "
              "validation now falls back to format checking)")
    cached = cs._currencies

    cs_base = cs.BASE_URL
    cs.BASE_URL = dead
    try:
        payload = cs.convert_currency(50000, "INR", "SGD")
        record(
            "exchange-rate service unreachable -> ok:false, no rate invented",
            payload["ok"] is False and "converted_amount" not in payload,
            str(payload.get("error"))[:170],
        )
    finally:
        cs.BASE_URL = cs_base
        cs._currencies = cached

    payload = cs.convert_currency(100, "XYZ", "SGD")
    record(
        "unknown currency code -> refused, lists what is supported",
        payload["ok"] is False and "not a currency" in str(payload.get("error")),
        str(payload.get("error"))[:170],
    )

    payload = ws.get_weather_forecast(days=3, start_date="2030-01-01")
    record(
        "date beyond the forecast horizon -> refused, horizon stated",
        payload["ok"] is False and "horizon" in str(payload.get("error")),
        str(payload.get("error"))[:170],
    )

    payload = ws.get_weather_forecast(days=99)
    notes = payload.get("data", {}).get("notes", [])
    record(
        "over-long forecast -> clamped, with the clamp stated",
        payload["ok"] is True and bool(notes),
        str(notes)[:170],
    )


# ---------------------------------------------------------------------------
# 3. MCP servers that will not start
# ---------------------------------------------------------------------------
async def check_mcp_degradation() -> None:
    print()
    print("=" * 78)
    print("MCP SERVER STARTUP")
    print("=" * 78)
    from app import mcp_client

    original = dict(mcp_client.SERVER_MODULES)
    try:
        mcp_client.SERVER_MODULES["weather"] = "app.mcp_servers.missing_module"
        toolset = await mcp_client.connect()
        record(
            "one server dead -> only its tools drop, the other survives",
            "weather" in toolset.degraded
            and "currency" in toolset.tools_by_server
            and len(toolset.tools) == 2,
            f"available={sorted(toolset.tool_names)} "
            f"degraded={list(toolset.degraded)}",
        )
        record(
            "degraded capability is named in the prompt, so it cannot be faked",
            "current conditions and weather forecasts" in toolset.prompt_note()
            and "Never estimate" in toolset.prompt_note(),
            toolset.prompt_note()[:170],
        )

        mcp_client.SERVER_MODULES["currency"] = "app.mcp_servers.missing_module"
        toolset = await mcp_client.connect()
        record(
            "both servers dead -> app still runs, RAG only",
            not toolset.tools and len(toolset.degraded) == 2,
            f"tools={len(toolset.tools)} degraded={sorted(toolset.degraded)}",
        )
    finally:
        mcp_client.SERVER_MODULES.clear()
        mcp_client.SERVER_MODULES.update(original)


# ---------------------------------------------------------------------------
# 4. Ingestion
# ---------------------------------------------------------------------------
def check_ingestion() -> None:
    print()
    print("=" * 78)
    print("INGESTION")
    print("=" * 78)
    from app.ingest.service import IngestionError, service

    cases = [
        ("scanned / image-only document refused",
         lambda: service.preview_upload(b"# Hi", "scan.pdf")),
        ("oversized upload refused",
         lambda: service.preview_upload(
             b"x" * (settings.max_upload_bytes + 1), "big.md")),
        ("unsupported file type refused",
         lambda: service.preview_upload(b"x" * 500, "thing.exe")),
        ("non-http URL refused",
         lambda: service.preview_url("file:///etc/passwd")),
        ("unreachable URL reported, not silently skipped",
         lambda: service.preview_url("https://127.0.0.1:9/nope")),
        ("stale preview token refused",
         lambda: service.confirm("nosuchtoken")),
        ("removing an unknown source refused",
         lambda: service.remove_source("no-such-source")),
        ("unknown job id refused",
         lambda: service.job("no-such-job")),
    ]
    for label, call in cases:
        try:
            call()
        except IngestionError as exc:
            record(label, True, str(exc)[:170])
        except Exception as exc:
            record(label, False, f"wrong exception {type(exc).__name__}: {exc}")
        else:
            record(label, False, "accepted, but should have been refused")


# ---------------------------------------------------------------------------
# 5. LLM configuration
# ---------------------------------------------------------------------------
def check_llm_config() -> None:
    print()
    print("=" * 78)
    print("LLM CONFIGURATION")
    print("=" * 78)
    import app.config as config_module
    from app import llm

    provider = settings.llm_provider
    field = "groq_api_key" if provider == "groq" else "anthropic_api_key"
    saved = getattr(config_module.settings, field)
    setattr(config_module.settings, field, None)
    try:
        llm.get_chat_model()
        record("missing API key -> fail fast", False, "no exception raised")
    except llm.LlmNotConfigured as exc:
        record(
            "missing API key -> fails fast naming the variable to set",
            settings.llm_api_key_env in str(exc),
            str(exc)[:170],
        )
    except Exception as exc:
        record("missing API key -> fail fast", False,
               f"wrong exception: {type(exc).__name__}")
    finally:
        setattr(config_module.settings, field, saved)

    from app.agent import _failure_message

    message = _failure_message(
        type("RateLimitError", (Exception,), {})("Error code: 429 ...")
    )
    record(
        "provider rate limit -> readable message, states no facts retrieved",
        "rate-limited" in message and "No travel information was retrieved" in message,
        message[:170],
    )
    message = _failure_message(
        type("APIStatusError", (Exception,), {})("Error code: 413 too large")
    )
    record(
        "oversized request -> readable message, suggests a shorter question",
        "too large" in message and "No travel information was retrieved" in message,
        message[:170],
    )


# ---------------------------------------------------------------------------
# 6. End to end: the assistant must admit a gap rather than invent
# ---------------------------------------------------------------------------
async def check_end_to_end() -> None:
    print()
    print("=" * 78)
    print("END TO END (one LLM call)")
    print("=" * 78)
    from app import agent as agent_module

    travel_agent = await agent_module.build_agent()
    question = "How much does a 7-day Antarctic cruise from Singapore cost?"
    answer, provenance = await agent_module.ask(travel_agent, "fail-e2e", question)

    if "rate-limited" in answer or "too large" in answer:
        record("out-of-scope question admitted, not answered", True,
               "SKIPPED -- provider quota; covered by smoke_agent scenario 6")
        return

    lowered = answer.lower()
    admits = any(
        phrase in lowered
        for phrase in (
            "does not", "doesn't", "no information", "not covered",
            "not contain", "no relevant", "cannot", "can't", "unable",
        )
    )
    record(
        "out-of-scope question -> the gap is admitted",
        admits,
        " ".join(answer.split())[:180],
    )
    record(
        "no price was invented for it",
        not any(token in answer for token in ("SGD ", "$", "USD ")),
        f"tools used: {[c['tool'] for c in provenance.tool_calls]}",
    )


async def main() -> int:
    print("FAILURE MATRIX -- every failure must be stated, never fabricated")
    check_knowledge_base()
    check_mcp_tools()
    await check_mcp_degradation()
    check_ingestion()
    check_llm_config()
    await check_end_to_end()

    failed = [label for label, passed, _ in results if not passed]
    print()
    print("=" * 78)
    print(f"{len(results) - len(failed)} of {len(results)} failure paths behave")
    if failed:
        print()
        print("NOT HANDLED:")
        for label in failed:
            print(f"  - {label}")
        return 1
    print("Every failure path reports honestly.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
