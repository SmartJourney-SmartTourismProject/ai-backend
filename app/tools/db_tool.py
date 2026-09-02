"""
Real database access for hotels/restaurants/attractions/events/profiles -
no mock data anywhere in this module (project concern #3, decision D5,
docs/master_plan/DATA_PLATFORM.md §9).

"No fallbacks" means exactly two outcomes, never conflated:
  - the query legitimately matches nothing  -> []  (a real, honest answer)
  - the database could not be reached/queried -> DataUnavailable is raised
A plan built from mock data used to be indistinguishable, to the caller,
from a real one. That's the exact failure mode this module no longer has.

Public function names/signatures (get_hotels, get_restaurants, get_events,
get_user_profile) are kept as-is - app/workflows/recommendation_agent.py and
app/utils/slot_filling.py call them with a destination STRING, and rewiring
that calling convention to the district_id-based ReAct tool contract in
docs/master_plan/AGENT_ARCHITECTURE.md §4 is Phase 6's job (the ReAct agent
rewrite), not this phase's. What changed here is everything underneath:
real schema (tags/price_level, not the old price_range/pickme_available
columns that never existed on the real table), real district resolution via
geo_tool.resolve_place() instead of an ILIKE match against a raw destination
string that the real district.name ("Kandy District") would rarely match
anyway, and no mock data of any kind.
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from typing import List, Optional, Union

from app.tools.geo_tool import resolve_place
from app.utils.db_pool import get_pool

logger = logging.getLogger(__name__)


class DataUnavailable(Exception):
    """The database could not answer - unreachable, unconfigured, or the
    query itself failed. Never swallowed, never silently substituted with
    placeholder data. Distinct from a query that legitimately matched
    nothing, which returns [] normally, not this."""


# --- Queries -------------------------------------------------------------
# Raw parameterised SQL rather than an ORM: Prisma (the NestJS backend) owns
# the schema, so defining models here as well would duplicate that definition
# in a second language and let the two drift silently.
#
# `latitude`/`longitude` are generated columns derived from the PostGIS
# `location` geography (backend/db/migrations/0001_core.sql) - selecting
# them directly means no EWKB hex decoding here.

_SELECT_LISTINGS = """
    SELECT l.id, l.name, l.description, l.tags, l.price_level, l.price_per_night, l.currency,
           l.latitude, l.longitude, l.rating, l.rating_count, l.photo_url, l.opening_hours,
           l.has_public_transit, l.nearest_transit_stop
    FROM travel_listing l
    JOIN category c ON c.id = l.category_id
    WHERE l.district_id = $1
      AND c.name = $2
      AND l.is_verified = true
      AND l.is_active = true
"""
_SELECT_LISTINGS_WITH_TAGS = _SELECT_LISTINGS + " AND l.tags && $3"

_SELECT_EVENTS = """
    SELECT e.id, e.name, e.description, e.start_datetime, e.end_datetime,
           e.venue_name, e.price_min, e.price_max, e.currency, e.tags
    FROM local_event e
    WHERE e.district_id = $1
      AND e.is_verified = true
      AND e.start_datetime <= $3
      AND e.end_datetime   >= $2
"""

_SELECT_PROFILE = """
    SELECT travel_interests, travel_style, default_budget,
           ST_Y(home_location::geometry) AS home_lat,
           ST_X(home_location::geometry) AS home_lon
    FROM traveler_profile
    WHERE user_id = $1
"""


def _coerce_date(value: Union[str, date, datetime, None]):
    """
    get_events takes ISO date strings (that's its published contract, and what
    RecommendationAgent passes), but the columns are timestamptz and asyncpg
    will not coerce a string the way Supabase's REST layer did - it raises
    instead. Convert here so callers keep passing plain strings.
    """
    if value is None or isinstance(value, (date, datetime)):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        logger.warning(f"Unparseable date {value!r} in events query.")
        return None


def _row_to_listing_dict(row) -> dict:
    """Maps a real `travel_listing` row - the actual schema
    (backend/db/migrations/0001_core.sql), not the old mock shape."""
    return {
        "id": str(row["id"]),
        "name": row["name"],
        "description": row["description"],
        "tags": list(row["tags"] or []),
        "price_level": row["price_level"],          # 1-4, None if unknown - app/core/scoring.py's cost() handles None
        "price_per_night": float(row["price_per_night"]) if row["price_per_night"] is not None else None,
        "currency": row["currency"],
        "lat": row["latitude"],
        "lon": row["longitude"],
        "rating": float(row["rating"]) if row["rating"] is not None else None,
        "rating_count": row["rating_count"] or 0,
        "photo_url": row["photo_url"],
        "opening_hours": row["opening_hours"],
        "has_public_transit": row["has_public_transit"] or False,
        "nearest_transit_stop": row["nearest_transit_stop"],
    }


def _row_to_event_dict(row) -> dict:
    """Maps a real `local_event` row - price_min/price_max, not the old
    mock shape's single price_info blob."""
    return {
        "id": str(row["id"]),
        "name": row["name"],
        "description": row["description"],
        "start_datetime": row["start_datetime"],
        "end_datetime": row["end_datetime"],
        "venue_name": row["venue_name"],
        "price_min": float(row["price_min"]) if row["price_min"] is not None else None,
        "price_max": float(row["price_max"]) if row["price_max"] is not None else None,
        "currency": row["currency"],
        "tags": list(row["tags"] or []),
    }


async def _require_pool():
    """Every real query goes through this - get_pool() returning None
    (unconfigured or unreachable) is a DataUnavailable, not a silent
    fallback to something that looks like real data but isn't.

    get_pool() itself (app/utils/db_pool.py) is designed to never raise -
    it catches connection failures internally and returns None. This
    try/except exists anyway so db_tool's own promise ("never an uncaught
    raw exception, always a clear DataUnavailable") holds even if that
    contract ever changes, rather than depending on a caller remembering
    a guarantee made in a different file."""
    try:
        pool = await get_pool()
    except Exception as e:
        raise DataUnavailable(f"get_pool() failed unexpectedly: {e}") from e
    if pool is None:
        raise DataUnavailable(
            "Database is not configured or unreachable - no data source to query. "
            "Set DATABASE_URL and ensure PostgreSQL is running (see docs/master_plan/API_SETUP.md §2.2)."
        )
    return pool


# --- Listings (hotels / restaurants / attractions) ------------------------

async def _resolve_district_id(destination: str) -> Optional[str]:
    """destination is a free-text string ("Kandy", "Ella") from user input;
    the real schema keys listings on district_id. geo_tool.resolve_place()
    already handles this correctly for any place in Sri Lanka, not just the
    25 district capitals - see docs/master_plan/PROJECT_MASTER_PLAN.md D17.
    Returns None if the destination can't be resolved to a Sri Lankan
    district (out of scope, or genuinely not found) - callers treat that as
    "no listings", not a database failure."""
    place = await resolve_place(destination)
    if place is None or place.get("confidence") == "out_of_country":
        return None
    return place.get("district_id")


async def _get_listings(
    destination: str,
    category: str,
    interests: Optional[List[str]] = None,
) -> List[dict]:
    """Shared implementation for hotels/restaurants/attractions. Real query
    only - no mock fallback. `interests` filters by tag overlap (tags &&
    array) against the real tag_vocabulary-derived tags column, when given -
    but as a SOFT preference, not a hard elimination: if the filtered query
    returns nothing, this falls back to the unfiltered category result
    rather than returning [].

    Found live 2026-09-02: a hotel's tags are only ever ["stay"] (that's
    all osm_listings' tag_mapping.csv maps tourism=hotel to), so filtering
    hotels by tags=["nature","hiking"] correctly matches zero rows every
    time - there is no such thing as a hotel tagged "hiking". The project's
    own design (docs/master_plan/DETERMINISM_AND_VALIDATION.md §6.2) treats
    interest overlap as one weighted factor in a *scoring* formula
    (score = 0.5*pref + 0.3*prox + 0.2*rating), never a hard filter that can
    eliminate every candidate - app/core/scoring.py (Phase 4) is where that
    real weighting belongs. Until then, a hard SQL filter that can zero out
    an entire category defeats the actual intent and made a plain "plan a
    trip to Ella, I like hiking" request fail outright. Soft fallback here
    is the correctly-scoped interim fix - not a redesign of the ranking
    system, just not letting a filter override "return something real"."""
    district_id = await _resolve_district_id(destination)
    if district_id is None:
        return []   # legitimately no district to search in - not a DB failure

    pool = await _require_pool()
    try:
        if interests:
            rows = await pool.fetch(_SELECT_LISTINGS_WITH_TAGS, district_id, category, list(interests))
            if not rows:
                rows = await pool.fetch(_SELECT_LISTINGS, district_id, category)
        else:
            rows = await pool.fetch(_SELECT_LISTINGS, district_id, category)
    except Exception as e:
        raise DataUnavailable(f"Listings query failed for district_id={district_id}, category={category}: {e}") from e

    return [_row_to_listing_dict(r) for r in rows]


async def get_hotels(destination: str, interests: Optional[List[str]] = None) -> List[dict]:
    return await _get_listings(destination, "hotel", interests)


async def get_restaurants(destination: str, interests: Optional[List[str]] = None) -> List[dict]:
    return await _get_listings(destination, "restaurant", interests)


async def get_attractions(destination: str, interests: Optional[List[str]] = None) -> List[dict]:
    return await _get_listings(destination, "attraction", interests)


# --- Events ----------------------------------------------------------------

async def get_events(destination: str, start_date: str, end_date: str) -> List[dict]:
    """
    Returns events in destination whose window overlaps [start_date, end_date].
    Real query only. Empty result for "no district" or "no matching events"
    is a legitimate answer, not a failure.
    """
    district_id = await _resolve_district_id(destination)
    if district_id is None:
        return []

    pool = await _require_pool()
    try:
        rows = await pool.fetch(
            _SELECT_EVENTS, district_id,
            _coerce_date(start_date), _coerce_date(end_date),
        )
    except Exception as e:
        raise DataUnavailable(f"Events query failed for district_id={district_id}: {e}") from e

    return [_row_to_event_dict(r) for r in rows]


# --- District_id-keyed search (Phase 6 ReAct tool contract) ----------------
# AGENT_ARCHITECTURE.md §4's `db_search_listings`/`db_search_events`: unlike
# get_hotels/get_restaurants/get_attractions above (which take a destination
# STRING for the pre-Phase-6 single-shot agents), these take a district_id
# directly - the recommendation ReAct agent already has one, from the
# orchestrator agent's TripContext, and re-resolving a place name it already
# resolved would be wasted work and a second chance to disagree with itself.

_SELECT_LISTINGS_FULL = """
    SELECT l.id, l.name, l.description, l.tags, l.price_level, l.price_per_night, l.currency,
           l.latitude, l.longitude, l.rating, l.rating_count, l.photo_url, l.opening_hours,
           l.has_public_transit, l.nearest_transit_stop
    FROM travel_listing l
    JOIN category c ON c.id = l.category_id
    WHERE l.district_id = $1
      AND c.name = $2
      AND l.is_verified = true
      AND l.is_active = true
"""


def _build_search_listings_query(tags: list[str], must_avoid: list[str], max_price_level, near) -> tuple[str, list]:
    """Builds the query and its params AFTER district_id/category ($1/$2,
    supplied separately by the caller) - `params` here holds only the
    extra, conditional filter values, starting at $3."""
    sql = _SELECT_LISTINGS_FULL
    params: list = []
    next_idx = 3
    if tags:
        sql += f" AND l.tags && ${next_idx}"
        params.append(list(tags))
        next_idx += 1
    if must_avoid:
        sql += f" AND NOT (l.tags && ${next_idx})"
        params.append(list(must_avoid))
        next_idx += 1
    if max_price_level is not None:
        sql += f" AND (l.price_level IS NULL OR l.price_level <= ${next_idx})"
        params.append(max_price_level)
        next_idx += 1
    if near is not None:
        sql += (
            f" AND ST_DWithin(l.location, ST_SetSRID(ST_MakePoint(${next_idx + 1}, ${next_idx}), 4326)::geography, "
            f"${next_idx + 2})"
        )
        params.append(near["lat"])
        params.append(near["lon"])
        params.append((near.get("radius_km") or 20.0) * 1000.0)
        next_idx += 3
    sql += f" ORDER BY l.rating_count DESC NULLS LAST, l.id LIMIT ${next_idx}"
    return sql, params


async def search_listings_by_district(
    district_id: str,
    category: str,
    tags: Optional[List[str]] = None,
    must_avoid: Optional[List[str]] = None,
    max_price_level: Optional[int] = None,
    near: Optional[dict] = None,           # {"lat":..., "lon":..., "radius_km":...}
    radius_km: Optional[float] = None,
    limit: int = 40,
) -> dict:
    """`db_search_listings` (AGENT_ARCHITECTURE.md §4). Returns
    {items, total, truncated} - never []-vs-error conflated, same
    DataUnavailable convention as every other function in this module.
    `near`/`radius_km` are accepted separately per the documented tool
    signature; when both are given, near's own radius_km (if set) wins,
    otherwise the standalone radius_km param is used."""
    if near is not None and radius_km is not None and near.get("radius_km") is None:
        near = {**near, "radius_km": radius_km}

    sql, extra_params = _build_search_listings_query(tags or [], must_avoid or [], max_price_level, near)
    pool = await _require_pool()
    try:
        rows = await pool.fetch(sql, district_id, category, *extra_params, limit)
    except Exception as e:
        raise DataUnavailable(f"search_listings_by_district failed for district_id={district_id}, category={category}: {e}") from e

    items = [_row_to_listing_dict(r) for r in rows]
    return {"items": items, "total": len(items), "truncated": len(items) >= limit}


async def search_events_by_district(
    district_id: str,
    date_from: str,
    date_to: str,
    tags: Optional[List[str]] = None,
    limit: int = 20,
) -> dict:
    """`db_search_events` (AGENT_ARCHITECTURE.md §4). Overlap semantics
    identical to get_events above; tags is an additional soft filter (tag
    overlap), applied only when given."""
    pool = await _require_pool()
    sql = _SELECT_EVENTS
    params = [district_id, _coerce_date(date_from), _coerce_date(date_to)]
    if tags:
        sql += " AND e.tags && $4"
        params.append(list(tags))
    sql += f" ORDER BY e.start_datetime LIMIT ${len(params) + 1}"
    params.append(limit)
    try:
        rows = await pool.fetch(sql, *params)
    except Exception as e:
        raise DataUnavailable(f"search_events_by_district failed for district_id={district_id}: {e}") from e

    items = [_row_to_event_dict(r) for r in rows]
    return {"items": items, "total": len(items), "truncated": len(items) >= limit}


# --- User profile --------------------------------------------------------

def _row_to_profile_dict(row) -> dict:
    """Maps a real `traveler_profile` row onto the shape slot_filling.py's
    defaulting logic expects. home_lat/home_lon come from ST_Y/ST_X in
    _SELECT_PROFILE, so there's no geometry decoding here."""
    lat, lon = row["home_lat"], row["home_lon"]
    return {
        "interests": list(row["travel_interests"] or []),
        "travel_style": row["travel_style"],
        "budget": float(row["default_budget"]) if row["default_budget"] is not None else None,
        "home_location": {"lat": lat, "lon": lon} if lat is not None else None,
    }


_DEFAULT_PROFILE = {"interests": [], "travel_style": None, "budget": None, "home_location": None}


async def get_user_profile(user_id: str) -> dict:
    """
    Returns a traveler's saved preferences for slot_filling.py's defaulting
    logic. A user with no traveler_profile row yet is a legitimate, expected
    case (NestJS creates the row at registration; see
    backend/docs/BACKEND_PLAN.md §5.2) - returns the default dict, not a
    DataUnavailable. The database itself being unreachable IS a
    DataUnavailable - those are different situations.
    """
    pool = await _require_pool()
    try:
        row = await pool.fetchrow(_SELECT_PROFILE, user_id)
    except Exception as e:
        raise DataUnavailable(f"Profile query failed for user '{user_id}': {e}") from e

    if row is None:
        return dict(_DEFAULT_PROFILE)
    return _row_to_profile_dict(row)


# --- Data freshness (Phase 7, /trip-plan response contract) ----------------

_DATA_FRESHNESS_SQL = """
    SELECT
        CASE WHEN bool_or(last_success_at IS NULL) THEN NULL
             ELSE MIN(last_success_at) END AS oldest_sync
    FROM data_source
    WHERE is_enabled = true
"""


async def get_data_freshness() -> Optional[str]:
    """
    ISO timestamp of the OLDEST successful sync among enabled data sources -
    a conservative "as of" bound, not the newest. If any enabled source has
    never synced at all (last_success_at IS NULL), returns None rather than
    silently excluding it from the MIN() - reporting a freshness bound while
    ignoring a source that's never actually run would overstate how current
    the data really is, the same "no fallbacks, be honest about gaps"
    convention this module keeps everywhere else.

    Never raises - degrades to None (unknown) on any failure, same spirit
    as every other soft-signal tool in this codebase. Unlike get_hotels/etc,
    this one legitimately has no "real failure" distinction worth making to
    the caller: the response field is informational, not something a plan
    can be built or blocked on.
    """
    try:
        pool = await get_pool()
        if pool is None:
            return None
        row = await pool.fetchrow(_DATA_FRESHNESS_SQL)
    except Exception as e:
        logger.warning(f"get_data_freshness failed: {e}")
        return None
    if row is None or row["oldest_sync"] is None:
        return None
    return row["oldest_sync"].isoformat()
