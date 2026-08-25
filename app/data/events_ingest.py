"""
Weekly local-events refresh.

Pulls upcoming events (next ~30 days) per Sri Lanka district from Ticketmaster
Discovery, and upserts them into Supabase's `local_event` table. Run via
app/scheduler.py (weekly) or manually:

    python -m app.data.events_ingest

Note (2026-08-25): Eventbrite was dropped as a source. Its public event
*search* endpoint was deprecated in 2019 - third-party apps can now only list
events for their own organization, not search public events by location, so
it structurally cannot serve this use case (confirmed: /events/search/
returns 404 regardless of key validity). Ticketmaster's key works but
currently returns zero events for Sri Lanka (checked Colombo/Kandy/Galle at
100km radius) - this job is kept running in case that changes, but real event
coverage for the platform comes from admin-entered events (Admin Panel,
BUILD_PLAN.md Phase 7), not this API.
"""
from __future__ import annotations
import time
import logging
from datetime import datetime, timedelta
from typing import Any

import requests

from app.config.settings import settings
from app.data.sri_lanka_districts import DISTRICTS
from app.data.supabase_writer import get_district_id_map, to_point_wkt, upsert_rows

logger = logging.getLogger(__name__)

TICKETMASTER_URL = "https://app.ticketmaster.com/discovery/v2/events.json"
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


def process_district(district: dict, session: requests.Session) -> list[dict[str, Any]]:
    print(f"\nFetching events for {district['name']} District...")

    events = fetch_ticketmaster_events(district["lat"], district["lon"], session)
    for ev in events:
        ev["district"] = district["name"]

    print(f"  -> Found {len(events)} events.")
    return events


def build_db_rows(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Maps our internal event shape (flat district/lat/lon) onto `local_event`'s
    real schema: district_id foreign key and a PostGIS `location` point.
    Events whose district can't be resolved are dropped (logged) rather than
    sent with a broken row.
    """
    district_map = get_district_id_map()

    rows = []
    for ev in events:
        district_id = district_map.get(ev["district"])
        if district_id is None:
            print(f"  [!] Skipping '{ev['name']}' - unknown district, not synced.")
            continue

        rows.append({
            "district_id": district_id,
            "source": ev["source"],
            "external_ref": ev["external_ref"],
            "name": ev["name"],
            "description": ev["description"],
            "start_datetime": ev["start_datetime"],
            "end_datetime": ev["end_datetime"],
            "venue_name": ev["venue_name"],
            "location": to_point_wkt(ev["lat"], ev["lon"]),
            "price_info": ev["price_info"],
            "is_verified": False,
        })

    return rows


def run_ingestion() -> None:
    print("--- STARTING WEEKLY EVENTS INGESTION (all 25 districts) ---")

    if not settings.ticketmaster_api_key:
        print("[WARN] No Ticketmaster API key configured - nothing to sync.")
        return

    session = requests.Session()
    session.headers.update({"User-Agent": "SmartJourney-AI-Backend/1.0"})

    all_events = []
    for district in DISTRICTS:
        all_events.extend(process_district(district, session))
        time.sleep(2)  # polite pause between districts

    print(f"\n[SUCCESS] Ingestion complete. Total events found: {len(all_events)}")

    db_rows = build_db_rows(all_events)
    synced = upsert_rows("local_event", db_rows, on_conflict="external_ref")
    if synced:
        print(f"[SUCCESS] Upserted {synced} rows into Supabase 'local_event'.")
    else:
        print("[WARN] Supabase not configured (or upsert failed) - rows were not persisted.")


if __name__ == "__main__":
    run_ingestion()
