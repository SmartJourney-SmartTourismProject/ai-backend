# app/tools/weather_tool.py
from collections import defaultdict
from datetime import datetime

import httpx

from app.config.settings import settings
from app.utils.cache import cache_get, cache_set
from app.utils.clock import SRI_LANKA_TZ


CURRENT_URL = "https://api.openweathermap.org/data/2.5/weather"
FORECAST_URL = "https://api.openweathermap.org/data/2.5/forecast"
WEATHER_CACHE_TTL_SECONDS = 45 * 60  # 45 minutes


def _for_dates(result: dict, dates: list[str]) -> dict:
    """`result` narrowed to `dates` (every forecast day when `dates` is empty)."""
    if not dates:
        return result
    wanted = set(dates)
    return {**result, "forecast": [d for d in result["forecast"] if d["date"] in wanted]}


def _local_date(slice_: dict) -> str:
    """The Sri Lanka calendar date a 3-hour slice belongs to. OpenWeather's
    `dt_txt` is a UTC date, which files the 00:00-05:30 local slices under
    the previous day; `dt` (unix seconds) converted with the explicit
    Asia/Colombo zone is right regardless of the server's own timezone."""
    if "dt" in slice_:
        return datetime.fromtimestamp(slice_["dt"], tz=SRI_LANKA_TZ).date().isoformat()
    return slice_["dt_txt"].split(" ")[0]


async def get_weather(lat: float, lon: float, dates: list[str]) -> dict | None:
    """
    Returns current conditions plus a per-date forecast summary for the
    given `dates` (ISO strings, e.g. "2026-08-25"); an empty `dates` returns
    every day OpenWeather forecasts (about 5).
    Shape:
      {
        "current": {"temp": float, "condition": str, "humidity": int},
        "forecast": [
          {"date": "2026-08-25", "temp_min": float, "temp_max": float,
           "condition": str, "rain_probability": float},
          ...
        ]
      }
    On any failure (bad key, network issue, API down), returns None —
    never raises. The Planner proceeds without weather in that case.
    """
    # Cached UNFILTERED and narrowed to `dates` per call. It used to cache
    # the already-filtered result, so the first caller for a place decided
    # what every later caller got for the next 45 minutes - a "tomorrow"
    # question after a 3-day plan could get the plan's days back instead.
    cache_key = f"weather:{lat:.2f}:{lon:.2f}"
    cached = await cache_get(cache_key)
    if cached:
        return _for_dates(cached, dates)

    if not settings.openweather_api_key:
        return None

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            current_resp = await client.get(CURRENT_URL, params={
                "lat": lat, "lon": lon,
                "appid": settings.openweather_api_key,
                "units": "metric",
            })
            current_resp.raise_for_status()
            current_data = current_resp.json()

            forecast_resp = await client.get(FORECAST_URL, params={
                "lat": lat, "lon": lon,
                "appid": settings.openweather_api_key,
                "units": "metric",
            })
            forecast_resp.raise_for_status()
            forecast_data = forecast_resp.json()

        current = {
            "temp": current_data["main"]["temp"],
            "condition": current_data["weather"][0]["main"],
            "humidity": current_data["main"]["humidity"],
        }

        # Group the 3-hour slices by Sri Lanka calendar date.
        by_date: dict[str, list[dict]] = defaultdict(list)
        for slice_ in forecast_data.get("list", []):
            by_date[_local_date(slice_)].append(slice_)

        forecast = []
        for day, slices in sorted(by_date.items()):
            temps = [s["main"]["temp"] for s in slices]
            pops = [s.get("pop", 0.0) for s in slices]  # probability of precipitation, 0-1

            conditions = [s["weather"][0]["main"] for s in slices]
            dominant_condition = max(set(conditions), key=conditions.count)

            forecast.append({
                "date": day,
                "temp_min": min(temps),
                "temp_max": max(temps),
                "condition": dominant_condition,
                "rain_probability": max(pops),
            })

        result = {"current": current, "forecast": forecast}
        await cache_set(cache_key, result, WEATHER_CACHE_TTL_SECONDS)
        return _for_dates(result, dates)

    except Exception:
        return None
