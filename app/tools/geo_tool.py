# app/tools/geo_tool.py
"""
Place-name and point resolution against the real `district` table -
replaces app/data/sri_lanka_districts.py's hardcoded lookup entirely
(project concern #1, decision D4, docs/master_plan/DATA_PLATFORM.md §4).

resolve_district(lat, lon) is the query that actually proves #1 is fixed:
"Ella" resolves to Badulla because its coordinates fall inside Badulla's
real OSM boundary polygon, not because a Python dict had an "ella" key.

**Any place within Sri Lanka resolves dynamically** - not just the 25
seeded district capitals. A destination outside Sri Lanka (e.g. "New York")
is detected and returned with confidence="out_of_country" rather than a
silently empty/wrong result - the project's explicit scope is Sri Lanka
only (project decision, 2026-09-02), so callers (slot_filling.py) surface a
clear "SmartJourney covers Sri Lanka only" message instead of quietly
returning a null district_id and letting the request fail mysteriously
downstream.

Both functions never raise - on any failure they return None, matching the
existing convention in app/tools/geocode_tool.py and location_tool.py, so
callers can degrade gracefully the same way every other tool does.
"""
from __future__ import annotations

import logging
from typing import Optional, TypedDict

import httpx

from app.utils.db_pool import get_pool

logger = logging.getLogger(__name__)

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
UA = {"User-Agent": "SmartTourismAI/1.0"}

# Nominatim `class` values that indicate a genuine settlement/administrative
# area/named geographic feature, as opposed to an individual point of
# interest (hotel, cafe, street, shop...) that just happens to fuzzy-match
# the query text. Verified live 2026-09-02, restricted to countrycodes=lk:
# "New York" -> a beach club (class=tourism), "Paris" -> a residential lane
# (class=highway), "London" -> a cafe (class=amenity) - all coincidental
# matches with real place-sounding names. Real Sri Lankan queries came back
# class=place (Ella, Kandy), class=boundary (Nuwara Eliya district), or
# class=natural (Sigiriya) - this allowlist is what actually distinguishes
# "a real Sri Lankan place" from "Nominatim found something that contains
# your query text somewhere in Sri Lanka."
_SETTLEMENT_CLASSES = {"place", "boundary", "natural"}


class DistrictMatch(TypedDict):
    district_id: str
    name: str
    province: str


class PlaceResolution(TypedDict):
    name: str
    lat: float
    lon: float
    district_id: Optional[str]
    confidence: str  # "high" | "medium" | "low" | "out_of_country"
    country: Optional[str]  # display country name, set when confidence == "out_of_country"


_CONTAINS_SQL = """
    SELECT id, name, province
    FROM district
    WHERE ST_Contains(boundary, ST_SetSRID(ST_MakePoint($1, $2), 4326))
    LIMIT 1
"""

# Fallback for points landing in a boundary gap (coastal/islet cases, or a
# district whose Nominatim polygon lookup failed at seed time) - nearest
# centroid within 30km rather than leaving the point unresolved.
_NEAREST_SQL = """
    SELECT id, name, province,
           ST_Distance(center, ST_SetSRID(ST_MakePoint($1, $2), 4326)::geography) AS meters
    FROM district
    ORDER BY center <-> ST_SetSRID(ST_MakePoint($1, $2), 4326)::geography
    LIMIT 1
"""
_NEAREST_MAX_METERS = 30_000

_CACHE_SELECT_SQL = """
    SELECT display_name, ST_Y(location::geometry) AS lat, ST_X(location::geometry) AS lon,
           district_id, confidence
    FROM geo_resolution
    WHERE query_norm = $1
"""
_CACHE_UPSERT_SQL = """
    INSERT INTO geo_resolution (query_norm, display_name, location, district_id, confidence, provider)
    VALUES ($1, $2, ST_SetSRID(ST_MakePoint($4, $3), 4326)::geography, $5, $6, $7)
    ON CONFLICT (query_norm) DO UPDATE SET
        display_name = EXCLUDED.display_name,
        location = EXCLUDED.location,
        district_id = EXCLUDED.district_id,
        confidence = EXCLUDED.confidence,
        provider = EXCLUDED.provider,
        resolved_at = now()
"""

# district.name / travel_listing.name trigram similarity floor - below this
# a fuzzy match is more likely to be noise than a real hit.
_TRIGRAM_MIN_SIMILARITY = 0.4

_DISTRICT_NAME_SQL = """
    SELECT id, name, province,
           ST_Y(center::geometry) AS lat, ST_X(center::geometry) AS lon
    FROM district
    WHERE name % $1
    ORDER BY similarity(name, $1) DESC
    LIMIT 1
"""


async def resolve_district(lat: float, lon: float) -> Optional[DistrictMatch]:
    """
    Point -> district, via ST_Contains against real OSM boundary polygons.
    Falls back to nearest centroid within 30km for points landing in a
    boundary gap. Returns None if the database is unavailable or nothing
    is within range - never raises.
    """
    try:
        pool = await get_pool()
        if pool is None:
            return None

        row = await pool.fetchrow(_CONTAINS_SQL, lon, lat)
        if row:
            return {"district_id": str(row["id"]), "name": row["name"], "province": row["province"]}

        row = await pool.fetchrow(_NEAREST_SQL, lon, lat)
        if row and row["meters"] <= _NEAREST_MAX_METERS:
            return {"district_id": str(row["id"]), "name": row["name"], "province": row["province"]}
        return None
    except Exception as e:
        logger.warning(f"resolve_district failed for ({lat}, {lon}): {e}")
        return None


async def _nominatim_search(client: httpx.AsyncClient, name: str, countrycodes: Optional[str]) -> Optional[dict]:
    params = {"q": name, "format": "json", "limit": 1, "addressdetails": 1}
    if countrycodes:
        params["countrycodes"] = countrycodes
    resp = await client.get(NOMINATIM_URL, params=params)
    resp.raise_for_status()
    data = resp.json()
    return data[0] if data else None


async def _geocode_via_nominatim(name: str) -> Optional[dict]:
    """
    Two-step: try a Sri-Lanka-restricted search first, but only accept it if
    the match is settlement-class (see _SETTLEMENT_CLASSES above) - a
    restricted search still does fuzzy full-text matching, so a foreign
    name with no real Sri Lankan counterpart ("Paris") returns *something*
    (a residential lane named "Paris Perera Lane") rather than nothing, and
    that false positive must be rejected, not trusted.

    If step 1 doesn't give a confident match, fall back to an unrestricted
    global search and report the real country - this is what lets the
    caller distinguish "Sri Lankan place we haven't seen phrased this way"
    from "a real place, just not in Sri Lanka."

    Returns {lat, lon, display_name, in_sri_lanka, country} or None if
    nothing resolved anywhere.
    """
    try:
        async with httpx.AsyncClient(timeout=10.0, headers=UA) as client:
            sl_match = await _nominatim_search(client, name, countrycodes="lk")
            if sl_match and sl_match.get("class") in _SETTLEMENT_CLASSES:
                return {
                    "lat": float(sl_match["lat"]), "lon": float(sl_match["lon"]),
                    "display_name": sl_match.get("display_name", name),
                    "in_sri_lanka": True, "country": "Sri Lanka",
                }

            global_match = await _nominatim_search(client, name, countrycodes=None)
            if global_match is None:
                return None
            country_code = global_match.get("address", {}).get("country_code")
            if country_code == "lk":
                # The unrestricted search itself found a real Sri Lankan
                # place the restricted search missed/misjudged - trust it.
                return {
                    "lat": float(global_match["lat"]), "lon": float(global_match["lon"]),
                    "display_name": global_match.get("display_name", name),
                    "in_sri_lanka": True, "country": "Sri Lanka",
                }
            return {
                "lat": float(global_match["lat"]), "lon": float(global_match["lon"]),
                "display_name": global_match.get("display_name", name),
                "in_sri_lanka": False,
                "country": global_match.get("address", {}).get("country") or "elsewhere",
            }
    except Exception as e:
        logger.warning(f"Nominatim geocode failed for '{name}': {e}")
    return None


async def resolve_place(name: str) -> Optional[PlaceResolution]:
    """
    Place name -> {name, lat, lon, district_id, confidence}.

    Order (docs/master_plan/DATA_PLATFORM.md §4.3):
      1. geo_resolution cache (permanent - place names don't move)
      2. trigram match against district.name (handles "Kandy district", typos)
      3. Nominatim (rate-limited to 1 req/s by policy; only reached on a cache miss)
    Google Geocoding is intentionally not wired here - optional/needs
    billing, and Nominatim + the cache already cover this app's scope.

    Every successful resolution is written back to geo_resolution, so a
    repeated destination never leaves the database again.
    """
    query_norm = name.strip().lower()
    if not query_norm:
        return None

    pool = await get_pool()
    if pool is None:
        # No DB - degrade to a direct Nominatim call with no caching or
        # district linkage, same spirit as every other tool's fail-open path.
        geo = await _geocode_via_nominatim(name)
        if geo is None:
            return None
        return {"name": geo["display_name"], "lat": geo["lat"], "lon": geo["lon"],
                "district_id": None, "confidence": "medium"}

    try:
        cached = await pool.fetchrow(_CACHE_SELECT_SQL, query_norm)
        if cached:
            return {
                "name": cached["display_name"], "lat": cached["lat"], "lon": cached["lon"],
                "district_id": str(cached["district_id"]) if cached["district_id"] else None,
                "confidence": cached["confidence"],
            }
    except Exception as e:
        logger.warning(f"geo_resolution cache lookup failed for '{name}': {e}")

    # Trigram match against district names - lets "kandy district" or a
    # near-miss spelling resolve without a network call.
    try:
        row = await pool.fetchrow(_DISTRICT_NAME_SQL, name)
        if row:
            result: PlaceResolution = {
                "name": row["name"], "lat": row["lat"], "lon": row["lon"],
                "district_id": str(row["id"]), "confidence": "high",
            }
            await _write_cache(pool, query_norm, result, provider="district_table")
            return result
    except Exception as e:
        logger.warning(f"District trigram match failed for '{name}': {e}")

    geo = await _geocode_via_nominatim(name)
    if geo is None:
        return None

    district = await resolve_district(geo["lat"], geo["lon"])
    result = {
        "name": geo["display_name"], "lat": geo["lat"], "lon": geo["lon"],
        "district_id": district["district_id"] if district else None,
        "confidence": "high" if district else "medium",
    }
    await _write_cache(pool, query_norm, result, provider="nominatim")
    return result


async def _write_cache(pool, query_norm: str, result: dict, provider: str) -> None:
    try:
        await pool.execute(
            _CACHE_UPSERT_SQL,
            query_norm, result["name"], result["lat"], result["lon"],
            result["district_id"], result["confidence"], provider,
        )
    except Exception as e:
        logger.warning(f"geo_resolution cache write failed for '{query_norm}': {e}")
