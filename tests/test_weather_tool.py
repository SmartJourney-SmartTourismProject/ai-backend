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
        {"dt": 1798693200, "main": {"temp": 27.0}, "weather": [{"main": "Clear"}], "pop": 0.1},
        {"dt": 1798704000, "main": {"temp": 30.0}, "weather": [{"main": "Clouds"}], "pop": 0.3},
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
async def test_get_weather_uses_cache_on_second_call(monkeypatch):
    monkeypatch.setattr(settings, "openweather_api_key", "fake-key")
    current_route = respx.get(CURRENT_URL).mock(return_value=httpx.Response(200, json=CURRENT_RESPONSE))
    respx.get(FORECAST_URL).mock(return_value=httpx.Response(200, json=FORECAST_RESPONSE))

    r1 = await get_weather(5.0, 5.0, ["2026-08-29"])
    r2 = await get_weather(5.0, 5.0, ["2026-08-29"])

    assert r1 == r2
    # Only one real HTTP hit - the second call was served from the cache.
    assert current_route.call_count == 1
