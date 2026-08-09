"""
Weather Prediction MCP server for Databricks Agent Bricks.

Exposes weather tools over MCP (Model Context Protocol) so an Agent Bricks
agent can register this deployed Databricks App as an external MCP server.

Run locally:
    python weather_mcp_server.py
"""

from __future__ import annotations

import logging
import os
from datetime import date

from fastmcp import FastMCP

import weather_broker

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("weather-mcp-server")

mcp = FastMCP("weather-prediction")


def _error_response(message: str) -> dict:
    return {"error": message}


def _parse_target_date(date_str: str) -> date | None:
    cleaned = (date_str or "").strip()
    if not cleaned:
        return None
    try:
        return date.fromisoformat(cleaned)
    except ValueError as exc:
        raise weather_broker.WeatherBrokerError(
            f"Invalid date format: {cleaned!r}. Use YYYY-MM-DD."
        ) from exc


def _build_travel_recommendation(location: str, day: dict, target_date: str) -> dict:
    reasons: list[str] = []
    recommendations: list[str] = []

    precip_prob = day.get("precipitation_probability_max")
    temp_max = day.get("temperature_max")
    wind_max = day.get("wind_speed_max")
    condition = day.get("condition", "Unknown")

    if precip_prob is not None:
        if precip_prob >= 70:
            reasons.append(f"Precipitation probability is {precip_prob}% (high rain risk).")
            recommendations.append("Expect significant rain; plan indoor alternatives if possible.")
        elif precip_prob >= 40:
            reasons.append(f"Precipitation probability is {precip_prob}%.")
            recommendations.append("Carry an umbrella.")

    if temp_max is not None:
        if temp_max <= 10:
            reasons.append(f"High temperature is only {temp_max}°C (~{temp_max * 9 / 5 + 32:.0f}°F).")
            recommendations.append("Wear a jacket or warm clothing.")
        elif temp_max >= 29:
            reasons.append(f"High temperature is {temp_max}°C (~{temp_max * 9 / 5 + 32:.0f}°F).")
            recommendations.append("Wear light clothing and stay hydrated.")

    if wind_max is not None and wind_max >= 40:
        reasons.append(f"Wind gusts up to {wind_max} km/h (~{wind_max * 0.621:.0f} mph) are forecast.")
        recommendations.append("Be prepared for strong winds.")

    if not recommendations:
        recommendations.append("Conditions look generally favorable for travel.")

    summary_parts = [condition]
    if temp_max is not None:
        summary_parts.append(f"high {temp_max}°C")
    if precip_prob is not None:
        summary_parts.append(f"{precip_prob}% chance of precipitation")

    return {
        "location": location,
        "date": target_date,
        "weather_summary": ", ".join(summary_parts),
        "recommendation": " ".join(recommendations),
        "reasons": reasons,
        "source": "Open-Meteo",
    }


@mcp.tool
def get_current_weather(location: str) -> dict:
    """
    Get current weather conditions for a city or place name.

    Args:
        location: City or place name, e.g. "Chicago" or "Austin, TX".

    Returns:
        A dict with location, temperature, apparent_temperature, precipitation,
        weather_code, condition, wind_speed, humidity (if available), and
        observed_at (ISO timestamp).
    """
    try:
        return weather_broker.get_current_weather(location)
    except weather_broker.LocationNotFoundError as exc:
        return _error_response(str(exc))
    except weather_broker.WeatherBrokerError as exc:
        logger.warning("get_current_weather failed: %s", exc)
        return _error_response(str(exc))


@mcp.tool
def get_forecast(location: str, days: int = 3) -> dict:
    """
    Get a daily weather forecast for a city or place name.

    Args:
        location: City or place name, e.g. "Chicago".
        days: Number of forecast days to return (1-7, default 3).

    Returns:
        A dict with location, timezone, days_requested, and a forecast list.
        Each forecast entry includes date, temperature_max, temperature_min,
        precipitation_probability_max, precipitation_sum, wind_speed_max,
        weather_code, and condition.
    """
    try:
        return weather_broker.get_forecast(location, days)
    except weather_broker.LocationNotFoundError as exc:
        return _error_response(str(exc))
    except weather_broker.WeatherBrokerError as exc:
        logger.warning("get_forecast failed: %s", exc)
        return _error_response(str(exc))


@mcp.tool
def get_travel_recommendation(location: str, date: str = "") -> dict:
    """
    Derive practical travel recommendations from forecast data.

    Uses precipitation, temperature, and wind thresholds to suggest clothing,
    umbrella use, and rain/wind warnings. Does not invent weather beyond the
    Open-Meteo forecast window.

    Args:
        location: City or place name, e.g. "Chicago".
        date: Optional target date in YYYY-MM-DD format. If omitted, uses the
            first available forecast day (today).

    Returns:
        A dict with location, date, weather_summary, recommendation, reasons,
        and source ("Open-Meteo").
    """
    try:
        target = _parse_target_date(date)
        forecast = weather_broker.get_forecast(location, days=7)
        records = forecast.get("forecast") or []
        if not records:
            return _error_response("No forecast days are available for this location.")

        if target is None:
            day = records[0]
            target_date = day.get("date") or date.today().isoformat()
        else:
            target_str = target.isoformat()
            day = next((record for record in records if record.get("date") == target_str), None)
            if day is None:
                available = ", ".join(record.get("date", "") for record in records if record.get("date"))
                return _error_response(
                    f"Date {target_str} is outside the forecast window. Available dates: {available}."
                )
            target_date = target_str

        return _build_travel_recommendation(forecast["location"], day, target_date)
    except weather_broker.ForecastDateError as exc:
        return _error_response(str(exc))
    except weather_broker.LocationNotFoundError as exc:
        return _error_response(str(exc))
    except weather_broker.WeatherBrokerError as exc:
        logger.warning("get_travel_recommendation failed: %s", exc)
        return _error_response(str(exc))


@mcp.tool
def compare_weather(location_a: str, location_b: str) -> dict:
    """
    Compare current weather and the next-day forecast for two locations.

    Args:
        location_a: First city or place name.
        location_b: Second city or place name.

    Returns:
        A dict with current conditions for both locations and a brief comparison
        of tomorrow's forecast highs and precipitation risk.
    """
    try:
        current_a = weather_broker.get_current_weather(location_a)
        current_b = weather_broker.get_current_weather(location_b)
        forecast_a = weather_broker.get_forecast(location_a, days=2)
        forecast_b = weather_broker.get_forecast(location_b, days=2)

        tomorrow_a = (forecast_a.get("forecast") or [{}])[1 if len(forecast_a.get("forecast", [])) > 1 else 0]
        tomorrow_b = (forecast_b.get("forecast") or [{}])[1 if len(forecast_b.get("forecast", [])) > 1 else 0]

        temp_a = current_a.get("temperature")
        temp_b = current_b.get("temperature")
        warmer = None
        if temp_a is not None and temp_b is not None:
            if temp_a > temp_b:
                warmer = current_a["location"]
            elif temp_b > temp_a:
                warmer = current_b["location"]
            else:
                warmer = "tie"

        return {
            "location_a": current_a,
            "location_b": current_b,
            "tomorrow_forecast_a": tomorrow_a,
            "tomorrow_forecast_b": tomorrow_b,
            "comparison": {
                "warmer_now": warmer,
                "summary": (
                    f"{current_a['location']} is {temp_a}°C with {current_a.get('condition', 'unknown')} ; "
                    f"{current_b['location']} is {temp_b}°C with {current_b.get('condition', 'unknown')}."
                ),
            },
            "source": "Open-Meteo",
        }
    except weather_broker.LocationNotFoundError as exc:
        return _error_response(str(exc))
    except weather_broker.WeatherBrokerError as exc:
        logger.warning("compare_weather failed: %s", exc)
        return _error_response(str(exc))


if __name__ == "__main__":
    # Databricks Apps route external HTTP traffic to this port via app.yaml;
    # streamable-http is the transport Databricks' MCP client/gateway expects.
    port = int(os.getenv("DATABRICKS_APP_PORT", os.getenv("PORT", 8000)))
    mcp.run(transport="http", host="0.0.0.0", port=port)
