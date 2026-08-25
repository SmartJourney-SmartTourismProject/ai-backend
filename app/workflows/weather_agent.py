"""
WeatherAgent: Fetches weather forecast for destination using OpenWeather API.
Integrated with orchestrator workflow.
"""
from __future__ import annotations
from typing import Any, Optional
import aiohttp
import logging
from datetime import datetime, timedelta

from app.config.settings import settings
from app.core.base_agent import BaseAgent
from app.core.result import AgentResult
from app.core.state import TripState
from app.data.sri_lanka_districts import get_district
from app.utils.cache import cache_get, cache_set

logger = logging.getLogger(__name__)

# Real-time weather isn't batch-refreshed - it's fetched live per request and
# cached briefly so repeat requests for the same location within a few
# minutes don't re-hit OpenWeather (see BUILD_PLAN.md §1/§8).
WEATHER_CACHE_TTL_SECONDS = 15 * 60

# A few well-known tourist towns that aren't a district name/capital
# themselves, kept alongside the district lookup for backwards compatibility.
_EXTRA_TOWN_COORDS = {
    "negombo": {"lat": 7.2064, "lon": 79.8581},
    "ella": {"lat": 6.8658, "lon": 81.0467},
    "mirissa": {"lat": 5.9471, "lon": 80.7757},
}


class WeatherAgent(BaseAgent):
    """Fetches weather forecast for trip destination."""

    name = "weather_agent"
    OPENWEATHER_BASE_URL = "https://api.openweathermap.org/data/2.5"

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.api_key = settings.openweather_api_key

    async def execute(self, state: TripState) -> AgentResult:
        """
        Fetch current weather and forecast for destination.
        Returns weather data added to state.weather.
        """
        if not state.destination:
            return AgentResult(
                success=False,
                message="No destination provided for weather check."
            )

        if not self.api_key:
            logger.warning("OpenWeather API key not configured, using fallback weather.")
            state.weather = self._get_fallback_weather()
            state.completed_steps.append("weather_agent")
            return AgentResult(
                success=True,
                message="Using fallback weather data (API key not configured)."
            )

        try:
            coords = await self._get_destination_coords(state.destination)

            if not coords:
                state.weather = self._get_fallback_weather()
                state.completed_steps.append("weather_agent")
                return AgentResult(
                    success=True,
                    message=f"Could not find coordinates for {state.destination}, using fallback."
                )

            cache_key = f"weather:{coords['lat']:.2f}:{coords['lon']:.2f}"
            cached = await cache_get(cache_key)
            if cached:
                state.weather = {**cached, "destination": state.destination}
                state.completed_steps.append("weather_agent")
                return AgentResult(
                    success=True,
                    message=f"Weather forecast (cached) for {state.destination}."
                )

            # Fetch current weather
            current_weather = await self._fetch_current_weather(
                coords["lat"], coords["lon"]
            )

            # Fetch forecast
            forecast = await self._fetch_forecast(
                coords["lat"], coords["lon"]
            )

            state.weather = {
                "destination": state.destination,
                "coordinates": coords,
                "current": current_weather,
                "forecast": forecast,
                "fetched_at": datetime.utcnow().isoformat(),
            }
            await cache_set(cache_key, state.weather, WEATHER_CACHE_TTL_SECONDS)

            state.completed_steps.append("weather_agent")
            return AgentResult(
                success=True,
                message=f"Weather forecast fetched for {state.destination}."
            )

        except Exception as e:
            logger.exception("Weather agent failed: %s", str(e))
            state.weather = self._get_fallback_weather()
            state.errors.append(f"Weather check failed: {str(e)}")
            state.completed_steps.append("weather_agent")
            return AgentResult(
                success=True,  # Don't fail the entire flow
                message=f"Weather check failed, using fallback: {str(e)}"
            )

    async def _get_destination_coords(self, destination: str) -> Optional[dict]:
        """
        Get latitude/longitude for destination: checks all 25 Sri Lanka
        districts first, then a short list of well-known tourist towns.
        """
        district = get_district(destination)
        if district:
            return {"lat": district["lat"], "lon": district["lon"]}

        return _EXTRA_TOWN_COORDS.get(destination.lower().strip())
    
    async def _fetch_current_weather(self, lat: float, lon: float) -> dict:
        """Fetch current weather from OpenWeather API."""
        url = f"{self.OPENWEATHER_BASE_URL}/weather"
        params = {
            "lat": lat,
            "lon": lon,
            "appid": self.api_key,
            "units": "metric",
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return {
                        "temperature": data.get("main", {}).get("temp"),
                        "feels_like": data.get("main", {}).get("feels_like"),
                        "humidity": data.get("main", {}).get("humidity"),
                        "description": data.get("weather", [{}])[0].get("description"),
                        "condition": data.get("weather", [{}])[0].get("main"),
                        "wind_speed": data.get("wind", {}).get("speed"),
                        "rainfall_chance": data.get("clouds", {}).get("all"),
                    }
                else:
                    logger.warning(f"OpenWeather API returned status {resp.status}")
                    return {}
    
    async def _fetch_forecast(self, lat: float, lon: float) -> list:
        """Fetch 5-day forecast from OpenWeather API."""
        url = f"{self.OPENWEATHER_BASE_URL}/forecast"
        params = {
            "lat": lat,
            "lon": lon,
            "appid": self.api_key,
            "units": "metric",
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    
                    # Group by day and take midday forecast
                    forecast_by_day = {}
                    for item in data.get("list", []):
                        dt = datetime.fromtimestamp(item["dt"])
                        day_key = dt.date().isoformat()
                        
                        # Prefer midday forecast (12:00)
                        if day_key not in forecast_by_day or dt.hour == 12:
                            forecast_by_day[day_key] = {
                                "date": day_key,
                                "temperature": item["main"]["temp"],
                                "description": item["weather"][0]["description"],
                                "condition": item["weather"][0]["main"],
                                "humidity": item["main"]["humidity"],
                                "wind_speed": item["wind"]["speed"],
                                "rainfall_probability": item.get("pop", 0) * 100,
                            }
                    
                    return list(forecast_by_day.values())
                else:
                    logger.warning(f"OpenWeather forecast API returned status {resp.status}")
                    return []
    
    def _get_fallback_weather(self) -> dict:
        """
        Return a fallback weather object when API fails or is not configured.
        Represents typical tropical weather (Sri Lanka context).
        """
        return {
            "destination": "Unknown",
            "current": {
                "temperature": 28,
                "feels_like": 32,
                "humidity": 75,
                "description": "Partly cloudy",
                "condition": "Clouds",
                "wind_speed": 3.5,
                "rainfall_chance": 40,
            },
            "forecast": [
                {
                    "date": (datetime.utcnow() + timedelta(days=i)).date().isoformat(),
                    "temperature": 27 + (i % 3),
                    "description": ["Sunny", "Partly cloudy", "Rainy"][i % 3],
                    "condition": ["Sunny", "Clouds", "Rain"][i % 3],
                    "humidity": 70 + (i * 5) % 20,
                    "wind_speed": 3.0 + (i % 2),
                    "rainfall_probability": [20, 40, 60][i % 3],
                }
                for i in range(5)
            ],
            "fetched_at": datetime.utcnow().isoformat(),
            "note": "Using fallback weather data",
        }
