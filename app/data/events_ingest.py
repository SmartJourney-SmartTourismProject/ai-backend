"""
Weekly local-events refresh.

Pulls upcoming events (next ~30 days) per Sri Lanka district from Ticketmaster
Discovery and Eventbrite, and upserts them into Supabase's `local_event`
table. Run via app/scheduler.py (weekly) or manually:

    python -m app.data.events_ingest
"""
from __future__ import annotations
import time
import logging
from datetime import datetime, timedelta
from typing import Any

import requests

from app.config.settings import settings
from app.data.sri_lanka_districts import DISTRICTS
from app.data.supabase_writer import upsert_rows

logger = logging.getLogger(__name__)

TICKETMASTER_URL = "https://app.ticketmaster.com/discovery/v2/events.json"
EVENTBRITE_URL = "https://www.eventbriteapi.com/v3/events/search/"
SEARCH_RADIUS_KM = 50
LOOKAHEAD_DAYS = 30


def fetch_ticketmaster_events(lat: float, lon: float, session: requests.Session) -> list[dict[str, Any]]:
    """Fetches events within SEARCH_RADIUS_KM of (lat, lon). Best-effort - returns [] on any failure."""
    if not settings.ticketmaster_api_key:
        return []

    params = {
        "apikey": settings.ticketmaster_api_key,
        "latlong": f"{lat},{lon}",
        "radius": SEARCH_RADIUS_KM,
        "unit": "km",
        "startDateTime": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "endDateTime": (datetime.utcnow() + timedelta(days=LOOKAHEAD_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    try:
        resp = session.get(TICKETMASTER_URL, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.RequestException as e:
        print(f"  [!] Ticketmaster error: {e}")
        return []

    events = []
    for ev in data.get("_embedded", {}).get("events", []):
        venue = (ev.get("_embedded", {}).get("venues") or [{}])[0]
        location = venue.get("location", {})
        events.append({
            "source": "ticketmaster",
            "external_ref": ev.get("id"),
            "name": ev.get("name"),
            "description": ev.get("info") or ev.get("pleaseNote"),
            "start_datetime": ev.get("dates", {}).get("start", {}).get("dateTime"),
            "end_datetime": None,
            "venue_name": venue.get("name"),
            "lat": float(location["latitude"]) if location.get("latitude") else lat,
            "lon": float(location["longitude"]) if location.get("longitude") else lon,
            "price_info": ev.get("priceRanges"),
        })
    return events


def fetch_eventbrite_events(lat: float, lon: float, session: requests.Session) -> list[dict[str, Any]]:
    """Fetches events within SEARCH_RADIUS_KM of (lat, lon). Best-effort - returns [] on any failure."""
    if not settings.eventbrite_api_key:
        return []

    params = {
        "location.latitude": lat,
        "location.longitude": lon,
        "location.within": f"{SEARCH_RADIUS_KM}km",
        "start_date.range_start": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "start_date.range_end": (datetime.utcnow() + timedelta(days=LOOKAHEAD_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    headers = {"Authorization": f"Bearer {settings.eventbrite_api_key}"}

    try:
        resp = session.get(EVENTBRITE_URL, params=params, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.RequestException as e:
        print(f"  [!] Eventbrite error: {e}")
        return []

    events = []
    for ev in data.get("events", []):
        venue = ev.get("venue") or {}
        address = venue.get("address", {})
        events.append({
            "source": "eventbrite",
            "external_ref": ev.get("id"),
            "name": (ev.get("name") or {}).get("text"),
            "description": (ev.get("description") or {}).get("text"),
            "start_datetime": (ev.get("start") or {}).get("utc"),
            "end_datetime": (ev.get("end") or {}).get("utc"),
            "venue_name": venue.get("name"),
            "lat": float(address["latitude"]) if address.get("latitude") else lat,
            "lon": float(address["longitude"]) if address.get("longitude") else lon,
            "price_info": None,
        })
    return events


def process_district(district: dict, session: requests.Session) -> list[dict[str, Any]]:
    print(f"\nFetching events for {district['name']} District...")

    events = []
    events.extend(fetch_ticketmaster_events(district["lat"], district["lon"], session))
    time.sleep(1)
    events.extend(fetch_eventbrite_events(district["lat"], district["lon"], session))

    for ev in events:
        ev["district"] = district["name"]

    print(f"  -> Found {len(events)} events.")
    return events


def run_ingestion() -> None:
    print("--- STARTING WEEKLY EVENTS INGESTION (all 25 districts) ---")

    if not settings.ticketmaster_api_key and not settings.eventbrite_api_key:
        print("[WARN] No Ticketmaster/Eventbrite API key configured - nothing to sync.")
        return

    session = requests.Session()
    session.headers.update({"User-Agent": "SmartJourney-AI-Backend/1.0"})

    all_events = []
    for district in DISTRICTS:
        all_events.extend(process_district(district, session))
        time.sleep(2)  # polite pause between districts

    print(f"\n[SUCCESS] Ingestion complete. Total events found: {len(all_events)}")

    synced = upsert_rows("local_event", all_events, on_conflict="external_ref")
    if synced:
        print(f"[SUCCESS] Upserted {synced} rows into Supabase 'local_event'.")
    else:
        print("[WARN] Supabase not configured (or upsert failed) - rows were not persisted.")


if __name__ == "__main__":
    run_ingestion()
