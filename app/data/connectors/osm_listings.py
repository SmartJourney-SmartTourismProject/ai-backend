"""
Weekly hotel/restaurant/attraction base-listing sync from OpenStreetMap
Overpass - the primary data source (docs/master_plan/DATA_PLATFORM.md §5.2).

Supersedes app/data/overpass_ingest.py: districts come from the `district`
table (Phase 1's real seed), not a hardcoded list; OSM tags map to this
project's canonical tag vocabulary via `tag_mapping` (§5.3) instead of a
single category guess; and `price_level` replaces the old `price_range`
string bucket to match the real schema. Real hotel prices are a separate
connector (booking_prices.py) that enriches these rows afterward.

Run via app/data/pipeline.py (recommended - tracks data_source_run), or
directly for manual testing:

    python -m app.data.connectors.osm_listings --district "Kandy District"
    python -m app.data.connectors.osm_listings              # all districts
"""
from __future__ import annotations

import argparse
import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

import psycopg2.extras
import requests

from app.data.connectors.base import (
    District, ensure_categories, fetch_all_districts, fetch_tag_mapping,
)
from app.data.postgres_writer import to_point_wkt, upsert_rows

logger = logging.getLogger(__name__)

OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]
UA = {"User-Agent": "SmartTourismAI/2.0 (university project)"}

NAME = "osm_listings"
CADENCE = "weekly"
REQUIRES_KEY = False
SCOPE = "per_district"


def _area_clause(district: District) -> str:
    """Relation-ID lookup, not name matching, when we have one.

    Found live 2026-09-02: Kurunegala District's name-based area query
    (`area["name"="Kurunegala District"]["admin_level"="5"]`) returned a
    genuinely empty area - not a mirror/rate-limit failure, confirmed by
    querying the bare area clause alone and getting zero elements back.
    Overpass's `area` index is a separately-maintained derived index that
    can lag or miss for a specific relation even when the relation itself
    resolves fine. `rel(<id>); map_to_area` bypasses that index entirely
    and go straight to the relation we already know exists (seeded in
    Phase 1, id stored as district.osm_relation_id) - confirmed fixed:
    the same query that returned 0 by name returned 23 hotels by relation
    id. Falls back to the name form only for a District built without one
    (e.g. in a unit test)."""
    if district.osm_relation_id:
        return f'rel({district.osm_relation_id});map_to_area->.searchArea;'
    return f'area["name"="{district.name}"]["admin_level"="5"]->.searchArea;'


def build_query(district: District) -> str:
    return f"""
    [out:json][timeout:180];
    {_area_clause(district)}
    (
      node["tourism"="hotel"](area.searchArea);
      node["tourism"="guest_house"](area.searchArea);
      node["tourism"="hostel"](area.searchArea);
      node["amenity"="restaurant"](area.searchArea);
      node["amenity"="cafe"](area.searchArea);
      node["tourism"="attraction"](area.searchArea);
      node["tourism"="viewpoint"](area.searchArea);
      node["tourism"="museum"](area.searchArea);
      node["historic"](area.searchArea);
      node["natural"="beach"](area.searchArea);
      node["natural"="waterfall"](area.searchArea);
    );
    out body;
    """


def build_transit_query(district: District) -> str:
    return f"""
    [out:json][timeout:180];
    {_area_clause(district)}
    (
      node["amenity"="bus_station"](area.searchArea);
      node["railway"="station"](area.searchArea);
      node["public_transport"="station"](area.searchArea);
    );
    out body;
    """


def _query_overpass(query: str, session: requests.Session) -> dict[str, Any]:
    """Rotates mirrors and retries the whole rotation on failure - the free
    Overpass instances routinely 429/504 (observed live 2026-09-02, same
    finding as seed_districts.py). Returns {} only after every mirror fails
    on every attempt."""
    for attempt in range(1, 3):
        for url in OVERPASS_MIRRORS:
            try:
                resp = session.post(url, data=query, timeout=200)
                if resp.status_code in (429, 502, 503, 504):
                    logger.warning("Overpass %s -> %s, trying next mirror", url, resp.status_code)
                    continue
                resp.raise_for_status()
                return resp.json()
            except requests.exceptions.RequestException as e:
                logger.warning("Overpass %s failed: %s", url, e)
        if attempt < 2:
            time.sleep(20)
    return {}


def haversine_km(lat1, lon1, lat2, lon2) -> float:
    import math
    R = 6371.0
    dlat, dlon = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _category_for(tags: dict[str, str]) -> str:
    if tags.get("tourism") in ("hotel", "guest_house", "hostel"):
        return "hotel"
    if tags.get("amenity") in ("restaurant", "cafe"):
        return "restaurant"
    return "attraction"


def _canonical_tags(osm_tags: dict[str, str], tag_map: dict[str, list[str]]) -> list[str]:
    """Maps every k=v pair on an OSM element that has an entry in
    tag_mapping onto this project's canonical tag_vocabulary, deduplicated."""
    matched: set[str] = set()
    for key, value in osm_tags.items():
        for canonical in tag_map.get(f"{key}={value}", []):
            matched.add(canonical)
    return sorted(matched)


class OSMListingsConnector:
    name = NAME
    cadence = CADENCE
    requires_key = REQUIRES_KEY
    scope = SCOPE

    def __init__(self) -> None:
        self._tag_map = fetch_tag_mapping("osm")

    async def fetch(self, district: Optional[District]) -> list[dict[str, Any]]:
        if district is None:
            raise ValueError("osm_listings is a per_district connector")

        session = requests.Session()
        session.headers.update(UA)

        listing_data = _query_overpass(build_query(district), session)
        elements = listing_data.get("elements", [])
        if not elements:
            return []

        time.sleep(2)
        transit_data = _query_overpass(build_transit_query(district), session)
        transit_elements = transit_data.get("elements", [])

        for el in elements:
            el["_transit_elements"] = transit_elements
        return elements

    def normalize(self, raw: list[dict[str, Any]], district: Optional[District]) -> list[dict[str, Any]]:
        if not raw:
            return []
        transit_elements = raw[0].get("_transit_elements", [])
        rows = []

        for el in raw:
            tags = el.get("tags", {})
            if "name" not in tags or "lat" not in el or "lon" not in el:
                continue

            lat, lon = el["lat"], el["lon"]
            has_transit, nearest_stop, min_dist = False, None, 1.0
            for t_el in transit_elements:
                if "lat" in t_el and "lon" in t_el:
                    d = haversine_km(lat, lon, t_el["lat"], t_el["lon"])
                    if d < min_dist:
                        min_dist, has_transit = d, True
                        nearest_stop = t_el.get("tags", {}).get("name", "Unnamed Station")

            rows.append({
                "district_id": district.id,
                "category": _category_for(tags),
                "name": tags["name"],
                "description": tags.get("description"),
                "lat": lat,
                "lon": lon,
                "tags": _canonical_tags(tags, self._tag_map),
                "opening_hours_raw": tags.get("opening_hours"),
                "has_public_transit": has_transit,
                "nearest_transit_stop": nearest_stop,
                "source": "osm",
                "external_ref": str(el["id"]),
            })
        return rows

    def upsert(self, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0
        category_ids = ensure_categories(["hotel", "restaurant", "attraction"])

        db_rows = []
        for r in rows:
            category_id = category_ids.get(r["category"])
            if category_id is None:
                continue
            db_rows.append({
                "district_id": r["district_id"],
                "category_id": category_id,
                "name": r["name"],
                "description": r["description"],
                "location": to_point_wkt(r["lat"], r["lon"]),
                "tags": r["tags"],
                # OSM's opening_hours syntax needs a real parser (the
                # `opening_hours` PyPI package) to become queryable structure -
                # out of scope for this connector. Stored as raw text under a
                # jsonb wrapper so it's not silently dropped, and is ready for
                # a parser to backfill later without a schema change.
                # psycopg2.extras.Json(...) is required here - upsert_rows'
                # placeholders bind everything as a plain parameter (except
                # geo_columns), and psycopg2 does not auto-adapt a Python
                # dict to jsonb without it.
                "opening_hours": (
                    psycopg2.extras.Json({"raw": r["opening_hours_raw"]})
                    if r["opening_hours_raw"] else None
                ),
                "has_public_transit": r["has_public_transit"],
                "nearest_transit_stop": r["nearest_transit_stop"],
                "source": r["source"],
                "external_ref": r["external_ref"],
                "is_verified": False,
                # A real value, not the literal string "now()" - upsert_rows
                # binds every column as a plain parameter (except geo_columns),
                # so a SQL function name here would bind as literal text and
                # fail against the timestamptz column.
                "last_seen_at": datetime.now(timezone.utc),
            })
        if not db_rows:
            return 0
        return upsert_rows(
            "travel_listing", db_rows, on_conflict=("source", "external_ref"), geo_columns={"location"},
        )


async def run_for_district(connector: OSMListingsConnector, district: District) -> int:
    raw = await connector.fetch(district)
    rows = connector.normalize(raw, district)
    return connector.upsert(rows)


async def run_all(district_filter: Optional[str] = None) -> None:
    connector = OSMListingsConnector()
    districts = fetch_all_districts()
    if district_filter:
        districts = [d for d in districts if district_filter.lower() in d.name.lower()]
    if not districts:
        print("[FATAL] No districts found - run app.data.seed_districts first.")
        return

    total = 0
    for i, d in enumerate(districts, 1):
        print(f"[{i}/{len(districts)}] {d.name} ...", end=" ", flush=True)
        count = await run_for_district(connector, d)
        total += count
        print(f"{count} listings")
        if i < len(districts):
            time.sleep(10)  # polite pause between districts

    print(f"\n[SUCCESS] Upserted {total} listings across {len(districts)} district(s).")


if __name__ == "__main__":
    import asyncio

    logging.basicConfig(level=logging.INFO)
    ap = argparse.ArgumentParser()
    ap.add_argument("--district", default=None, help="substring match, e.g. 'Kandy'")
    args = ap.parse_args()
    asyncio.run(run_all(args.district))
