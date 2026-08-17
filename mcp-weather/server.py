"""Custom Weather MCP server.

Exposes ONE tool: ``get_weather(city: str) -> dict`` that calls the free
OpenWeatherMap API and returns a compact JSON summary.

Runs as HTTP-Streamable transport on ``$PORT`` (default 8080).  Portkey MCP
Gateway registers this at ``http://mcp-weather.mcp-servers.svc.cluster.local:8080/mcp``.
"""
from __future__ import annotations

import os
import logging
from typing import Any

import httpx
from fastmcp import FastMCP

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("mcp-weather")

OPENWEATHER_API_KEY = os.environ.get("OPENWEATHER_API_KEY", "")
OWM_URL             = "https://api.openweathermap.org/data/2.5/weather"

mcp = FastMCP(name="weather-mcp")


@mcp.tool()
async def get_weather(city: str) -> dict[str, Any]:
    """Get current weather for a city name.

    Args:
        city: City name, e.g. "Bengaluru", "London", "New York".

    Returns:
        JSON dict with temperature (C), conditions, humidity, wind.
    """
    if not OPENWEATHER_API_KEY:
        return {"error": "OPENWEATHER_API_KEY not configured on the MCP server"}
    params = {"q": city, "appid": OPENWEATHER_API_KEY, "units": "metric"}
    async with httpx.AsyncClient(timeout=10.0, verify=False) as client:
        r = await client.get(OWM_URL, params=params)
        if r.status_code != 200:
            return {"error": f"OpenWeather returned {r.status_code}", "body": r.text[:400]}
        data = r.json()
    return {
        "city":        data.get("name"),
        "country":     data.get("sys", {}).get("country"),
        "temperature": data.get("main", {}).get("temp"),
        "feels_like":  data.get("main", {}).get("feels_like"),
        "humidity":    data.get("main", {}).get("humidity"),
        "conditions":  (data.get("weather") or [{}])[0].get("description"),
        "wind_mps":    data.get("wind", {}).get("speed"),
        "units":       "metric (Celsius, m/s)",
    }


@mcp.tool()
async def health() -> dict[str, str]:
    """Simple health probe that Portkey / kubectl can call."""
    return {"status": "ok"}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    log.info("starting weather-mcp on 0.0.0.0:%d (streamable-http /mcp)", port)
    # FastMCP >=2.8 exposes streamable-http transport with configurable host/port/path
    mcp.run(transport="streamable-http", host="0.0.0.0", port=port, path="/mcp")
