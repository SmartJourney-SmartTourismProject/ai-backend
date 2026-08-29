# app/tools/disaster_tool.py
import asyncio
import math
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import httpx

from app.utils.cache import cache_get, cache_set


EONET_URL = "https://eonet.gsfc.nasa.gov/api/v3/events"
USGS_URL = "https://earthquake.usgs.gov/fdsnws/event/1/query"
GDACS_URL = "https://www.gdacs.org/xml/rss.xml"
DISASTER_CACHE_TTL_SECONDS = 60 * 60  # 1 hour - disasters change slower than weather


#distance in kilometers between two locations on Earth using their latitude and longitude.
def _distance_km(lat1, lon1, lat2, lon2) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(a))

#fetches active natural-disaster events from NASA EONET and
# returns only the events that are within a specified distance of the user's destination.
# Returns None if the request itself failed (so get_disaster_info can tell
# "this source is down" apart from "this source confirmed zero nearby
# events" - the latter is a real []), and [] once it succeeded regardless
# of how many (zero or more) events matched.
async def _fetch_eonet(lat, lon, radius_km) -> list[dict] | None:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(EONET_URL, params={"status": "open", "days": 20})
            resp.raise_for_status()
            data = resp.json()
    except Exception:
        return None
    events = []
    for event in data.get("events", []):
        geometries = event.get("geometries") or []
        if not geometries:
            continue
        coords = geometries[0].get("coordinates")
        if not coords or len(coords) < 2:
            continue
        distance = _distance_km(lat, lon, coords[1], coords[0])
        if distance > radius_km:
            continue
        category = (event.get("categories") or [{}])[0].get("id", "").lower()
        severity = (
            "red" if category in {"volcanoes", "wildfires", "severe_storms"}
            else "orange" if category in {"floods", "tropical_cyclones", "droughts"}
            else "green"
        )
        events.append({"type": category or "eonet", "severity": severity,
                        "title": event.get("title"), "source": "EONET",
                        "distance_km": round(distance, 1)})
    return events

#specifically checks for earthquakes using the USGS API.
async def _fetch_usgs(lat, lon, radius_km) -> list[dict] | None:
    try:
        start_time = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(USGS_URL, params={
                "format": "geojson", "latitude": lat, "longitude": lon,
                "maxradiuskm": radius_km, "minmagnitude": 4, "starttime": start_time,
            })
            resp.raise_for_status()
            data = resp.json()
    except Exception:
        return None
    events = []
    for feature in data.get("features", []):
        props = feature.get("properties", {})
        mag = props.get("mag") or 0
        coords = feature.get("geometry", {}).get("coordinates", [None, None])
        distance = _distance_km(lat, lon, coords[1], coords[0]) if coords[0] is not None else None
        severity = "red" if mag >= 7 else "orange" if mag >= 6 else "green"
        events.append({"type": "earthquake", "severity": severity,
                        "title": f"Earthquake M{mag} - {props.get('place', 'Unknown location')}",
                        "source": "USGS",
                        "distance_km": round(distance, 1) if distance is not None else None})
    return events

#fetches the GDACS disaster RSS feed, extracts the location and severity of each alert,
#keeps only alerts within the specified radius of the destination, and returns them in a simplified format.
async def _fetch_gdacs(lat, lon, radius_km) -> list[dict] | None:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(GDACS_URL)
            resp.raise_for_status()
            xml_content = resp.text
    except Exception:
        return None
    events = []
    try:
        root = ET.fromstring(xml_content)
        ns = {"georss": "http://www.georss.org/georss"}
        for item in root.findall(".//item"):
            point_elem = item.find("georss:point", ns)
            if point_elem is None or not point_elem.text:
                continue
            try:
                g_lat, g_lon = map(float, point_elem.text.split())
            except ValueError:
                continue
            distance = _distance_km(lat, lon, g_lat, g_lon)
            if distance > radius_km:
                continue
            title = item.findtext("title", "") or ""
            severity = ("red" if "red" in title.lower()
                        else "orange" if "orange" in title.lower() else "green")
            events.append({"type": "gdacs_alert", "severity": severity, "title": title,
                            "source": "GDACS", "distance_km": round(distance, 1)})
    except ET.ParseError:
        return None
    return events

#checks multiple disaster data sources concurrently, combines the nearby events, sorts them by severity, 
#and determines whether the destination appears safe.
async def get_disaster_info(lat: float, lon: float, radius_km: int = 300) -> dict:
    cache_key = f"disaster:{lat:.2f}:{lon:.2f}:{radius_km}"
    cached = await cache_get(cache_key)
    if cached:
        return cached

    results = await asyncio.gather(
        _fetch_eonet(lat, lon, radius_km),
        _fetch_usgs(lat, lon, radius_km),
        _fetch_gdacs(lat, lon, radius_km),
        return_exceptions=True,
    )

    # Each _fetch_* returns None on failure, [] on a confirmed-empty result
    # (source is up, zero matching events) - only when EVERY source failed
    # do we not actually know whether it's safe, hence the "unavailable"
    # note rather than a confident (but potentially wrong) "safe".
    if all(r is None or isinstance(r, Exception) for r in results):
        return {"safe": True, "active_events": [], "note": "disaster data unavailable"}

    active_events: list[dict] = []
    for r in results:
        if isinstance(r, list):
            active_events.extend(r)

    rank = {"red": 0, "orange": 1, "green": 2}
    active_events.sort(key=lambda e: rank.get(e["severity"], 3))

    result = {"safe": len(active_events) == 0, "active_events": active_events}
    await cache_set(cache_key, result, DISASTER_CACHE_TTL_SECONDS)
    return result
