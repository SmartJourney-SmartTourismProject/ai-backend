# tests/test_geo_tool.py
# No real database or network - the asyncpg pool and Nominatim's HTTP
# responses are both faked, so these test OUR resolution logic (cache ->
# district trigram -> Nominatim -> resolve_district -> cache write), not
# PostgreSQL or Nominatim themselves. The live Ella -> Badulla gate is a
# manual verification step (docs/master_plan/DATA_PLATFORM.md §8), proven
# once against the real seeded database; these tests protect the code path
# that gate exercises.

from unittest.mock import AsyncMock

import httpx
import respx

from app.tools import geo_tool
from app.tools.geo_tool import resolve_district, resolve_place, NOMINATIM_URL


class _FakePool:
    """Dispatches fetchrow/execute by matching a substring in the SQL - lets
    each test configure different responses for the cache lookup, the
    district-name trigram match, and the point-in-polygon query without
    needing a real query planner."""

    def __init__(self, responses: dict[str, dict | None] = None):
        self._responses = responses or {}
        self.calls: list[tuple] = []

    def _match(self, sql: str):
        for key, value in self._responses.items():
            if key in sql:
                return value
        return None

    async def fetchrow(self, sql, *args):
        self.calls.append((sql, args))
        return self._match(sql)

    async def execute(self, sql, *args):
        self.calls.append((sql, args))


def _patch_pool(monkeypatch, responses=None) -> _FakePool:
    pool = _FakePool(responses)
    monkeypatch.setattr(geo_tool, "get_pool", AsyncMock(return_value=pool))
    return pool


# ---- resolve_district -------------------------------------------------

async def test_resolve_district_no_database_returns_none(monkeypatch):
    monkeypatch.setattr(geo_tool, "get_pool", AsyncMock(return_value=None))
    result = await resolve_district(6.8658, 81.0467)
    assert result is None


async def test_resolve_district_contains_hit(monkeypatch):
    # ST_Contains query matches first - the real Ella->Badulla gate's shape.
    pool = _FakePool({
        "ST_Contains": {"id": "district-uuid-1", "name": "Badulla District", "province": "Uva Province"},
    })
    monkeypatch.setattr(geo_tool, "get_pool", AsyncMock(return_value=pool))

    result = await resolve_district(6.8658, 81.0467)
    assert result == {"district_id": "district-uuid-1", "name": "Badulla District", "province": "Uva Province"}


async def test_resolve_district_falls_back_to_nearest_within_range(monkeypatch):
    # ST_Contains finds nothing (coastal gap); nearest-centroid fallback
    # matches within the 30km cutoff.
    pool = _FakePool({
        "ORDER BY center <->": {
            "id": "district-uuid-2", "name": "Galle District", "province": "Southern Province",
            "meters": 12_000.0,
        },
    })
    monkeypatch.setattr(geo_tool, "get_pool", AsyncMock(return_value=pool))

    result = await resolve_district(6.0, 80.2)
    assert result == {"district_id": "district-uuid-2", "name": "Galle District", "province": "Southern Province"}


async def test_resolve_district_nearest_too_far_returns_none(monkeypatch):
    pool = _FakePool({
        "ORDER BY center <->": {
            "id": "district-uuid-3", "name": "Somewhere District", "province": "Nowhere Province",
            "meters": 50_000.0,  # beyond the 30km cutoff
        },
    })
    monkeypatch.setattr(geo_tool, "get_pool", AsyncMock(return_value=pool))

    result = await resolve_district(0.0, 0.0)
    assert result is None


async def test_resolve_district_db_error_returns_none(monkeypatch):
    monkeypatch.setattr(geo_tool, "get_pool", AsyncMock(side_effect=RuntimeError("connection refused")))
    result = await resolve_district(6.8658, 81.0467)
    assert result is None


# ---- resolve_place ------------------------------------------------------

async def test_resolve_place_empty_string_returns_none():
    assert await resolve_place("   ") is None


async def test_resolve_place_cache_hit_skips_nominatim(monkeypatch):
    pool = _FakePool({
        "FROM geo_resolution": {
            "display_name": "Ella, Badulla District, Sri Lanka",
            "lat": 6.8736, "lon": 81.0490,
            "district_id": "district-uuid-1", "confidence": "high",
        },
    })
    monkeypatch.setattr(geo_tool, "get_pool", AsyncMock(return_value=pool))

    with respx.mock:
        # No route registered for NOMINATIM_URL - if the code tries to call
        # it despite the cache hit, respx raises and the test fails loudly.
        result = await resolve_place("Ella")

    assert result == {
        "name": "Ella, Badulla District, Sri Lanka", "lat": 6.8736, "lon": 81.0490,
        "district_id": "district-uuid-1", "confidence": "high",
    }


async def test_resolve_place_district_trigram_match_skips_nominatim(monkeypatch):
    pool = _FakePool({
        "FROM geo_resolution": None,  # cache miss
        "WHERE name %": {
            "id": "district-uuid-4", "name": "Kandy District", "province": "Central Province",
            "lat": 7.29, "lon": 80.63,
        },
    })
    monkeypatch.setattr(geo_tool, "get_pool", AsyncMock(return_value=pool))

    with respx.mock:
        result = await resolve_place("kandy")

    assert result["name"] == "Kandy District"
    assert result["district_id"] == "district-uuid-4"
    assert result["confidence"] == "high"
    # cache write happened
    assert any("INSERT INTO geo_resolution" in sql for sql, _ in pool.calls)


async def test_resolve_place_falls_through_to_nominatim_then_resolves_district(monkeypatch):
    pool = _FakePool({
        "FROM geo_resolution": None,        # cache miss
        "WHERE name %": None,               # no district name match ("Ella" isn't a district)
        "ST_Contains": {                    # resolve_district's own lookup, called after geocoding
            "id": "district-uuid-1", "name": "Badulla District", "province": "Uva Province",
        },
    })
    monkeypatch.setattr(geo_tool, "get_pool", AsyncMock(return_value=pool))

    with respx.mock:
        respx.get(NOMINATIM_URL).mock(return_value=httpx.Response(
            200, json=[{"lat": "6.8736", "lon": "81.0490", "display_name": "Ella, Sri Lanka"}]
        ))
        result = await resolve_place("Ella")

    assert result == {
        "name": "Ella, Sri Lanka", "lat": 6.8736, "lon": 81.0490,
        "district_id": "district-uuid-1", "confidence": "high",
    }


async def test_resolve_place_nominatim_hit_but_no_district_match_is_medium_confidence(monkeypatch):
    pool = _FakePool({
        "FROM geo_resolution": None,
        "WHERE name %": None,
        "ST_Contains": None,
        "ORDER BY center <->": None,  # resolve_district's nearest-fallback also misses
    })
    monkeypatch.setattr(geo_tool, "get_pool", AsyncMock(return_value=pool))

    with respx.mock:
        respx.get(NOMINATIM_URL).mock(return_value=httpx.Response(
            200, json=[{"lat": "1.0", "lon": "1.0", "display_name": "Somewhere, Elsewhere"}]
        ))
        result = await resolve_place("Somewhere")

    assert result["district_id"] is None
    assert result["confidence"] == "medium"


async def test_resolve_place_no_database_falls_back_to_direct_nominatim(monkeypatch):
    monkeypatch.setattr(geo_tool, "get_pool", AsyncMock(return_value=None))

    with respx.mock:
        respx.get(NOMINATIM_URL).mock(return_value=httpx.Response(
            200, json=[{"lat": "6.8736", "lon": "81.0490", "display_name": "Ella, Sri Lanka"}]
        ))
        result = await resolve_place("Ella")

    assert result == {"name": "Ella, Sri Lanka", "lat": 6.8736, "lon": 81.0490,
                      "district_id": None, "confidence": "medium"}


async def test_resolve_place_everything_fails_returns_none(monkeypatch):
    pool = _FakePool({"FROM geo_resolution": None, "WHERE name %": None})
    monkeypatch.setattr(geo_tool, "get_pool", AsyncMock(return_value=pool))

    with respx.mock:
        respx.get(NOMINATIM_URL).mock(return_value=httpx.Response(500))
        result = await resolve_place("Xyzzyplonkqqq123")

    assert result is None
