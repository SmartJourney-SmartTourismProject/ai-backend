"""
ContextAgent: Fetches weather forecast (OpenWeather) and disaster alerts
(NASA EONET, USGS, GDACS) for the trip destination in one unit.

Merged from what were previously separate WeatherAgent/DisasterAgent
classes - both need the same destination coordinates, so combining them
means that lookup happens once instead of twice, while still fetching
weather and disaster data concurrently internally (they're independent
I/O calls with no data dependency on each other).
"""
from __future__ import annotations
from typing import Any, Optional, List
import asyncio
import aiohttp
import logging
from datetime import datetime, timedelta
import xml.etree.ElementTree as ET

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


class ContextAgent(BaseAgent):
    """Fetches weather forecast and disaster alerts for trip destination."""

    name = "context_agent"
    OPENWEATHER_BASE_URL = "https://api.openweathermap.org/data/2.5"
    EONET_URL = "https://eonet.gsfc.nasa.gov/api/v3/events"
    USGS_URL = "https://earthquake.usgs.gov/fdsnws/event/1/query"
    GDACS_URL = "https://www.gdacs.org/xml/rss.xml"

    EARTHQUAKE_MIN_MAGNITUDE = 4.0
    FORECAST_DAYS_AHEAD = 7

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.api_key = settings.openweather_api_key

    async def execute(self, state: TripState) -> AgentResult:
        """
        Resolves destination coordinates once, then fetches weather and
        disaster data concurrently. Sets state.weather and state.disaster.
        """
        if not state.destination:
            return AgentResult(
                success=False,
                message="No destination provided for weather/disaster check."
            )

        coords = self._get_destination_coords(state.destination)

        if not coords:
            state.weather = self._get_fallback_weather()
            state.disaster = {"alerts": [], "destination": state.destination}
            state.completed_steps.append("weather_agent")
            state.completed_steps.append("disaster_agent")
            return AgentResult(
                success=True,
                message=f"Could not find coordinates for {state.destination}; "
                        f"using fallback weather, no disaster check possible."
            )

        weather_result, disaster_result = await asyncio.gather(
            self._get_weather(state.destination, coords),
            self._get_disaster_info(state.destination, coords),
        )

        state.weather = weather_result
        state.disaster = disaster_result
        state.completed_steps.append("weather_agent")
        state.completed_steps.append("disaster_agent")

        alert_count = len(disaster_result.get("alerts", []))
        return AgentResult(
            success=True,
            message=f"Weather and disaster check completed for {state.destination}. "
                    f"Found {alert_count} disaster alert(s)."
        )

    # --- Shared coordinate resolution ------------------------------------

    def _get_destination_coords(self, destination: str) -> Optional[dict]:
        """
        Get latitude/longitude for destination: checks all 25 Sri Lanka
        districts first, then a short list of well-known tourist towns.
        """
        district = get_district(destination)
        if district:
            return {"lat": district["lat"], "lon": district["lon"]}

        return _EXTRA_TOWN_COORDS.get(destination.lower().strip())

    # --- Weather -----------------------------------------------------------

    async def _get_weather(self, destination: str, coords: dict) -> dict:
        """Fetches current weather + forecast, with caching and graceful fallback."""
        if not self.api_key:
            logger.warning("OpenWeather API key not configured, using fallback weather.")
            return self._get_fallback_weather()

        try:
            cache_key = f"weather:{coords['lat']:.2f}:{coords['lon']:.2f}"
            cached = await cache_get(cache_key)
            if cached:
                return {**cached, "destination": destination}

            current_weather = await self._fetch_current_weather(coords["lat"], coords["lon"])
            forecast = await self._fetch_forecast(coords["lat"], coords["lon"])

            weather = {
                "destination": destination,
                "coordinates": coords,
                "current": current_weather,
                "forecast": forecast,
                "fetched_at": datetime.utcnow().isoformat(),
            }
            await cache_set(cache_key, weather, WEATHER_CACHE_TTL_SECONDS)
            return weather

        except Exception as e:
            logger.exception("Weather fetch failed: %s", str(e))
            return self._get_fallback_weather()

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

    # --- Disaster ------------------------------------------------------------

    async def _get_disaster_info(self, destination: str, coords: dict) -> dict:
        """Fetches disaster alerts from EONET/USGS/GDACS concurrently, merging results."""
        try:
            eonet_events, earthquakes, gdacs_alerts = await asyncio.gather(
                self._fetch_eonet_events(coords),
                self._fetch_usgs_earthquakes(coords),
                self._fetch_gdacs_alerts(coords),
            )
            all_alerts = eonet_events + earthquakes + gdacs_alerts

            if all_alerts:
                logger.warning(f"Found {len(all_alerts)} disaster alert(s) near {destination}")

            return {
                "destination": destination,
                "coordinates": coords,
                "alerts": all_alerts,
                "has_severe_alert": any(a.get("severity", "low") in ["high", "critical"] for a in all_alerts),
                "fetched_at": datetime.utcnow().isoformat(),
            }
        except Exception as e:
            logger.exception("Disaster check failed: %s", str(e))
            return {"alerts": [], "destination": destination}

    async def _fetch_eonet_events(self, coords: dict) -> List[dict]:
        """
        Fetch NASA EONET events (wildfires, storms, volcanoes, etc.)
        API: https://eonet.gsfc.nasa.gov/api/v3/events
        """
        try:
            params = {
                "status": "open",
                "days": self.FORECAST_DAYS_AHEAD,
            }

            async with aiohttp.ClientSession() as session:
                async with session.get(self.EONET_URL, params=params) as resp:
                    if resp.status == 200:
                        # EONET mislabels its JSON responses as application/rss+xml -
                        # content_type=None skips aiohttp's strict content-type check.
                        data = await resp.json(content_type=None)
                        alerts = []

                        for event in data.get("events", []):
                            # Check if event is near destination
                            if self._is_near_coords(event, coords):
                                alerts.append({
                                    "type": "eonet",
                                    "category": event.get("categories", [{}])[0].get("title"),
                                    "title": event.get("title"),
                                    "description": event.get("description", ""),
                                    "severity": self._assess_severity(event),
                                    "last_update": event.get("geometries", [{}])[0].get("date"),
                                    "url": event.get("sources", [{}])[0].get("url"),
                                })

                        return alerts
        except Exception as e:
            logger.warning(f"EONET fetch failed: {str(e)}")

        return []

    async def _fetch_usgs_earthquakes(self, coords: dict) -> List[dict]:
        """
        Fetch USGS earthquake data.
        API: https://earthquake.usgs.gov/fdsnws/event/1/query
        """
        try:
            start_time = (datetime.utcnow() - timedelta(days=7)).isoformat()

            params = {
                "format": "geojson",
                "latitude": coords["lat"],
                "longitude": coords["lon"],
                "maxradiuskm": 300,  # 300km radius
                "minmagnitude": self.EARTHQUAKE_MIN_MAGNITUDE,
                "starttime": start_time,
            }

            async with aiohttp.ClientSession() as session:
                async with session.get(self.USGS_URL, params=params) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        alerts = []

                        for feature in data.get("features", []):
                            props = feature.get("properties", {})
                            alerts.append({
                                "type": "earthquake",
                                "title": f"Earthquake - Magnitude {props.get('mag')}",
                                "magnitude": props.get("mag"),
                                "depth_km": feature.get("geometry", {}).get("coordinates", [None, None, None])[2],
                                "description": props.get("place", "Unknown location"),
                                "severity": self._assess_earthquake_severity(props.get("mag", 0)),
                                "timestamp": datetime.fromtimestamp(
                                    props.get("time", 0) / 1000
                                ).isoformat(),
                                "url": props.get("url"),
                            })

                        return alerts
        except Exception as e:
            logger.warning(f"USGS fetch failed: {str(e)}")

        return []

    async def _fetch_gdacs_alerts(self, coords: dict) -> List[dict]:
        """
        Fetch GDACS alerts (floods, cyclones, tsunamis, etc.)
        API: https://www.gdacs.org/xml/rss.xml (GeoRSS format)
        """
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(self.GDACS_URL) as resp:
                    if resp.status == 200:
                        xml_content = await resp.text()
                        root = ET.fromstring(xml_content)

                        alerts = []
                        # GeoRSS namespace
                        ns = {"georss": "http://www.georss.org/georss"}

                        for item in root.findall(".//item"):
                            title = item.findtext("title", "")
                            description = item.findtext("description", "")
                            link = item.findtext("link", "")

                            # Try to extract georss:point (latitude longitude)
                            point_elem = item.find("georss:point", ns)
                            if point_elem is not None and point_elem.text:
                                try:
                                    lat, lon = map(float, point_elem.text.split())
                                    if self._distance_km(coords["lat"], coords["lon"], lat, lon) <= 500:
                                        alerts.append({
                                            "type": "gdacs",
                                            "title": title,
                                            "description": description,
                                            "severity": "high" if "alert" in title.lower() else "medium",
                                            "coordinates": {"lat": lat, "lon": lon},
                                            "url": link,
                                        })
                                except (ValueError, AttributeError):
                                    pass

                        return alerts
        except Exception as e:
            logger.warning(f"GDACS fetch failed: {str(e)}")

        return []

    def _is_near_coords(self, event: dict, coords: dict) -> bool:
        """Check if EONET event is near the destination."""
        geometries = event.get("geometries", [])
        if not geometries:
            return False

        try:
            geom = geometries[0]
            lon, lat = geom.get("coordinates", [None, None])

            if lon is None or lat is None:
                return False

            # Consider "near" if within 500km
            distance = self._distance_km(coords["lat"], coords["lon"], lat, lon)
            return distance <= 500
        except (ValueError, TypeError, IndexError):
            return False

    def _distance_km(self, lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """Calculate distance between two coordinates using Haversine formula."""
        from math import radians, cos, sin, asin, sqrt

        lon1, lat1, lon2, lat2 = map(radians, [lon1, lat1, lon2, lat2])
        dlon = lon2 - lon1
        dlat = lat2 - lat1
        a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
        c = 2 * asin(sqrt(a))
        km = 6371 * c
        return km

    def _assess_severity(self, event: dict) -> str:
        """Assess EONET event severity."""
        category = event.get("categories", [{}])[0].get("id", "").lower()

        if category in ["volcanoes", "wildfires", "severe_storms"]:
            return "high"
        elif category in ["floods", "tropical_cyclones", "droughts"]:
            return "medium"
        else:
            return "low"

    def _assess_earthquake_severity(self, magnitude: float) -> str:
        """Assess earthquake severity by magnitude."""
        if magnitude >= 6.0:
            return "critical"
        elif magnitude >= 5.0:
            return "high"
        elif magnitude >= 4.5:
            return "medium"
        else:
            return "low"
