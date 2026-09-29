"""
Seeds the `district` table with Sri Lanka's 25 administrative districts,
including real boundary polygons - the replacement for the deleted
app/data/sri_lanka_districts.py hardcoded list (project concern #1, decision
D4, docs/master_plan/DATA_PLATFORM.md §4).

Two live sources, combined:
  1. Overpass  - enumerates the 25 admin_level=5 relations inside Sri Lanka
                 and gives each one's real OSM relation id (provenance).
  2. Nominatim - for each district name, returns its province (via
                 addressdetails) and a ready-assembled boundary polygon
                 (via polygon_geojson) - reliable, ring-correct geometry,
                 unlike hand-assembling Overpass relation members into rings.

Run once (then quarterly, per the plan's cadence table):

    python -m app.data.seed_districts

Idempotent - upserts on osm_relation_id, so re-running just refreshes
boundaries rather than duplicating rows.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Optional

import requests

from app.data.postgres_writer import get_connection

logger = logging.getLogger(__name__)

OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
UA = {"User-Agent": "SmartTourismAI/1.0 (university project, district seed)"}

_DISTRICTS_QUERY = """
[out:json][timeout:180];
area["ISO3166-1"="LK"][admin_level=2]->.lk;
relation["admin_level"="5"]["boundary"="administrative"](area.lk);
out tags;
"""


def fetch_district_relations() -> list[dict[str, Any]]:
    """Enumerates the 25 districts with their real OSM relation ids -
    rotating mirrors and retrying the whole rotation on failure, since the
    free Overpass instances are shared and routinely return 429/504
    (observed live 2026-09-02 - see docs/master_plan/DATA_PLATFORM.md §5.1)."""
    for attempt in range(1, 4):
        for url in OVERPASS_MIRRORS:
            try:
                resp = requests.post(url, data=_DISTRICTS_QUERY, headers=UA, timeout=200)
                if resp.status_code in (429, 502, 503, 504):
                    logger.warning("Overpass %s returned %s, trying next mirror", url, resp.status_code)
                    continue
                resp.raise_for_status()
                elements = resp.json().get("elements", [])
                if elements:
                    return elements
            except requests.exceptions.RequestException as e:
                logger.warning("Overpass %s failed: %s", url, e)
        if attempt < 3:
            time.sleep(30 * attempt)
    raise RuntimeError("All Overpass mirrors failed after 3 rotations - see logs above.")


def fetch_nominatim_district(name: str) -> Optional[dict[str, Any]]:
    """Returns {lat, lon, province, geojson} for a district name, or None on
    any failure. Nominatim's own polygon_geojson output is already assembled
    into valid closed rings - far more reliable than stitching Overpass
    relation member ways into a MultiPolygon by hand."""
    try:
        resp = requests.get(
            NOMINATIM_URL,
            params={
                "q": f"{name}, Sri Lanka",
                "format": "json",
                "polygon_geojson": 1,
                "addressdetails": 1,
                "limit": 1,
            },
            headers=UA,
            timeout=30,
        )
        resp.raise_for_status()
        results = resp.json()
        if not results:
            logger.warning("Nominatim returned no match for '%s'", name)
            return None
        item = results[0]
        return {
            "lat": float(item["lat"]),
            "lon": float(item["lon"]),
            "province": item.get("address", {}).get("state"),
            "geojson": item.get("geojson"),
        }
    except requests.exceptions.RequestException as e:
        logger.warning("Nominatim lookup failed for '%s': %s", name, e)
        return None


def _geojson_to_multipolygon_wkt(geojson: dict) -> Optional[str]:
    """Normalizes a Polygon or MultiPolygon GeoJSON geometry into
    MULTIPOLYGON WKT, since district.boundary is typed geometry(MultiPolygon,4326)
    but Nominatim returns a plain Polygon for most (non-archipelago) districts."""
    gtype = geojson.get("type")
    coords = geojson.get("coordinates")

    def ring_wkt(ring: list[list[float]]) -> str:
        return "(" + ",".join(f"{lon} {lat}" for lon, lat in ring) + ")"

    def polygon_wkt(rings: list[list[list[float]]]) -> str:
        return "(" + ",".join(ring_wkt(r) for r in rings) + ")"

    if gtype == "Polygon":
        return f"MULTIPOLYGON({polygon_wkt(coords)})"
    if gtype == "MultiPolygon":
        return f"MULTIPOLYGON({','.join(polygon_wkt(p) for p in coords)})"
    logger.warning("Unexpected geometry type from Nominatim: %s", gtype)
    return None


_UPSERT = """
    INSERT INTO district (name, province, osm_relation_id, center, boundary, source)
    VALUES (
        %(name)s, %(province)s, %(osm_relation_id)s,
        ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326)::geography,
        ST_SimplifyPreserveTopology(
            ST_SetSRID(ST_GeomFromText(%(boundary_wkt)s), 4326), 0.0005
        ),
        'osm'
    )
    ON CONFLICT (osm_relation_id) DO UPDATE SET
        name = EXCLUDED.name,
        province = EXCLUDED.province,
        center = EXCLUDED.center,
        boundary = EXCLUDED.boundary,
        updated_at = now()
"""


def run_seed() -> int:
    print("--- SEEDING district table (Overpass relations + Nominatim boundaries) ---")

    relations = fetch_district_relations()
    print(f"Found {len(relations)} admin_level=5 relations in Sri Lanka.")

    conn = get_connection()
    if conn is None:
        print("[FATAL] DATABASE_URL not configured or database unreachable - nothing to seed into.")
        return 0

    seeded = 0
    try:
        with conn, conn.cursor() as cur:
            for i, rel in enumerate(relations, 1):
                tags = rel.get("tags", {})
                name = tags.get("name:en") or tags.get("name")
                osm_id = rel["id"]
                if not name:
                    print(f"  [!] Relation {osm_id} has no name tag - skipped.")
                    continue

                print(f"  [{i}/{len(relations)}] {name} (osm={osm_id}) ...", end=" ")
                nom = fetch_nominatim_district(name)
                time.sleep(1.1)  # Nominatim's usage policy: max 1 req/s

                if nom is None or nom["geojson"] is None:
                    print("SKIPPED (no Nominatim match/geometry)")
                    continue

                boundary_wkt = _geojson_to_multipolygon_wkt(nom["geojson"])
                if boundary_wkt is None:
                    print("SKIPPED (unusable geometry)")
                    continue

                cur.execute(_UPSERT, {
                    "name": name,
                    "province": nom["province"] or "Unknown",
                    "osm_relation_id": osm_id,
                    "lat": nom["lat"],
                    "lon": nom["lon"],
                    "boundary_wkt": boundary_wkt,
                })
                seeded += 1
                print(f"OK ({nom['province']})")
    finally:
        conn.close()

    print(f"\n[SUCCESS] Seeded/updated {seeded}/{len(relations)} districts.")
    return seeded


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_seed()
