"""
Open-Meteo weather broker / adapter.

Owns all HTTP requests and response parsing for geocoding, current weather,
and forecasts. MCP tool functions must not call HTTP clients directly.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import requests

GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
HTTP_TIMEOUT_SECONDS = 10

# WMO weather interpretation codes (Open-Meteo)
WEATHER_CONDITIONS: dict[int, str] = {
    0: "Clear sky",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Depositing rime fog",
    51: "Light drizzle",
    53: "Moderate drizzle",
    55: "Dense drizzle",
    56: "Light freezing drizzle",
    57: "Dense freezing drizzle",
    61: "Slight rain",
    63: "Moderate rain",
    65: "Heavy rain",
    66: "Light freezing rain",
    67: "Heavy freezing rain",
    71: "Slight snow",
    73: "Moderate snow",
    75: "Heavy snow",
    77: "Snow grains",
    80: "Slight rain showers",
    81: "Moderate rain showers",
    82: "Violent rain showers",
    85: "Slight snow showers",
    86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm with slight hail",
    99: "Thunderstorm with heavy hail",
}


class WeatherBrokerError(Exception):
    """Base error for weather broker failures."""


class LocationNotFoundError(WeatherBrokerError):
    """Raised when a location cannot be resolved via geocoding."""


class ForecastDateError(WeatherBrokerError):
    """Raised when a requested forecast date is outside the available window."""


class WeatherAPIError(WeatherBrokerError):
    """Raised when the Open-Meteo API returns an unexpected response."""


_session: requests.Session | None = None


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update({"User-Agent": "weather-mcp-agent/1.0"})
    return _session


def _weather_condition(code: int | None) -> str:
    if code is None:
        return "Unknown"
    return WEATHER_CONDITIONS.get(code, f"Weather code {code}")


def _request_json(url: str, params: dict[str, Any]) -> dict[str, Any]:
    try:
        response = _get_session().get(url, params=params, timeout=HTTP_TIMEOUT_SECONDS)
        response.raise_for_status()
        payload = response.json()
    except requests.Timeout as exc:
        raise WeatherAPIError("Weather service timed out. Please try again.") from exc
    except requests.RequestException as exc:
        raise WeatherAPIError("Unable to reach the weather service.") from exc
    except ValueError as exc:
        raise WeatherAPIError("Received an invalid response from the weather service.") from exc

    if not isinstance(payload, dict):
        raise WeatherAPIError("Received an invalid response from the weather service.")
    return payload


def resolve_location(location: str) -> dict[str, Any]:
    """
    Resolve a city name to coordinates and metadata via Open-Meteo geocoding.

    Returns:
        dict with name, latitude, longitude, country, timezone
    """
    query = (location or "").strip()
    if not query:
        raise LocationNotFoundError("Location must be a non-empty city name.")

    payload = _request_json(
        GEOCODING_URL,
        {"name": query, "count": 1, "language": "en", "format": "json"},
    )
    results = payload.get("results") or []
    if not results:
        raise LocationNotFoundError(f"Could not resolve location: {query!r}")

    match = results[0]
    return {
        "name": match.get("name", query),
        "latitude": match["latitude"],
        "longitude": match["longitude"],
        "country": match.get("country", "Unknown"),
        "timezone": match.get("timezone", "auto"),
    }


def _fetch_forecast_payload(
    location_info: dict[str, Any],
    forecast_days: int,
    *,
    include_current: bool = False,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "latitude": location_info["latitude"],
        "longitude": location_info["longitude"],
        "timezone": location_info.get("timezone", "auto"),
        "forecast_days": forecast_days,
        "daily": ",".join(
            [
                "weather_code",
                "temperature_2m_max",
                "temperature_2m_min",
                "precipitation_probability_max",
                "precipitation_sum",
                "wind_speed_10m_max",
            ]
        ),
    }
    if include_current:
        params["current"] = ",".join(
            [
                "temperature_2m",
                "apparent_temperature",
                "precipitation",
                "weather_code",
                "wind_speed_10m",
                "relative_humidity_2m",
            ]
        )

    payload = _request_json(FORECAST_URL, params)
    if "daily" not in payload and not include_current:
        raise WeatherAPIError("Forecast data is unavailable for this location.")
    if include_current and "current" not in payload:
        raise WeatherAPIError("Current weather data is unavailable for this location.")
    return payload


def get_current_weather(location: str) -> dict[str, Any]:
    """Return current weather for a resolved location."""
    location_info = resolve_location(location)
    payload = _fetch_forecast_payload(location_info, forecast_days=1, include_current=True)
    current = payload.get("current") or {}
    current_units = payload.get("current_units") or {}

    observed_at = current.get("time")
    if observed_at:
        observed_at = datetime.fromisoformat(observed_at).isoformat()

    weather_code = current.get("weather_code")
    display_name = f"{location_info['name']}, {location_info['country']}"

    return {
        "location": display_name,
        "temperature": current.get("temperature_2m"),
        "temperature_unit": current_units.get("temperature_2m", "°C"),
        "apparent_temperature": current.get("apparent_temperature"),
        "apparent_temperature_unit": current_units.get("apparent_temperature", "°C"),
        "precipitation": current.get("precipitation"),
        "precipitation_unit": current_units.get("precipitation", "mm"),
        "weather_code": weather_code,
        "condition": _weather_condition(weather_code),
        "wind_speed": current.get("wind_speed_10m"),
        "wind_speed_unit": current_units.get("wind_speed_10m", "km/h"),
        "humidity": current.get("relative_humidity_2m"),
        "humidity_unit": current_units.get("relative_humidity_2m", "%"),
        "observed_at": observed_at,
    }


def get_forecast(location: str, days: int = 3) -> dict[str, Any]:
    """Return a daily forecast for the requested number of days (1-7)."""
    if days < 1 or days > 7:
        raise WeatherBrokerError("days must be between 1 and 7.")

    location_info = resolve_location(location)
    payload = _fetch_forecast_payload(location_info, forecast_days=days)
    daily = payload.get("daily") or {}
    daily_units = payload.get("daily_units") or {}

    dates = daily.get("time") or []
    records: list[dict[str, Any]] = []
    for index, forecast_date in enumerate(dates[:days]):
        weather_code = _value_at(daily, "weather_code", index)
        records.append(
            {
                "date": forecast_date,
                "temperature_max": _value_at(daily, "temperature_2m_max", index),
                "temperature_min": _value_at(daily, "temperature_2m_min", index),
                "temperature_unit": daily_units.get("temperature_2m_max", "°C"),
                "precipitation_probability_max": _value_at(
                    daily, "precipitation_probability_max", index
                ),
                "precipitation_sum": _value_at(daily, "precipitation_sum", index),
                "precipitation_unit": daily_units.get("precipitation_sum", "mm"),
                "wind_speed_max": _value_at(daily, "wind_speed_10m_max", index),
                "wind_speed_unit": daily_units.get("wind_speed_10m_max", "km/h"),
                "weather_code": weather_code,
                "condition": _weather_condition(weather_code),
            }
        )

    display_name = f"{location_info['name']}, {location_info['country']}"
    return {
        "location": display_name,
        "timezone": payload.get("timezone", location_info.get("timezone", "auto")),
        "days_requested": days,
        "forecast": records,
    }


def get_forecast_day(location: str, target_date: date | None = None, days: int = 7) -> dict[str, Any]:
    """
    Return a single forecast day record.

    If target_date is None, returns the first available forecast day (today).
    """
    forecast = get_forecast(location, days=days)
    records = forecast.get("forecast") or []
    if not records:
        raise WeatherAPIError("No forecast days are available for this location.")

    if target_date is None:
        return records[0]

    target_str = target_date.isoformat()
    for record in records:
        if record.get("date") == target_str:
            return record

    available_dates = [record.get("date") for record in records if record.get("date")]
    raise ForecastDateError(
        f"Date {target_str} is outside the forecast window. "
        f"Available dates: {', '.join(available_dates)}."
    )


def _value_at(series: dict[str, Any], key: str, index: int) -> Any:
    values = series.get(key) or []
    if index >= len(values):
        return None
    return values[index]
