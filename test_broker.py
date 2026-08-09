"""Quick smoke tests for weather_broker.py (no test framework)."""

from datetime import date, timedelta

import weather_broker


def _print_section(title: str) -> None:
    print(f"\n=== {title} ===")


def main() -> None:
    _print_section("Valid city — resolve_location")
    chicago = weather_broker.resolve_location("Chicago")
    print(chicago)
    assert "latitude" in chicago and "longitude" in chicago

    _print_section("Invalid city — resolve_location")
    try:
        weather_broker.resolve_location("NotARealCityXYZ123")
        raise AssertionError("Expected LocationNotFoundError")
    except weather_broker.LocationNotFoundError as exc:
        print(f"OK: {exc}")

    _print_section("Current weather — Chicago")
    current = weather_broker.get_current_weather("Chicago")
    print(current)
    assert current.get("location") and current.get("temperature") is not None

    _print_section("3-day forecast — Austin")
    forecast = weather_broker.get_forecast("Austin", days=3)
    print(forecast)
    assert len(forecast.get("forecast", [])) == 3

    _print_section("Travel recommendation — Chicago tomorrow")
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    day = weather_broker.get_forecast_day("Chicago", target_date=date.fromisoformat(tomorrow))
    print({"date": tomorrow, "day": day})

    _print_section("Travel recommendation — derived logic")
    from weather_mcp_server import get_travel_recommendation

    recommendation = get_travel_recommendation("Chicago")
    print(recommendation)
    assert recommendation.get("recommendation") and recommendation.get("source") == "Open-Meteo"

    _print_section("All broker smoke tests passed")


if __name__ == "__main__":
    main()
