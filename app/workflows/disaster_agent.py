"""
DisasterAgent: Checks for active disaster alerts (earthquakes, floods, wildfires, etc.)
using free public APIs: NASA EONET, USGS, GDACS.
"""
from __future__ import annotations
from typing import Any, Optional, List
import aiohttp
import logging
from datetime import datetime, timedelta
import xml.etree.ElementTree as ET

from app.core.base_agent import BaseAgent
from app.core.result import AgentResult
from app.core.state import TripState

logger = logging.getLogger(__name__)


class DisasterAgent(BaseAgent):
    """
    Checks for active disaster alerts near destination.
    Uses public APIs with no authentication required.
    """
    
    name = "disaster_agent"
    
    # Public API endpoints (no auth required)
    EONET_URL = "https://eonet.gsfc.nasa.gov/api/v3/events"
    USGS_URL = "https://earthquake.usgs.gov/fdsnws/event/1/query"
    GDACS_URL = "https://www.gdacs.org/xml/rss.xml"
    
    # Disaster alert thresholds
    EARTHQUAKE_MIN_MAGNITUDE = 4.0
    FORECAST_DAYS_AHEAD = 7
    
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
    
    async def execute(self, state: TripState) -> AgentResult:
        """
        Fetch disaster alerts for destination.
        Returns alerts added to state.disaster.
        """
        if not state.destination:
            return AgentResult(
                success=True,
                message="No destination provided for disaster check."
            )
        
        try:
            # Get coordinates for destination
            coords = await self._get_destination_coords(state.destination)
            
            if not coords:
                state.disaster = {"alerts": [], "destination": state.destination}
                state.completed_steps.append("disaster_agent")
                return AgentResult(
                    success=True,
                    message=f"Could not find coordinates for {state.destination}."
                )
            
            # Fetch from all three APIs
            eonet_events = await self._fetch_eonet_events(coords)
            earthquakes = await self._fetch_usgs_earthquakes(coords)
            gdacs_alerts = await self._fetch_gdacs_alerts(coords)
            
            # Combine and assess severity
            all_alerts = eonet_events + earthquakes + gdacs_alerts
            
            state.disaster = {
                "destination": state.destination,
                "coordinates": coords,
                "alerts": all_alerts,
                "has_severe_alert": any(a.get("severity", "low") in ["high", "critical"] 
                                       for a in all_alerts),
                "fetched_at": datetime.utcnow().isoformat(),
            }
            
            if all_alerts:
                logger.warning(
                    f"Found {len(all_alerts)} disaster alert(s) near {state.destination}"
                )
            
            state.completed_steps.append("disaster_agent")
            return AgentResult(
                success=True,
                message=f"Disaster check completed for {state.destination}. "
                       f"Found {len(all_alerts)} alert(s)."
            )
        
        except Exception as e:
            logger.exception("Disaster agent failed: %s", str(e))
            state.disaster = {"alerts": [], "destination": state.destination}
            state.errors.append(f"Disaster check failed: {str(e)}")
            state.completed_steps.append("disaster_agent")
            return AgentResult(
                success=True,  # Don't fail the entire flow
                message=f"Disaster check failed: {str(e)}"
            )
    
    async def _get_destination_coords(self, destination: str) -> Optional[dict]:
        """Get coordinates for destination (mock for now)."""
        mock_coords = {
            "colombo": {"lat": 6.9271, "lon": 80.7789},
            "kandy": {"lat": 7.2906, "lon": 80.6337},
            "galle": {"lat": 6.0535, "lon": 80.2210},
            "negombo": {"lat": 7.2064, "lon": 79.8581},
            "ella": {"lat": 6.8658, "lon": 81.0467},
            "mirissa": {"lat": 5.9471, "lon": 80.7757},
        }
        
        destination_lower = destination.lower().strip()
        return mock_coords.get(destination_lower)
    
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
                        data = await resp.json()
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
