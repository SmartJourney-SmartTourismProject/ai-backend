# tests/test_geo_tool.py
# No real database or network - the asyncpg pool and Nominatim's HTTP
# responses are both faked, so these test OUR resolution logic (cache ->
# district trigram -> Nominatim -> resolve_district -> cache write), not
# PostgreSQL or Nominatim themselves. The live Ella -> Badulla gate is a
# manual verification step (docs/master_plan/DATA_PLATFORM.md §8), proven
# once against the real seeded database; these tests protect the code path
# that gate exercises.
#
# Nominatim fixtures below carry real "class"/"address.country_code" shape,
# not just lat/lon/display_name - _geocode_via_nominatim's settlement-class
# filter (geo_tool.py's module docstring) depends on them, and a fixture
# missing those fields would make every mocked response look like a
# no-match / out-of-country case regardless of what's being tested.

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


def _sl_settlement(name: str, lat: str, lon: str) -> dict:
    """A genuine Sri Lankan settlement match - class=place, as verified
    live for Ella/Kandy against countrycodes=lk."""
    return {"lat": lat, "lon": lon, "display_name": f"{name}, Sri Lanka",
            "class": "place", "type": "town", "address": {"country_code": "lk"}}


def _sl_fluke(name: str, lat: str, lon: str) -> dict:
    """A coincidental Sri-Lanka-restricted match on an unrelated business/
    street name - class is NOT in _SETTLEMENT_CLASSES, as verified live for
    "Paris" (class=highway, a residential lane) and "New York"
    (class=tourism, a beach club) restricted to countrycodes=lk."""
    return {"lat": lat, "lon": lon, "display_name": name,
            "class": "highway", "type": "residential", "address": {"country_code": "lk"}}


def _foreign(name: str, lat: str, lon: str, country: str, country_code: str) -> dict:
    return {"lat": lat, "lon": lon, "display_name": f"{name}, {country}",
            "class": "place", "type": "city",
            "address": {"country_code": country_code, "country": country}}


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


# ---- resolve_place: cache / trigram (no network) -------------------------

async def test_resolve_place_empty_string_returns_none():
    assert await resolve_place("   ") is None


async def test_resolve_place_cache_hit_skips_nominatim(monkeypatch):
    pool = _FakePool({
        "FROM geo_resolution": {
            "display_name": "Ella, Badulla District, Sri Lanka",
            "lat": 6.8736, "lon": 81.0490,
            "district_id": "district-uuid-1", "confidence": "high", "country": "Sri Lanka",
        },
    })
    monkeypatch.setattr(geo_tool, "get_pool", AsyncMock(return_value=pool))

    with respx.mock:
        # No route registered for NOMINATIM_URL - if the code tries to call
        # it despite the cache hit, respx raises and the test fails loudly.
        result = await resolve_place("Ella")

    assert result == {
        "name": "Ella, Badulla District, Sri Lanka", "lat": 6.8736, "lon": 81.0490,
        "district_id": "district-uuid-1", "confidence": "high", "country": "Sri Lanka",
    }


async def test_resolve_place_cache_hit_out_of_country(monkeypatch):
    pool = _FakePool({
        "FROM geo_resolution": {
            "display_name": "New York, United States",
            "lat": 40.71, "lon": -74.0,
            "district_id": None, "confidence": "out_of_country", "country": "United States",
        },
    })
    monkeypatch.setattr(geo_tool, "get_pool", AsyncMock(return_value=pool))

    with respx.mock:
        result = await resolve_place("New York")

    assert result["confidence"] == "out_of_country"
    assert result["country"] == "United States"
    assert result["district_id"] is None


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
    assert result["country"] == "Sri Lanka"
    # cache write happened
    assert any("INSERT INTO geo_resolution" in sql for sql, _ in pool.calls)


# ---- resolve_place: Nominatim, Sri Lankan matches -------------------------

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
        # Only ONE call expected: the countrycodes=lk search succeeds with a
        # settlement-class match, so the global fallback must never fire.
        respx.get(NOMINATIM_URL).mock(return_value=httpx.Response(
            200, json=[_sl_settlement("Ella", "6.8736", "81.0490")]
        ))
        result = await resolve_place("Ella")

    assert result == {
        "name": "Ella, Sri Lanka", "lat": 6.8736, "lon": 81.0490,
        "district_id": "district-uuid-1", "confidence": "high", "country": "Sri Lanka",
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
            200, json=[_sl_settlement("Somewhere", "1.0", "1.0")]
        ))
        result = await resolve_place("Somewhere")

    assert result["district_id"] is None
    assert result["confidence"] == "medium"
    assert result["country"] == "Sri Lanka"


async def test_resolve_place_no_database_falls_back_to_direct_nominatim(monkeypatch):
    monkeypatch.setattr(geo_tool, "get_pool", AsyncMock(return_value=None))

    with respx.mock:
        respx.get(NOMINATIM_URL).mock(return_value=httpx.Response(
            200, json=[_sl_settlement("Ella", "6.8736", "81.0490")]
        ))
        result = await resolve_place("Ella")

    assert result == {"name": "Ella, Sri Lanka", "lat": 6.8736, "lon": 81.0490,
                      "district_id": None, "confidence": "medium", "country": "Sri Lanka"}


async def test_resolve_place_everything_fails_returns_none(monkeypatch):
    pool = _FakePool({"FROM geo_resolution": None, "WHERE name %": None})
    monkeypatch.setattr(geo_tool, "get_pool", AsyncMock(return_value=pool))

    with respx.mock:
        # Both the restricted and the global fallback call fail.
        respx.get(NOMINATIM_URL).mock(return_value=httpx.Response(500))
        result = await resolve_place("Xyzzyplonkqqq123")

    assert result is None


# ---- resolve_place: out-of-country detection -----------------------------
# The concern this whole section protects: a country-restricted search
# still does fuzzy full-text matching, so it can return a coincidental
# match for a foreign name (verified live: "Paris" -> a residential lane in
# Sri Lanka named "Paris Perera Lane", class=highway). Accepting that match
# at face value would silently tell a user planning a Paris trip that
# SmartJourney found "Paris" nearby, in Sri Lanka. These tests exist to make
# that specific failure impossible.

async def test_resolve_place_detects_out_of_country_destination(monkeypatch):
    pool = _FakePool({"FROM geo_resolution": None, "WHERE name %": None})
    monkeypatch.setattr(geo_tool, "get_pool", AsyncMock(return_value=pool))

    with respx.mock:
        respx.get(NOMINATIM_URL).mock(side_effect=[
            # 1. countrycodes=lk search: no result at all for "New York"
            httpx.Response(200, json=[]),
            # 2. unrestricted global fallback: the real New York
            httpx.Response(200, json=[_foreign("New York", "40.71", "-74.0", "United States", "us")]),
        ])
        result = await resolve_place("New York")

    assert result["confidence"] == "out_of_country"
    assert result["country"] == "United States"
    assert result["district_id"] is None
    assert result["lat"] == 40.71 and result["lon"] == -74.0
    # the out-of-country detection must still be cached, so a repeat query
    # doesn't re-hit Nominatim
    assert any("INSERT INTO geo_resolution" in sql for sql, _ in pool.calls)


async def test_resolve_place_rejects_fluke_sri_lanka_match_for_foreign_name(monkeypatch):
    """The core regression case: the SL-restricted search finds SOMETHING
    (a fluke fuzzy match), but it must be rejected because its class isn't
    a settlement - the code must then fall through to the global search and
    report the real country, not accept the fluke as a Sri Lankan match."""
    pool = _FakePool({"FROM geo_resolution": None, "WHERE name %": None})
    monkeypatch.setattr(geo_tool, "get_pool", AsyncMock(return_value=pool))

    with respx.mock:
        respx.get(NOMINATIM_URL).mock(side_effect=[
            # 1. countrycodes=lk: a fluke match ("Paris Perera Lane"), class=highway
            httpx.Response(200, json=[_sl_fluke("Paris Perera 4 Lane", "7.05", "79.89")]),
            # 2. global fallback: the real Paris, France
            httpx.Response(200, json=[_foreign("Paris", "48.85", "2.35", "France", "fr")]),
        ])
        result = await resolve_place("Paris")

    assert result["confidence"] == "out_of_country"
    assert result["country"] == "France"
    assert result["name"] == "Paris, France"


async def test_resolve_place_global_search_can_still_find_sri_lanka(monkeypatch):
    """If the restricted search's top hit isn't settlement-class but the
    UNRESTRICTED global search independently resolves to Sri Lanka anyway,
    trust that - it's still a real match, just found the other way."""
    pool = _FakePool({
        "FROM geo_resolution": None, "WHERE name %": None,
        "ST_Contains": {"id": "d-1", "name": "Kandy District", "province": "Central Province"},
    })
    monkeypatch.setattr(geo_tool, "get_pool", AsyncMock(return_value=pool))

    with respx.mock:
        respx.get(NOMINATIM_URL).mock(side_effect=[
            httpx.Response(200, json=[_sl_fluke("Something Road", "7.0", "80.0")]),
            httpx.Response(200, json=[{
                "lat": "7.29", "lon": "80.63", "display_name": "Kandy, Sri Lanka",
                "class": "place", "type": "city", "address": {"country_code": "lk"},
            }]),
        ])
        result = await resolve_place("some obscure kandy phrasing")

    assert result["confidence"] == "high"
    assert result["country"] == "Sri Lanka"
    assert result["district_id"] == "d-1"


async def test_resolve_place_no_database_out_of_country(monkeypatch):
    monkeypatch.setattr(geo_tool, "get_pool", AsyncMock(return_value=None))

    with respx.mock:
        respx.get(NOMINATIM_URL).mock(side_effect=[
            httpx.Response(200, json=[]),
            httpx.Response(200, json=[_foreign("London", "51.5", "-0.13", "United Kingdom", "gb")]),
        ])
        result = await resolve_place("London")

    assert result["confidence"] == "out_of_country"
    assert result["country"] == "United Kingdom"
