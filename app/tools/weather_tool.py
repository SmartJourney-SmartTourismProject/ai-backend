# app/tools/weather_tool.py
from collections import defaultdict
from datetime import datetime

import httpx

from app.config.settings import settings

CURRENT_URL = "https://api.openweathermap.org/data/2.5/weather"
FORECAST_URL = "https://api.openweathermap.org/data/2.5/forecast"


async def get_weather(lat: float, lon: float, dates: list[str]) -> dict | None:
    """
    Returns current conditions plus a per-date forecast summary for the
    given `dates` (ISO strings, e.g. "2026-08-25").
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

        # Group the 3-hour slices by calendar date
        by_date: dict[str, list[dict]] = defaultdict(list)
        for slice_ in forecast_data.get("list", []):
            slice_date = datetime.fromtimestamp(slice_["dt"]).date().isoformat()
            by_date[slice_date].append(slice_)

        wanted_dates = set(dates) if dates else set(by_date.keys())

        forecast = []
        for day, slices in sorted(by_date.items()):
            if day not in wanted_dates:
                continue

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

        return {"current": current, "forecast": forecast}

    except Exception:
        return None