# tests/test_weather_tool.py
# No real network calls, no API key needed - OpenWeather's HTTP responses
# are mocked with respx.

import respx
import httpx

from app.tools import weather_tool
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
async def test_forecast_buckets_by_sri_lanka_date_not_utc_or_server_time(monkeypatch):
    # 2026-08-31 20:00 UTC is 2026-09-01 01:30 in Sri Lanka. The slice
    # belongs to the traveller's Sep 1 - not UTC's Aug 31 (dt_txt), and not
    # whatever the server's own timezone says (the original Galle bug,
    # docs/NEXT_STEPS.md P0#1).
    monkeypatch.setattr(settings, "openweather_api_key", "fake-key")
    respx.get(CURRENT_URL).mock(return_value=httpx.Response(200, json=CURRENT_RESPONSE))
    respx.get(FORECAST_URL).mock(return_value=httpx.Response(200, json={
        "list": [
            {"dt": 1788206400, "dt_txt": "2026-08-31 20:00:00",
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


@respx.mock
async def test_cache_holds_the_full_forecast_so_each_caller_gets_its_own_dates(monkeypatch):
    # Regression: the cache used to store the FIRST caller's filtered days,
    # so a later "tomorrow" question for the same place got those days back.
    monkeypatch.setattr(settings, "openweather_api_key", "fake-key")
    store = {}

    async def fake_get(key):
        return store.get(key)

    async def fake_set(key, value, ttl):
        store[key] = value
    monkeypatch.setattr(weather_tool, "cache_get", fake_get)
    monkeypatch.setattr(weather_tool, "cache_set", fake_set)

    def slice_at(day, pop):
        from datetime import datetime, timezone
        dt = int(datetime(2026, 9, day, 6, 0, tzinfo=timezone.utc).timestamp())
        return {"dt": dt, "dt_txt": "", "main": {"temp": 28.0}, "weather": [{"main": "Rain"}], "pop": pop}

    respx.get(CURRENT_URL).mock(return_value=httpx.Response(200, json=CURRENT_RESPONSE))
    respx.get(FORECAST_URL).mock(return_value=httpx.Response(200, json={"list": [slice_at(1, 0.1), slice_at(2, 0.9)]}))

    first = await get_weather(1.0, 1.0, ["2026-09-01"])
    second = await get_weather(1.0, 1.0, ["2026-09-02"])   # served from cache

    assert [d["date"] for d in first["forecast"]] == ["2026-09-01"]
    assert [d["date"] for d in second["forecast"]] == ["2026-09-02"]
