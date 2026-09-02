"""
Daily local-events sync from Ticketmaster Discovery -
docs/master_plan/DATA_PLATFORM.md §5.2. Supersedes app/data/events_ingest.py:
districts come from the `district` table, not a hardcoded list, and rows
target local_event's real schema (tags text[], price_min/price_max,
compound (source, external_ref) uniqueness).

Known gap, unchanged from the old script (confirmed again 2026-09-02): the
Ticketmaster key returns ~zero events for Sri Lanka. This connector is kept
running in case that changes; real event coverage is expected to come from
admin-entered events (NestJS admin panel), not this API - see
docs/master_plan/PROJECT_MASTER_PLAN.md §7 risks.

    python -m app.data.connectors.ticketmaster_events --district "Kandy"
"""
from __future__ import annotations

import argparse
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import requests

from app.config.settings import settings
from app.data.connectors.base import District, fetch_all_districts
from app.data.postgres_writer import to_point_wkt, upsert_rows

logger = logging.getLogger(__name__)

TICKETMASTER_URL = "https://app.ticketmaster.com/discovery/v2/events.json"
SEARCH_RADIUS_KM = 50
LOOKAHEAD_DAYS = 30

NAME = "ticketmaster_events"
CADENCE = "daily"
REQUIRES_KEY = True
SCOPE = "per_district"


class TicketmasterEventsConnector:
    name = NAME
    cadence = CADENCE
    requires_key = REQUIRES_KEY
    scope = SCOPE

    async def fetch(self, district: Optional[District]) -> list[dict[str, Any]]:
        if district is None:
            raise ValueError("ticketmaster_events is a per_district connector")
        if not settings.ticketmaster_api_key:
            return []

        params = {
            "apikey": settings.ticketmaster_api_key,
            "latlong": f"{district.lat},{district.lon}",
            "radius": SEARCH_RADIUS_KM, "unit": "km",
            "startDateTime": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "endDateTime": (datetime.now(timezone.utc) + timedelta(days=LOOKAHEAD_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        try:
            resp = requests.get(TICKETMASTER_URL, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except requests.exceptions.RequestException as e:
            logger.warning(f"Ticketmaster fetch failed for {district.name}: {e}")
            return []
        return data.get("_embedded", {}).get("events", [])

    def normalize(self, raw: list[dict[str, Any]], district: Optional[District]) -> list[dict[str, Any]]:
        if not raw or district is None:
            return []
        rows = []
        for ev in raw:
            venue = (ev.get("_embedded", {}).get("venues") or [{}])[0]
            location = venue.get("location", {})
            price_ranges = ev.get("priceRanges") or [{}]
            price = price_ranges[0]
            rows.append({
                "district_id": district.id,
                "name": ev.get("name"),
                "description": ev.get("info") or ev.get("pleaseNote"),
                "start_datetime": ev.get("dates", {}).get("start", {}).get("dateTime"),
                "end_datetime": None,
                "venue_name": venue.get("name"),
                "lat": float(location["latitude"]) if location.get("latitude") else district.lat,
                "lon": float(location["longitude"]) if location.get("longitude") else district.lon,
                "price_min": price.get("min"),
                "price_max": price.get("max"),
                "source": "ticketmaster",
                "external_ref": ev.get("id"),
            })
        return [r for r in rows if r["name"] and r["start_datetime"] and r["external_ref"]]

    def upsert(self, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0
        db_rows = [{
            "district_id": r["district_id"],
            "name": r["name"],
            "description": r["description"],
            "start_datetime": r["start_datetime"],
            "end_datetime": r["end_datetime"],
            "venue_name": r["venue_name"],
            "location": to_point_wkt(r["lat"], r["lon"]),
            "price_min": r["price_min"],
            "price_max": r["price_max"],
            "source": r["source"],
            "external_ref": r["external_ref"],
            "is_verified": False,
        } for r in rows]
        return upsert_rows(
            "local_event", db_rows, on_conflict=("source", "external_ref"), geo_columns={"location"},
        )


async def run_all(district_filter: str = None) -> None:
    connector = TicketmasterEventsConnector()
    districts = fetch_all_districts()
    if district_filter:
        districts = [d for d in districts if district_filter.lower() in d.name.lower()]
    if not districts:
        print("[FATAL] No districts found.")
        return
    if not settings.ticketmaster_api_key:
        print("[WARN] TICKETMASTER_API_KEY not set - nothing to sync.")
        return

    total = 0
    for i, d in enumerate(districts, 1):
        print(f"[{i}/{len(districts)}] {d.name} ...", end=" ", flush=True)
        raw = await connector.fetch(d)
        rows = connector.normalize(raw, d)
        count = connector.upsert(rows)
        total += count
        print(f"{count} events (of {len(raw)} found)")
        time.sleep(1)

    print(f"\n[SUCCESS] Upserted {total} event(s) total.")


if __name__ == "__main__":
    import asyncio

    logging.basicConfig(level=logging.INFO)
    ap = argparse.ArgumentParser()
    ap.add_argument("--district", default=None)
    args = ap.parse_args()
    asyncio.run(run_all(args.district))
