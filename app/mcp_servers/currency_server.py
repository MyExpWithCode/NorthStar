"""MCP server 2 of 2: currency conversion, via Frankfurter (ECB rates).

    python -m app.mcp_servers.currency_server       # stdio, for an MCP client

Standalone, like the weather server: no knowledge of the RAG index, the agent
or the LLM. Frankfurter publishes European Central Bank reference rates and
needs no API key.

Every tool returns the same envelope as the weather server::

    {"ok": true,  "source": ..., "retrieved_at": ..., "data": {...}}
    {"ok": false, "source": ..., "retrieved_at": ..., "error": "..."}

Two deliberate choices about honesty:

* **`rate_date` is always surfaced.** ECB reference rates are published once a
  business day, so a Sunday conversion uses Friday's rate. Reporting the rate's
  own date lets the assistant say how current the number is instead of implying
  it is live to the second.
* **Unknown currency codes are rejected, never guessed.** The supported list
  comes from the service itself, so "XYZ" produces a clear error rather than a
  plausible-looking number.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

# httpx logs every request at INFO; on a stdio server that buries the useful
# content of the client's captured stderr.
logging.getLogger("httpx").setLevel(logging.WARNING)

SERVER_NAME = "currency"
SOURCE = "Frankfurter (European Central Bank reference rates)"
BASE_URL = "https://api.frankfurter.dev/v1"
REQUEST_TIMEOUT = 20.0

# NOTE ON THE SDK VERSION
# mcp 2.x renamed FastMCP to MCPServer, but langchain-mcp-adapters (the client
# side of this integration) requires mcp<2. Server and client have to agree, so
# this targets the 1.x API. The decorator and constructor signatures are
# identical across both, so only the import differs.
server = FastMCP(
    name=SERVER_NAME,
    instructions=(
        "Currency conversion and exchange rates from European Central Bank "
        "reference data. Use this for any question involving converting money "
        "between currencies or asking what a rate is; it is the only source of "
        "exchange-rate information."
    ),
)

#: Cached for the process lifetime. The supported-currency list changes at most
#: a few times a decade, and caching keeps a bad code from costing a round trip.
_currencies: dict[str, str] | None = None


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _ok(data: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "source": SOURCE, "retrieved_at": _now(), "data": data}


def _error(message: str) -> dict[str, Any]:
    return {"ok": False, "source": SOURCE, "retrieved_at": _now(), "error": message}


def _get_json(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
        response = client.get(f"{BASE_URL}{path}", params=params or {})
        response.raise_for_status()
        return response.json()


def _supported_currencies() -> dict[str, str]:
    """`{"SGD": "Singapore Dollar", ...}` straight from the service."""
    global _currencies
    if _currencies is None:
        _currencies = _get_json("/currencies")
    return _currencies


def _validate(code: str, label: str) -> tuple[str | None, str | None]:
    """Normalise and check a currency code. Returns `(code, error)`."""
    normalised = (code or "").strip().upper()
    if not normalised:
        return None, f"{label} currency is required."
    if len(normalised) != 3 or not normalised.isalpha():
        return None, (
            f"{label} currency {code!r} is not a three-letter ISO 4217 code "
            "(for example INR, SGD, USD)."
        )
    try:
        supported = _supported_currencies()
    except httpx.HTTPError:
        # The supported-currency list is only metadata. If that endpoint
        # hiccups we must not block a conversion whose rate endpoint is
        # perfectly healthy -- accept a well-formed code and let /latest be
        # the authority. An unsupported pair still produces a clear error
        # from the conversion call itself.
        return normalised, None
    if normalised not in supported:
        return None, (
            f"{normalised} is not a currency this service publishes rates for. "
            f"It supports {len(supported)} currencies including "
            f"{', '.join(sorted(supported)[:8])}."
        )
    return normalised, None


@server.tool(
    name="convert_currency",
    description=(
        "Convert an amount of money from one currency to another at the "
        "latest published exchange rate. Use for any question like 'convert "
        "INR 50,000 to SGD' or 'how much is my budget in Singapore dollars'. "
        "Returns the converted amount, the rate used, and the date that rate "
        "was published."
    ),
)
def convert_currency(
    amount: float, from_currency: str, to_currency: str
) -> dict[str, Any]:
    """Convert `amount` from one ISO 4217 currency to another."""
    if not isinstance(amount, (int, float)) or isinstance(amount, bool):
        return _error(f"`amount` must be a number, got {amount!r}.")
    if amount < 0:
        return _error("`amount` cannot be negative.")

    source, error = _validate(from_currency, "Source")
    if error:
        return _error(error)
    target, error = _validate(to_currency, "Target")
    if error:
        return _error(error)

    if source == target:
        return _ok(
            {
                "amount": amount,
                "from_currency": source,
                "to_currency": target,
                "rate": 1.0,
                "converted_amount": round(float(amount), 2),
                "rate_date": None,
                "note": "Same currency; no conversion applied.",
            }
        )

    try:
        payload = _get_json(
            "/latest", {"base": source, "symbols": target, "amount": amount}
        )
    except httpx.HTTPError as exc:
        return _error(f"Could not reach the exchange-rate service: {exc}")

    rates = payload.get("rates") or {}
    if target not in rates:
        return _error(
            f"The exchange-rate service did not return a {source}->{target} rate."
        )

    converted = float(rates[target])
    unit_rate = converted / amount if amount else 0.0
    return _ok(
        {
            "amount": amount,
            "from_currency": source,
            "to_currency": target,
            "rate": round(unit_rate, 6),
            "converted_amount": round(converted, 2),
            "rate_date": payload.get("date"),
            "from_currency_name": _supported_currencies().get(source),
            "to_currency_name": _supported_currencies().get(target),
        }
    )


@server.tool(
    name="get_exchange_rate",
    description=(
        "Get the latest exchange rate between two currencies, without "
        "converting a specific amount. Returns the rate and the date it was "
        "published."
    ),
)
def get_exchange_rate(from_currency: str, to_currency: str) -> dict[str, Any]:
    """Look up the current rate for one unit of `from_currency`."""
    source, error = _validate(from_currency, "Source")
    if error:
        return _error(error)
    target, error = _validate(to_currency, "Target")
    if error:
        return _error(error)

    if source == target:
        return _ok(
            {
                "from_currency": source,
                "to_currency": target,
                "rate": 1.0,
                "rate_date": None,
                "note": "Same currency.",
            }
        )

    try:
        payload = _get_json("/latest", {"base": source, "symbols": target})
    except httpx.HTTPError as exc:
        return _error(f"Could not reach the exchange-rate service: {exc}")

    rates = payload.get("rates") or {}
    if target not in rates:
        return _error(
            f"The exchange-rate service did not return a {source}->{target} rate."
        )

    return _ok(
        {
            "from_currency": source,
            "to_currency": target,
            "rate": float(rates[target]),
            "rate_date": payload.get("date"),
            "from_currency_name": _supported_currencies().get(source),
            "to_currency_name": _supported_currencies().get(target),
        }
    )


if __name__ == "__main__":
    server.run(transport="stdio")
