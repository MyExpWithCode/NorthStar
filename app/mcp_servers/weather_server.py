"""MCP server 1 of 2: current weather and forecast, via Open-Meteo.

    python -m app.mcp_servers.weather_server        # stdio, for an MCP client

A standalone MCP server. It knows nothing about the RAG index, the agent or the
LLM -- any MCP client can connect to it, which is what makes this a real MCP
integration rather than a local function wearing an MCP label.

Open-Meteo is used because it is free and needs no API key, so the application
has no signup step and the demo cannot fail on a missing credential.

Every tool returns the same envelope::

    {"ok": true,  "source": ..., "retrieved_at": ..., "data": {...}}
    {"ok": false, "source": ..., "retrieved_at": ..., "error": "..."}

Failures are reported, never fabricated: if Open-Meteo is unreachable the tool
says so, and the assistant is instructed to pass that on rather than guess a
forecast.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

# httpx logs every request at INFO. On a stdio server that noise lands in the
# client's captured stderr and buries anything worth reading.
logging.getLogger("httpx").setLevel(logging.WARNING)

SERVER_NAME = "weather"
SOURCE = "Open-Meteo"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
REQUEST_TIMEOUT = 20.0

#: Open-Meteo serves at most 16 days of daily forecast.
MAX_FORECAST_DAYS = 16

#: The destination this deployment is about. Geocoding is still attempted for
#: other cities, but Singapore is hardcoded as a fallback so the flagship
#: scenario cannot fail just because the geocoding endpoint is down.
DEFAULT_CITY = "Singapore"
FALLBACK_COORDINATES: dict[str, dict[str, Any]] = {
    "singapore": {
        "name": "Singapore",
        "country": "Singapore",
        "latitude": 1.2897,
        "longitude": 103.8501,
        "timezone": "Asia/Singapore",
    }
}

#: WMO weather interpretation codes -> plain English.
WMO_CODES: dict[int, str] = {
    0: "clear sky",
    1: "mainly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "fog",
    48: "depositing rime fog",
    51: "light drizzle",
    53: "moderate drizzle",
    55: "dense drizzle",
    56: "light freezing drizzle",
    57: "dense freezing drizzle",
    61: "slight rain",
    63: "moderate rain",
    65: "heavy rain",
    66: "light freezing rain",
    67: "heavy freezing rain",
    71: "slight snowfall",
    73: "moderate snowfall",
    75: "heavy snowfall",
    77: "snow grains",
    80: "slight rain showers",
    81: "moderate rain showers",
    82: "violent rain showers",
    85: "slight snow showers",
    86: "heavy snow showers",
    95: "thunderstorm",
    96: "thunderstorm with slight hail",
    99: "thunderstorm with heavy hail",
}

# NOTE ON THE SDK VERSION
# mcp 2.x renamed FastMCP to MCPServer, but langchain-mcp-adapters (the client
# side of this integration) requires mcp<2. Server and client have to agree, so
# this targets the 1.x API. The decorator and constructor signatures are
# identical across both, so only the import differs.
server = FastMCP(
    name=SERVER_NAME,
    instructions=(
        "Current weather conditions and daily forecasts for travel planning. "
        "Use this for any time-sensitive weather question; it is the only "
        "source of weather information."
    ),
)


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _ok(data: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "source": SOURCE, "retrieved_at": _now(), "data": data}


def _error(message: str) -> dict[str, Any]:
    return {"ok": False, "source": SOURCE, "retrieved_at": _now(), "error": message}


def _describe(code: Any) -> str:
    try:
        return WMO_CODES.get(int(code), f"unknown conditions (WMO code {code})")
    except (TypeError, ValueError):
        return "unknown conditions"


def _get_json(url: str, params: dict[str, Any]) -> dict[str, Any]:
    with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
        response = client.get(url, params=params)
        response.raise_for_status()
        return response.json()


def _resolve_location(city: str) -> dict[str, Any]:
    """Geocode a city name, falling back to known coordinates."""
    fallback = FALLBACK_COORDINATES.get(city.strip().lower())
    try:
        payload = _get_json(
            GEOCODING_URL, {"name": city, "count": 1, "language": "en", "format": "json"}
        )
        results = payload.get("results") or []
        if results:
            top = results[0]
            return {
                "name": top.get("name", city),
                "country": top.get("country", ""),
                "latitude": top["latitude"],
                "longitude": top["longitude"],
                "timezone": top.get("timezone", "auto"),
            }
    except (httpx.HTTPError, KeyError, ValueError):
        if fallback is None:
            raise

    if fallback is not None:
        return dict(fallback)
    raise LookupError(f"Could not find coordinates for {city!r}")


def _outdoor_suitability(
    precipitation_probability: Any, precipitation_mm: Any, weather_code: Any
) -> str:
    """Derive a coarse outdoor/indoor verdict from the daily numbers.

    This is what the assistant keys the indoor/outdoor itinerary swap off, so
    it is computed here -- from the numbers the service actually returned --
    rather than left to the model to infer.
    """
    probability = precipitation_probability if isinstance(
        precipitation_probability, (int, float)
    ) else 0
    millimetres = precipitation_mm if isinstance(precipitation_mm, (int, float)) else 0
    try:
        code = int(weather_code)
    except (TypeError, ValueError):
        code = 0

    if code >= 95 or probability >= 70 or millimetres >= 10:
        return "poor"
    if probability >= 40 or millimetres >= 2:
        return "mixed"
    return "good"


@server.tool(
    name="get_current_weather",
    description=(
        "Current weather conditions for a city. Use for 'what is the weather "
        "right now' style questions. Returns temperature in Celsius, relative "
        "humidity, precipitation, wind speed and a plain-English description."
    ),
)
def get_current_weather(city: str = DEFAULT_CITY) -> dict[str, Any]:
    """Retrieve current conditions from Open-Meteo."""
    try:
        location = _resolve_location(city)
    except (httpx.HTTPError, LookupError) as exc:
        return _error(f"Could not resolve the location {city!r}: {exc}")

    try:
        payload = _get_json(
            FORECAST_URL,
            {
                "latitude": location["latitude"],
                "longitude": location["longitude"],
                "current": (
                    "temperature_2m,relative_humidity_2m,apparent_temperature,"
                    "precipitation,weather_code,wind_speed_10m"
                ),
                "timezone": location["timezone"],
            },
        )
    except httpx.HTTPError as exc:
        return _error(f"Could not reach the weather service: {exc}")

    current = payload.get("current") or {}
    if not current:
        return _error("The weather service returned no current conditions.")

    return _ok(
        {
            "location": f"{location['name']}, {location['country']}".strip(", "),
            "observed_at": current.get("time"),
            "timezone": payload.get("timezone", location["timezone"]),
            "temperature_c": current.get("temperature_2m"),
            "feels_like_c": current.get("apparent_temperature"),
            "relative_humidity_pct": current.get("relative_humidity_2m"),
            "precipitation_mm": current.get("precipitation"),
            "wind_speed_kmh": current.get("wind_speed_10m"),
            "conditions": _describe(current.get("weather_code")),
        }
    )


@server.tool(
    name="get_weather_forecast",
    description=(
        "Daily weather forecast for a city, up to 16 days ahead. Use for trip "
        "planning and any 'next few days' or 'next week' question. Each day "
        "includes min/max temperature, rain probability and total, a "
        "plain-English description, and an outdoor_suitability rating of "
        "'good', 'mixed' or 'poor' for deciding between outdoor and indoor "
        "activities."
    ),
)
def get_weather_forecast(
    city: str = DEFAULT_CITY,
    days: int = 3,
    start_date: str | None = None,
) -> dict[str, Any]:
    """Retrieve a daily forecast. `start_date` is ISO `YYYY-MM-DD`."""
    if days < 1:
        return _error("`days` must be at least 1.")

    notes: list[str] = []
    requested_days = days
    if days > MAX_FORECAST_DAYS:
        days = MAX_FORECAST_DAYS
        notes.append(
            f"Requested {requested_days} days but the forecast service provides "
            f"at most {MAX_FORECAST_DAYS}; returning {MAX_FORECAST_DAYS}."
        )

    first_day: date | None = None
    if start_date:
        try:
            first_day = date.fromisoformat(start_date)
        except ValueError:
            return _error(
                f"`start_date` must be ISO format YYYY-MM-DD, got {start_date!r}."
            )
        today = datetime.now(timezone.utc).date()
        horizon = today + timedelta(days=MAX_FORECAST_DAYS - 1)
        if first_day > horizon:
            return _error(
                f"{start_date} is beyond the {MAX_FORECAST_DAYS}-day forecast "
                f"horizon, which currently ends {horizon.isoformat()}. "
                "No forecast is available for that date."
            )
        if first_day < today:
            return _error(
                f"{start_date} is in the past; this tool only forecasts forward."
            )

    try:
        location = _resolve_location(city)
    except (httpx.HTTPError, LookupError) as exc:
        return _error(f"Could not resolve the location {city!r}: {exc}")

    params: dict[str, Any] = {
        "latitude": location["latitude"],
        "longitude": location["longitude"],
        "daily": (
            "weather_code,temperature_2m_max,temperature_2m_min,"
            "precipitation_sum,precipitation_probability_max,wind_speed_10m_max"
        ),
        "timezone": location["timezone"],
    }
    if first_day is None:
        params["forecast_days"] = days
    else:
        last_day = first_day + timedelta(days=days - 1)
        params["start_date"] = first_day.isoformat()
        params["end_date"] = last_day.isoformat()

    try:
        payload = _get_json(FORECAST_URL, params)
    except httpx.HTTPError as exc:
        return _error(f"Could not reach the weather service: {exc}")

    daily = payload.get("daily") or {}
    dates = daily.get("time") or []
    if not dates:
        return _error("The weather service returned no forecast days.")

    def column(key: str) -> list[Any]:
        values = daily.get(key) or []
        return list(values) + [None] * (len(dates) - len(values))

    codes = column("weather_code")
    highs = column("temperature_2m_max")
    lows = column("temperature_2m_min")
    rain_mm = column("precipitation_sum")
    rain_pct = column("precipitation_probability_max")
    wind = column("wind_speed_10m_max")

    forecast_days = [
        {
            "date": dates[i],
            "temp_max_c": highs[i],
            "temp_min_c": lows[i],
            "precipitation_mm": rain_mm[i],
            "precipitation_probability_pct": rain_pct[i],
            "wind_speed_max_kmh": wind[i],
            "conditions": _describe(codes[i]),
            "outdoor_suitability": _outdoor_suitability(rain_pct[i], rain_mm[i], codes[i]),
        }
        for i in range(len(dates))
    ]

    if len(forecast_days) < requested_days and not notes:
        notes.append(
            f"Only {len(forecast_days)} of the {requested_days} requested days "
            "are available from the forecast service."
        )

    data: dict[str, Any] = {
        "location": f"{location['name']}, {location['country']}".strip(", "),
        "timezone": payload.get("timezone", location["timezone"]),
        "days_returned": len(forecast_days),
        "forecast": forecast_days,
    }
    if notes:
        data["notes"] = notes
    return _ok(data)


if __name__ == "__main__":
    server.run(transport="stdio")
