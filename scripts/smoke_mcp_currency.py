"""Exercise the currency MCP server over a real stdio MCP session.

    python scripts/smoke_mcp_currency.py

Covers the brief's own currency examples plus the failure paths that must not
produce a plausible-looking but invented number.
"""

from __future__ import annotations

import sys as _sys

# The assistant's provenance labels are emoji; a Windows cp1252 console would
# raise UnicodeEncodeError when printing them.
for _stream in (_sys.stdout, _sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

import asyncio
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from scripts.smoke_mcp_weather import unwrap

SERVER_MODULE = "app.mcp_servers.currency_server"


async def main() -> int:
    params = StdioServerParameters(command=sys.executable, args=["-m", SERVER_MODULE])
    failures: list[str] = []

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            names = sorted(tool.name for tool in tools.tools)
            print(f"discovered {len(names)} tools: {names}")
            for expected in ("convert_currency", "get_exchange_rate"):
                if expected not in names:
                    failures.append(f"missing tool {expected}")

            print()
            print("--- the brief's currency examples ---")
            cases = [
                ("Convert INR 50,000 to SGD", 50_000, "INR", "SGD"),
                ("How much is 200 SGD in INR", 200, "SGD", "INR"),
                ("Convert a USD budget to SGD", 3_000, "USD", "SGD"),
            ]
            for label, amount, source, target in cases:
                data = unwrap(
                    await session.call_tool(
                        "convert_currency",
                        {
                            "amount": amount,
                            "from_currency": source,
                            "to_currency": target,
                        },
                    )
                )
                if not data.get("ok"):
                    failures.append(f"{label} failed: {data.get('error')}")
                    print(f"  FAIL {label}: {data.get('error')}")
                    continue
                inner = data["data"]
                print(f"  OK  {label}")
                print(f"        {inner['amount']:,} {inner['from_currency']} = "
                      f"{inner['converted_amount']:,} {inner['to_currency']}")
                print(f"        rate {inner['rate']} | published {inner['rate_date']} "
                      f"| source {data['source'][:40]}")
                if inner.get("rate_date") is None:
                    failures.append(f"{label} did not report a rate_date")

            print()
            print("--- get_exchange_rate ---")
            data = unwrap(
                await session.call_tool(
                    "get_exchange_rate", {"from_currency": "SGD", "to_currency": "INR"}
                )
            )
            if data.get("ok"):
                inner = data["data"]
                print(f"  OK  1 {inner['from_currency']} = {inner['rate']} "
                      f"{inner['to_currency']}  (published {inner['rate_date']})")
            else:
                failures.append(f"rate lookup failed: {data.get('error')}")

            print()
            print("--- failure paths that must not invent a number ---")
            bad_cases = [
                ("unknown code", "convert_currency",
                 {"amount": 100, "from_currency": "XYZ", "to_currency": "SGD"}),
                ("not a code", "convert_currency",
                 {"amount": 100, "from_currency": "rupees", "to_currency": "SGD"}),
                ("negative amount", "convert_currency",
                 {"amount": -5, "from_currency": "INR", "to_currency": "SGD"}),
                ("unknown target", "get_exchange_rate",
                 {"from_currency": "SGD", "to_currency": "QQQ"}),
            ]
            for label, tool_name, args in bad_cases:
                data = unwrap(await session.call_tool(tool_name, args))
                refused = not data.get("ok")
                print(f"  {'OK  ' if refused else 'FAIL'} {label}: "
                      f"{str(data.get('error'))[:110]}")
                if not refused:
                    failures.append(f"{label} was not refused")

            print()
            print("--- same-currency conversion is a no-op, not an error ---")
            data = unwrap(
                await session.call_tool(
                    "convert_currency",
                    {"amount": 100, "from_currency": "SGD", "to_currency": "SGD"},
                )
            )
            print(f"  ok={data.get('ok')} rate={data['data']['rate']} "
                  f"note={data['data'].get('note')}")
            if not data.get("ok"):
                failures.append("same-currency conversion errored")

    print()
    if failures:
        print(f"{len(failures)} FAILURE(S):")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("All currency MCP checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
