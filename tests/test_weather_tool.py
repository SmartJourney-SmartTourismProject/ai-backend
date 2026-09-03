# tests/test_weather_tool.py
# No real network calls, no API key needed - OpenWeather's HTTP responses
# are mocked with respx.

import respx
import httpx

from app.tools.weather_tool import get_weather, CURRENT_URL, FORECAST_URL
from app.config.settings import settings

CURRENT_RESPONSE = {
    "main": {"temp": 28.0, "humidity": 70},
    "weather": [{"main": "Clear"}],
}

FORECAST_RESPONSE = {
    "list": [
        {"dt": 1798693200, "dt_txt": "2026-08-29 09:00:00",
         "main": {"temp": 27.0}, "weather": [{"main": "Clear"}], "pop": 0.1},
        {"dt": 1798704000, "dt_txt": "2026-08-29 12:00:00",
         "main": {"temp": 30.0}, "weather": [{"main": "Clouds"}], "pop": 0.3},
    ]
}


@respx.mock
async def test_get_weather_success(monkeypatch):
    monkeypatch.setattr(settings, "openweather_api_key", "fake-key")
    respx.get(CURRENT_URL).mock(return_value=httpx.Response(200, json=CURRENT_RESPONSE))
    respx.get(FORECAST_URL).mock(return_value=httpx.Response(200, json=FORECAST_RESPONSE))

    result = await get_weather(7.29, 80.63, ["2026-08-29"])

    assert result is not None
    assert result["current"] == {"temp": 28.0, "condition": "Clear", "humidity": 70}


async def test_get_weather_no_api_key(monkeypatch):
    monkeypatch.setattr(settings, "openweather_api_key", "")
    result = await get_weather(1.23, 4.56, ["2026-08-29"])
    assert result is None


@respx.mock
async def test_get_weather_api_failure_returns_none(monkeypatch):
    monkeypatch.setattr(settings, "openweather_api_key", "fake-key")
    respx.get(CURRENT_URL).mock(return_value=httpx.Response(500))
    respx.get(FORECAST_URL).mock(return_value=httpx.Response(200, json=FORECAST_RESPONSE))

    result = await get_weather(9.87, 6.54, ["2026-08-29"])
    assert result is None


@respx.mock
async def test_forecast_buckets_by_utc_date_not_server_local_time(monkeypatch):
    # A slice timestamped just after UTC midnight must land in THAT day's
    # bucket even if the server's local timezone would put it the day
    # before or after - this is the exact bug that made Galle's forecast
    # come back empty during manual testing (see docs/NEXT_STEPS.md P0#1).
    monkeypatch.setattr(settings, "openweather_api_key", "fake-key")
    respx.get(CURRENT_URL).mock(return_value=httpx.Response(200, json=CURRENT_RESPONSE))
    respx.get(FORECAST_URL).mock(return_value=httpx.Response(200, json={
        "list": [
            {"dt": 0, "dt_txt": "2026-09-01 00:30:00",
             "main": {"temp": 25.0}, "weather": [{"main": "Rain"}], "pop": 0.5},
        ]
    }))

    result = await get_weather(1.0, 1.0, ["2026-09-01"])

    assert result["forecast"] == [{
        "date": "2026-09-01", "temp_min": 25.0, "temp_max": 25.0,
        "condition": "Rain", "rain_probability": 0.5,
    }]


@respx.mock
async def test_get_weather_uses_cache_on_second_call(monkeypatch):
    monkeypatch.setattr(settings, "openweather_api_key", "fake-key")
    current_route = respx.get(CURRENT_URL).mock(return_value=httpx.Response(200, json=CURRENT_RESPONSE))
    respx.get(FORECAST_URL).mock(return_value=httpx.Response(200, json=FORECAST_RESPONSE))

    r1 = await get_weather(5.0, 5.0, ["2026-08-29"])
    r2 = await get_weather(5.0, 5.0, ["2026-08-29"])

    assert r1 == r2
    # Only one real HTTP hit - the second call was served from the cache.
    assert current_route.call_count == 1
