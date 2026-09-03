# app/tools/routing_tool.py
"""
Real road travel times, backing the `travel_matrix` tool
(docs/master_plan/AGENT_ARCHITECTURE.md §4, DATA_PLATFORM.md §7).

Order: travel_time table cache (90-day TTL) -> OpenRouteService Matrix V2
(one many-to-many call, never per-pair - see the quota note below) ->
haversine + distance-banded speed estimate. Always returns something -
never raises.

Quota note: ORS Matrix V2's free tier is 500 requests/day, 40/minute - a
much smaller budget than Directions V2's 2,000/day (verified 2026-09-02,
docs/master_plan/API_SETUP.md §3.1). ONE matrix call covers every
origin-destination pair in a batch, so a trip plan costs ~1-2 calls, not
one per pair - callers MUST batch through get_travel_matrix() rather than
looping single-pair calls, or the daily quota is gone within an hour.
"""
from __future__ import annotations

import logging
import math
from typing import Optional, TypedDict

import httpx

from app.config.settings import settings
from app.utils.db_pool import get_pool

logger = logging.getLogger(__name__)

_TRAVEL_TIME_TTL_DAYS = 90


class Point(TypedDict):
    lat: float
    lon: float


def _round_key(p: Point) -> str:
    """4dp (~11m) rounding is what makes the cache actually hit - without
    it, GPS jitter produces a unique key every request."""
    return f"{round(p['lat'], 4)},{round(p['lon'], 4)}"


def haversine_km(a: Point, b: Point) -> float:
    R = 6371.0
    lat1, lon1, lat2, lon2 = a["lat"], a["lon"], b["lat"], b["lon"]
    dlat, dlon = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    x = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(x), math.sqrt(1 - x))


def _avg_kmh(straight_km: float) -> float:
    """Distance-banded, because one constant is wrong at both ends.

    Measured against real ORS 2026-09-02, Colombo->Kandy: actual 121 km /
    128 min (~57 km/h). A flat 32 km/h predicted 239 min - nearly 2x over,
    which would push proximity scores toward 0 on every inter-city leg and
    make the scorer over-prefer whatever is nearest, exactly the bias this
    factor exists to avoid. The 1.35 road-distance factor checked out fine
    (127.6 km predicted vs 121 km actual) and is kept as-is.

    Provisional bands - recalibrate in Phase 4 against more real ORS
    samples if scoring behavior suggests they're off (see
    docs/master_plan/API_SETUP.md §3.1.1)."""
    if straight_km < 5:
        return 18.0   # urban, congested
    if straight_km < 30:
        return 35.0   # suburban / secondary roads
    return 50.0        # inter-city A-roads


def haversine_minutes(a: Point, b: Point) -> float:
    straight = haversine_km(a, b)
    return (straight * 1.35) / _avg_kmh(straight) * 60.0


class MatrixResult(TypedDict):
    minutes: list[list[float]]
    km: list[list[float]]
    provider: str


async def _cache_lookup(origins: list[Point], destinations: list[Point]) -> dict[tuple[str, str], dict]:
    pool = await get_pool()
    if pool is None:
        return {}
    okeys = [_round_key(p) for p in origins]
    dkeys = [_round_key(p) for p in destinations]
    try:
        rows = await pool.fetch(
            "SELECT origin_key, dest_key, minutes, km, provider FROM travel_time "
            "WHERE origin_key = ANY($1) AND dest_key = ANY($2) AND mode = 'drive' "
            "AND fetched_at > now() - ($3 || ' days')::interval",
            okeys, dkeys, str(_TRAVEL_TIME_TTL_DAYS),
        )
        return {(r["origin_key"], r["dest_key"]): dict(r) for r in rows}
    except Exception as e:
        logger.warning(f"travel_time cache lookup failed: {e}")
        return {}


async def _cache_write(origin: Point, dest: Point, minutes: float, km: float, provider: str) -> None:
    pool = await get_pool()
    if pool is None:
        return
    try:
        await pool.execute(
            "INSERT INTO travel_time (origin_key, dest_key, mode, minutes, km, provider) "
            "VALUES ($1, $2, 'drive', $3, $4, $5) "
            "ON CONFLICT (origin_key, dest_key, mode) DO UPDATE SET "
            "minutes = EXCLUDED.minutes, km = EXCLUDED.km, provider = EXCLUDED.provider, fetched_at = now()",
            _round_key(origin), _round_key(dest), minutes, km, provider,
        )
    except Exception as e:
        logger.warning(f"travel_time cache write failed: {e}")


async def _ors_matrix(origins: list[Point], destinations: list[Point]) -> Optional[dict]:
    if not settings.ors_api_key:
        return None
    locations = [[p["lon"], p["lat"]] for p in origins] + [[p["lon"], p["lat"]] for p in destinations]
    n_origins = len(origins)
    sources = list(range(n_origins))
    dest_idx = list(range(n_origins, n_origins + len(destinations)))
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{settings.ors_base_url}/v2/matrix/driving-car",
                json={
                    "locations": locations, "sources": sources, "destinations": dest_idx,
                    "metrics": ["duration", "distance"],
                },
                headers={"Authorization": settings.ors_api_key, "Content-Type": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()
            return {
                "minutes": [[d / 60.0 for d in row] for row in data["durations"]],
                "km": [[d / 1000.0 for d in row] for row in data["distances"]],
            }
    except Exception as e:
        logger.warning(f"ORS matrix call failed: {e}")
        return None


async def get_travel_matrix(origins: list[Point], destinations: list[Point]) -> MatrixResult:
    """
    Returns {minutes, km, provider} for every origin x destination pair, in
    one call. This is the ONLY entry point that should reach ORS - never
    loop single-pair lookups, per the module docstring's quota note.

    provider is "cache" | "ors" | "haversine" - a per-matrix label, since a
    single call may mix cache hits with fresh ORS/haversine values but the
    overall matrix is reported by its weakest link for transparency.
    """
    if not origins or not destinations:
        return {"minutes": [], "km": [], "provider": "none"}

    cached = await _cache_lookup(origins, destinations)
    n_cached = len(cached)
    n_total = len(origins) * len(destinations)

    if n_cached == n_total:
        minutes = [[cached[(_round_key(o), _round_key(d))]["minutes"] for d in destinations] for o in origins]
        km = [[cached[(_round_key(o), _round_key(d))]["km"] for d in destinations] for o in origins]
        return {"minutes": minutes, "km": km, "provider": "cache"}

    ors_result = await _ors_matrix(origins, destinations)
    if ors_result is not None:
        for i, o in enumerate(origins):
            for j, d in enumerate(destinations):
                await _cache_write(o, d, ors_result["minutes"][i][j], ors_result["km"][i][j], "ors")
        return {"minutes": ors_result["minutes"], "km": ors_result["km"], "provider": "ors"}

    minutes, km = [], []
    for o in origins:
        m_row, k_row = [], []
        for d in destinations:
            key = (_round_key(o), _round_key(d))
            if key in cached:
                m_row.append(cached[key]["minutes"])
                k_row.append(cached[key]["km"])
            else:
                m = haversine_minutes(o, d)
                k = haversine_km(o, d) * 1.35
                m_row.append(m)
                k_row.append(k)
                await _cache_write(o, d, m, k, "haversine")
        minutes.append(m_row)
        km.append(k_row)
    return {"minutes": minutes, "km": km, "provider": "haversine"}
