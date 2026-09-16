"""Verify the MCP client discovers both servers and degrades one at a time.

    python scripts/smoke_mcp_client.py

The degradation check matters more than the happy path: the brief requires
unavailable tools to be handled without fabricating an answer, and that starts
with the application continuing to run when a server will not start.
"""

from __future__ import annotations

import sys as _sys

# The assistant's provenance labels are emoji; a Windows cp1252 console would
# raise UnicodeEncodeError when printing them.
for _stream in (_sys.stdout, _sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

import asyncio
import json
import logging
import sys

from app import mcp_client
from app.mcp_client import parse_tool_payload


async def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    failures: list[str] = []

    print("=" * 78)
    print("BOTH SERVERS AVAILABLE")
    print("=" * 78)
    toolset = await mcp_client.connect()
    print(mcp_client.describe(toolset))
    print()
    expected = {
        "get_current_weather",
        "get_weather_forecast",
        "convert_currency",
        "get_exchange_rate",
    }
    found = set(toolset.tool_names)
    print(f"tools discovered: {len(found)} | expected: {len(expected)}")
    if found != expected:
        failures.append(f"tool mismatch: missing {expected - found}, extra {found - expected}")
    if toolset.degraded:
        failures.append(f"unexpected degradation: {toolset.degraded}")

    print()
    print("--- schemas made available to the LLM ---")
    for tool in sorted(toolset.tools, key=lambda t: t.name):
        schema = tool.args_schema or {}
        params = list((schema.get("properties") or {}).keys())
        print(f"  {tool.name:22} server={mcp_client.server_of_or_none(toolset, tool.name)}"
              f"  args={params}")
        print(f"      {tool.description.splitlines()[0][:96]}")

    print()
    print("--- invoking one tool from each server through the LangChain wrapper ---")
    weather = next(t for t in toolset.tools if t.name == "get_weather_forecast")
    result = await weather.ainvoke({"city": "Singapore", "days": 2})
    payload = parse_tool_payload(result)
    inner = payload.get("data", {})
    print(f"  get_weather_forecast -> ok={payload.get('ok')} "
          f"days={inner.get('days_returned')}")
    if not payload.get("ok"):
        failures.append("weather tool call failed through the adapter")

    currency = next(t for t in toolset.tools if t.name == "convert_currency")
    result = await currency.ainvoke(
        {"amount": 50000, "from_currency": "INR", "to_currency": "SGD"}
    )
    payload = parse_tool_payload(result)
    inner = payload.get("data", {})
    print(f"  convert_currency     -> ok={payload.get('ok')} "
          f"{inner.get('converted_amount')} SGD (rate {inner.get('rate')}, "
          f"published {inner.get('rate_date')})")
    if not payload.get("ok"):
        failures.append("currency tool call failed through the adapter")

    print()
    print("=" * 78)
    print("ONE SERVER BROKEN -- the other must survive")
    print("=" * 78)
    original = dict(mcp_client.SERVER_MODULES)
    for broken in ("weather", "currency"):
        mcp_client.SERVER_MODULES.clear()
        mcp_client.SERVER_MODULES.update(original)
        mcp_client.SERVER_MODULES[broken] = "app.mcp_servers.does_not_exist"
        try:
            degraded_set = await mcp_client.connect()
        finally:
            mcp_client.SERVER_MODULES.clear()
            mcp_client.SERVER_MODULES.update(original)

        survivor = "currency" if broken == "weather" else "weather"
        ok = (
            broken in degraded_set.degraded
            and survivor in degraded_set.tools_by_server
            and len(degraded_set.tools) == 2
        )
        print()
        print(f"{'OK  ' if ok else 'FAIL'} broke {broken!r}: "
              f"{len(degraded_set.tools)} tools still available "
              f"({', '.join(sorted(degraded_set.tool_names))})")
        print(f"      degraded: {json.dumps(degraded_set.degraded_summary())[:150]}")
        print(f"      prompt note: {degraded_set.prompt_note()[:150]}")
        if not ok:
            failures.append(f"degradation for {broken} did not behave")
        if not degraded_set.prompt_note():
            failures.append(f"no prompt note generated for degraded {broken}")

    print()
    print("=" * 78)
    print("BOTH SERVERS BROKEN -- app must still start, RAG-only")
    print("=" * 78)
    mcp_client.SERVER_MODULES.clear()
    mcp_client.SERVER_MODULES.update(
        {"weather": "app.mcp_servers.nope", "currency": "app.mcp_servers.nope"}
    )
    try:
        dead = await mcp_client.connect()
    finally:
        mcp_client.SERVER_MODULES.clear()
        mcp_client.SERVER_MODULES.update(original)
    print(f"tools={len(dead.tools)} degraded={sorted(dead.degraded)}")
    print(f"prompt note: {dead.prompt_note()[:200]}")
    if dead.tools or len(dead.degraded) != 2:
        failures.append("both-servers-down case did not degrade cleanly")

    print()
    if failures:
        print(f"{len(failures)} FAILURE(S):")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("All MCP client checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
