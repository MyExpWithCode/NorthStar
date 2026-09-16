"""Exercise the weather MCP server over a real stdio MCP session.

    python scripts/smoke_mcp_weather.py

Speaks the MCP protocol to the server as a subprocess -- the same way the
application will -- rather than importing the functions directly, so this also
proves the server is a working MCP server and not just a module.
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
import sys
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SERVER_MODULE = "app.mcp_servers.weather_server"


def unwrap(result: Any) -> dict:
    """Pull the tool's dict payload out of an MCP CallToolResult."""
    if getattr(result, "structuredContent", None):
        payload = result.structuredContent
        # MCPServer wraps a non-model return value under "result".
        return payload.get("result", payload)
    for block in result.content:
        text = getattr(block, "text", None)
        if text:
            return json.loads(text)
    raise AssertionError(f"no payload in {result!r}")


async def main() -> int:
    params = StdioServerParameters(command=sys.executable, args=["-m", SERVER_MODULE])
    failures: list[str] = []

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            names = sorted(tool.name for tool in tools.tools)
            print(f"discovered {len(names)} tools: {names}")
            for expected in ("get_current_weather", "get_weather_forecast"):
                if expected not in names:
                    failures.append(f"missing tool {expected}")

            print()
            print("--- get_current_weather(Singapore) ---")
            data = unwrap(await session.call_tool("get_current_weather", {}))
            print(json.dumps(data, indent=2)[:600])
            if not data.get("ok"):
                failures.append(f"current weather failed: {data.get('error')}")
            else:
                inner = data["data"]
                for field in ("temperature_c", "relative_humidity_pct", "conditions"):
                    if inner.get(field) is None:
                        failures.append(f"current weather missing {field}")

            print()
            print("--- get_weather_forecast(days=3) ---")
            data = unwrap(
                await session.call_tool("get_weather_forecast", {"days": 3})
            )
            if not data.get("ok"):
                failures.append(f"forecast failed: {data.get('error')}")
            else:
                print(f"location: {data['data']['location']}  "
                      f"source: {data['source']}  retrieved: {data['retrieved_at']}")
                for day in data["data"]["forecast"]:
                    print(f"  {day['date']}  {day['temp_min_c']}-{day['temp_max_c']}C  "
                          f"rain {day['precipitation_probability_pct']}% / "
                          f"{day['precipitation_mm']}mm  "
                          f"{day['conditions']:22} outdoor={day['outdoor_suitability']}")
                if data["data"]["days_returned"] != 3:
                    failures.append("expected 3 forecast days")
                verdicts = {d["outdoor_suitability"] for d in data["data"]["forecast"]}
                if not verdicts <= {"good", "mixed", "poor"}:
                    failures.append(f"unexpected suitability values: {verdicts}")

            print()
            print("--- over-horizon request must say so, not extrapolate ---")
            data = unwrap(
                await session.call_tool(
                    "get_weather_forecast", {"days": 40}
                )
            )
            notes = data.get("data", {}).get("notes", [])
            print(f"ok={data.get('ok')} days_returned="
                  f"{data.get('data', {}).get('days_returned')}")
            print(f"notes: {notes}")
            if not notes:
                failures.append("no note for an over-horizon day count")

            print()
            print("--- a date beyond the horizon must be refused ---")
            data = unwrap(
                await session.call_tool(
                    "get_weather_forecast", {"days": 3, "start_date": "2027-12-01"}
                )
            )
            print(f"ok={data.get('ok')}  error={data.get('error')}")
            if data.get("ok"):
                failures.append("a far-future date was not refused")

            print()
            print("--- a malformed date must be refused ---")
            data = unwrap(
                await session.call_tool(
                    "get_weather_forecast", {"start_date": "next Tuesday"}
                )
            )
            print(f"ok={data.get('ok')}  error={data.get('error')}")
            if data.get("ok"):
                failures.append("a malformed date was not refused")

            print()
            print("--- an unknown city must fail cleanly, not invent weather ---")
            data = unwrap(
                await session.call_tool(
                    "get_current_weather", {"city": "Zzzqqx Nowhereville"}
                )
            )
            print(f"ok={data.get('ok')}  error={str(data.get('error'))[:120]}")
            if data.get("ok"):
                failures.append("an unknown city returned success")

    print()
    if failures:
        print(f"{len(failures)} FAILURE(S):")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("All weather MCP checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
