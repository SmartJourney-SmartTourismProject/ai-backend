# tests/test_db_tool.py
# No real database or network - the asyncpg pool AND geo_tool.resolve_place
# are both faked. No mock DATA exists in db_tool.py anymore (decision D5,
# docs/master_plan/DATA_PLATFORM.md §9) - these tests protect the real
# contract instead: a legitimately empty query result returns [], while the
# database being unreachable raises DataUnavailable. Those two outcomes
# must never again look the same to a caller.

from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from app.tools import db_tool
from app.tools.db_tool import DataUnavailable

_KANDY_DISTRICT = {
    "name": "Kandy District", "lat": 7.29, "lon": 80.63,
    "district_id": "district-uuid-kandy", "confidence": "high", "country": "Sri Lanka",
}


class _FakePool:
    """Stands in for an asyncpg pool. `rows` is what fetch/fetchrow return;
    the SQL and its arguments are recorded so tests can assert on them."""

    def __init__(self, rows=None, raise_on_query: Exception = None):
        self._rows = rows if rows is not None else []
        self._raise = raise_on_query
        self.calls: list[tuple] = []

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        if self._raise:
            raise self._raise
        return list(self._rows)

    async def fetchrow(self, sql, *args):
        self.calls.append((sql, args))
        if self._raise:
            raise self._raise
        return self._rows[0] if self._rows else None


def _patch(monkeypatch, place=_KANDY_DISTRICT, pool=None, pool_error: Exception = None):
    """Patches both resolve_place (district resolution) and get_pool - every
    listings/events call now goes through both, in that order."""
    monkeypatch.setattr(db_tool, "resolve_place", AsyncMock(return_value=place))
    if pool_error is not None:
        monkeypatch.setattr(db_tool, "get_pool", AsyncMock(side_effect=pool_error))
    else:
        monkeypatch.setattr(db_tool, "get_pool", AsyncMock(return_value=pool))
    return pool


_REAL_LISTING_ROW = {
    "id": "listing-1", "name": "Real DB Hotel", "description": "A real listing",
    "tags": ["stay", "culture"], "price_level": 2, "price_per_night": 12000.0, "currency": "LKR",
    "latitude": 7.2906, "longitude": 80.6337, "rating": 4.5, "rating_count": 120,
    "photo_url": "https://example.com/photo.jpg", "opening_hours": {"raw": "24 hours"},
    "has_public_transit": True, "nearest_transit_stop": "Kandy Station",
}


# ---- destination resolution gate ------------------------------------------

async def test_get_hotels_unresolvable_destination_returns_empty_not_raises(monkeypatch):
    # Not a Sri Lankan place, or resolve_place found nothing at all - a
    # legitimate "no listings" answer, not a database problem.
    _patch(monkeypatch, place=None, pool=_FakePool([_REAL_LISTING_ROW]))
    hotels = await db_tool.get_hotels("Nowhereville")
    assert hotels == []


async def test_get_hotels_out_of_country_destination_returns_empty(monkeypatch):
    _patch(monkeypatch, place={
        "name": "New York, United States", "lat": 40.71, "lon": -74.0,
        "district_id": None, "confidence": "out_of_country", "country": "United States",
    }, pool=_FakePool([_REAL_LISTING_ROW]))
    hotels = await db_tool.get_hotels("New York")
    assert hotels == []


# ---- real listings query ---------------------------------------------------

async def test_get_hotels_maps_real_row_shape(monkeypatch):
    pool = _patch(monkeypatch, pool=_FakePool([_REAL_LISTING_ROW]))

    hotels = await db_tool.get_hotels("Kandy")

    assert len(hotels) == 1
    h = hotels[0]
    assert h["id"] == "listing-1"
    assert h["name"] == "Real DB Hotel"
    assert h["tags"] == ["stay", "culture"]
    assert h["price_level"] == 2
    assert h["price_per_night"] == 12000.0
    assert h["currency"] == "LKR"
    assert h["lat"] == 7.2906 and h["lon"] == 80.6337
    assert h["rating"] == 4.5
    assert h["rating_count"] == 120
    # old mock-era fields must be gone entirely
    assert "price_range" not in h
    assert "pickme_available" not in h

    # One JOIN query, parameterised by district_id + category.
    assert len(pool.calls) == 1
    _, args = pool.calls[0]
    assert args == ("district-uuid-kandy", "hotel")


async def test_get_hotels_with_interests_uses_tag_overlap_query(monkeypatch):
    pool = _patch(monkeypatch, pool=_FakePool([_REAL_LISTING_ROW]))

    await db_tool.get_hotels("Kandy", interests=["culture", "food"])

    sql, args = pool.calls[0]
    assert "tags &&" in sql
    assert args == ("district-uuid-kandy", "hotel", ["culture", "food"])


class _FakePoolSequence:
    """Returns a different row set on each successive fetch() call - needed
    to test the tag-filter-then-unfiltered-fallback path, where the same
    category is queried twice with different results."""

    def __init__(self, responses: list[list[dict]]):
        self._responses = list(responses)
        self.calls: list[tuple] = []

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        return self._responses.pop(0) if self._responses else []


async def test_get_hotels_interests_filter_falls_back_when_it_matches_nothing(monkeypatch):
    # Regression test for a real bug found live 2026-09-02: hotel rows are
    # only ever tagged ["stay"] (osm_listings' tag_mapping.csv has no other
    # mapping for tourism=hotel), so filtering hotels by interests like
    # ["nature","hiking"] matched zero rows every time - "plan a trip to
    # Ella, I like hiking" returned no hotels at all, failing the whole
    # request. Interest overlap is meant to be a soft preference (it's one
    # weighted factor in app/core/scoring.py's formula, not a hard
    # eliminator), so an empty filtered result must fall back to the
    # unfiltered category, not propagate as "no listings."
    monkeypatch.setattr(db_tool, "resolve_place", AsyncMock(return_value=_KANDY_DISTRICT))
    pool = _FakePoolSequence([[], [_REAL_LISTING_ROW]])   # filtered: empty, unfiltered: 1 row
    monkeypatch.setattr(db_tool, "get_pool", AsyncMock(return_value=pool))

    hotels = await db_tool.get_hotels("Kandy", interests=["nature", "hiking"])

    assert len(hotels) == 1
    assert hotels[0]["name"] == "Real DB Hotel"
    assert len(pool.calls) == 2
    assert "tags &&" in pool.calls[0][0]        # first call: filtered
    assert "tags &&" not in pool.calls[1][0]    # second call: unfiltered fallback


async def test_get_hotels_interests_filter_kept_when_it_matches_something(monkeypatch):
    # The fallback must not fire when the filter genuinely found results -
    # only a truly empty filtered result triggers the broaden-out.
    monkeypatch.setattr(db_tool, "resolve_place", AsyncMock(return_value=_KANDY_DISTRICT))
    pool = _FakePoolSequence([[_REAL_LISTING_ROW]])
    monkeypatch.setattr(db_tool, "get_pool", AsyncMock(return_value=pool))

    hotels = await db_tool.get_hotels("Kandy", interests=["culture"])

    assert len(hotels) == 1
    assert len(pool.calls) == 1   # no second, unfiltered call needed


async def test_get_hotels_empty_result_is_a_real_empty_answer_not_a_fallback(monkeypatch):
    # Database reachable, zero verified listings - the whole point of D5:
    # this must return [], never substitute mock data.
    _patch(monkeypatch, pool=_FakePool([]))
    hotels = await db_tool.get_hotels("Kandy")
    assert hotels == []


async def test_get_restaurants_and_attractions_use_their_own_category(monkeypatch):
    pool = _patch(monkeypatch, pool=_FakePool([_REAL_LISTING_ROW]))

    await db_tool.get_restaurants("Kandy")
    assert pool.calls[-1][1][1] == "restaurant"

    await db_tool.get_attractions("Kandy")
    assert pool.calls[-1][1][1] == "attraction"


# ---- DataUnavailable: the database itself failing --------------------------

async def test_get_hotels_no_pool_raises_data_unavailable(monkeypatch):
    _patch(monkeypatch, pool=None)   # get_pool() returns None - unconfigured/unreachable
    with pytest.raises(DataUnavailable):
        await db_tool.get_hotels("Kandy")


async def test_get_hotels_query_exception_raises_data_unavailable(monkeypatch):
    _patch(monkeypatch, pool_error=RuntimeError("connection refused"))
    with pytest.raises(DataUnavailable):
        await db_tool.get_hotels("Kandy")


async def test_get_hotels_query_failure_raises_data_unavailable(monkeypatch):
    pool = _FakePool(raise_on_query=RuntimeError("syntax error"))
    _patch(monkeypatch, pool=pool)
    with pytest.raises(DataUnavailable):
        await db_tool.get_hotels("Kandy")


# ---- events -----------------------------------------------------------------

_REAL_EVENT_ROW = {
    "id": "event-1", "name": "Kandy Esala Perahera", "description": "Festival",
    "start_datetime": "2026-08-20T19:00:00", "end_datetime": "2026-08-30T23:00:00",
    "venue_name": "Temple of the Tooth", "price_min": None, "price_max": None,
    "currency": "LKR", "tags": ["culture"],
}


async def test_get_events_maps_real_row_shape(monkeypatch):
    _patch(monkeypatch, pool=_FakePool([_REAL_EVENT_ROW]))

    events = await db_tool.get_events("Kandy", "2026-08-20", "2026-08-23")

    assert len(events) == 1
    assert events[0]["name"] == "Kandy Esala Perahera"
    assert events[0]["tags"] == ["culture"]
    assert "price_info" not in events[0]   # old mock-era field gone


async def test_get_events_unresolvable_destination_returns_empty(monkeypatch):
    _patch(monkeypatch, place=None, pool=_FakePool([_REAL_EVENT_ROW]))
    events = await db_tool.get_events("Nowhereville", "2026-08-20", "2026-08-23")
    assert events == []


async def test_get_events_binds_dates_as_datetimes_not_strings(monkeypatch):
    # Regression test. get_events' contract takes ISO strings, but the columns
    # are timestamptz and asyncpg refuses a str where it wants a datetime
    # ("invalid input for query argument $2") - unlike the Supabase REST layer,
    # which coerced silently. Caught only by running against a real database,
    # so pin it here.
    pool = _patch(monkeypatch, pool=_FakePool([]))

    await db_tool.get_events("Kandy", "2026-08-20", "2026-08-23")

    _, args = pool.calls[0]
    assert args[0] == "district-uuid-kandy"
    assert args[1] == datetime(2026, 8, 20)
    assert args[2] == datetime(2026, 8, 23)


async def test_get_events_tolerates_unparseable_dates(monkeypatch):
    pool = _patch(monkeypatch, pool=_FakePool([]))

    await db_tool.get_events("Kandy", "not-a-date", "2026-08-23")

    _, args = pool.calls[0]
    assert args[1] is None


async def test_get_events_no_pool_raises_data_unavailable(monkeypatch):
    _patch(monkeypatch, pool=None)
    with pytest.raises(DataUnavailable):
        await db_tool.get_events("Kandy", "2026-08-20", "2026-08-23")


# ---- user profile -----------------------------------------------------------

_DEFAULT_PROFILE = {"interests": [], "travel_style": None, "budget": None, "home_location": None}


async def test_get_user_profile_from_real_row(monkeypatch):
    # home_lat/home_lon come from ST_Y/ST_X in the query itself, so the row
    # already carries plain floats - no geometry decoding in Python.
    monkeypatch.setattr(db_tool, "get_pool", AsyncMock(return_value=_FakePool([{
        "travel_interests": ["culture", "food"], "travel_style": "budget",
        "default_budget": 300.0, "home_lat": 6.9271, "home_lon": 79.8612,
    }])))

    profile = await db_tool.get_user_profile("user-1")

    assert profile["interests"] == ["culture", "food"]
    assert profile["travel_style"] == "budget"
    assert profile["budget"] == 300.0
    assert profile["home_location"] == {"lat": 6.9271, "lon": 79.8612}


async def test_get_user_profile_with_no_home_location(monkeypatch):
    monkeypatch.setattr(db_tool, "get_pool", AsyncMock(return_value=_FakePool([{
        "travel_interests": None, "travel_style": None,
        "default_budget": None, "home_lat": None, "home_lon": None,
    }])))

    profile = await db_tool.get_user_profile("user-1")
    assert profile == _DEFAULT_PROFILE


async def test_get_user_profile_unknown_user_returns_defaults_not_an_error(monkeypatch):
    # No traveler_profile row yet is a legitimate, expected case (NestJS
    # creates it at registration) - defaults, not DataUnavailable. Database
    # unreachable IS DataUnavailable - see the test right below.
    monkeypatch.setattr(db_tool, "get_pool", AsyncMock(return_value=_FakePool([])))

    profile = await db_tool.get_user_profile("nobody")
    assert profile == _DEFAULT_PROFILE


async def test_get_user_profile_no_pool_raises_data_unavailable(monkeypatch):
    monkeypatch.setattr(db_tool, "get_pool", AsyncMock(return_value=None))
    with pytest.raises(DataUnavailable):
        await db_tool.get_user_profile("user-1")


async def test_get_user_profile_query_error_raises_data_unavailable(monkeypatch):
    monkeypatch.setattr(db_tool, "get_pool", AsyncMock(side_effect=RuntimeError("connection refused")))
    with pytest.raises(DataUnavailable):
        await db_tool.get_user_profile("user-1")


# ---- district_id-keyed search (Phase 6 ReAct tool contract) ----------------
# Unlike get_hotels/get_restaurants/get_attractions above, these take a
# district_id directly - no resolve_place() call, no destination string.

async def test_search_listings_by_district_returns_items_total_truncated(monkeypatch):
    pool = _FakePool([_REAL_LISTING_ROW])
    monkeypatch.setattr(db_tool, "get_pool", AsyncMock(return_value=pool))

    result = await db_tool.search_listings_by_district("district-uuid-kandy", "hotel", limit=40)

    assert result["total"] == 1
    assert result["truncated"] is False
    assert result["items"][0]["id"] == "listing-1"
    sql, args = pool.calls[0]
    assert args[:2] == ("district-uuid-kandy", "hotel")


async def test_search_listings_by_district_truncated_when_result_hits_limit(monkeypatch):
    pool = _FakePool([_REAL_LISTING_ROW, _REAL_LISTING_ROW])
    monkeypatch.setattr(db_tool, "get_pool", AsyncMock(return_value=pool))

    result = await db_tool.search_listings_by_district("district-uuid-kandy", "hotel", limit=2)
    assert result["truncated"] is True


async def test_search_listings_by_district_applies_tags_must_avoid_and_price_filters(monkeypatch):
    pool = _FakePool([])
    monkeypatch.setattr(db_tool, "get_pool", AsyncMock(return_value=pool))

    await db_tool.search_listings_by_district(
        "d1", "attraction", tags=["culture"], must_avoid=["hike"], max_price_level=2,
    )

    sql, args = pool.calls[0]
    assert "l.tags && $3" in sql
    assert "NOT (l.tags && $4)" in sql
    assert "l.price_level <= $5" in sql
    assert args == ("d1", "attraction", ["culture"], ["hike"], 2, 40)


async def test_search_listings_by_district_near_adds_st_dwithin(monkeypatch):
    pool = _FakePool([])
    monkeypatch.setattr(db_tool, "get_pool", AsyncMock(return_value=pool))

    await db_tool.search_listings_by_district(
        "d1", "attraction", near={"lat": 7.29, "lon": 80.63, "radius_km": 5.0},
    )

    sql, args = pool.calls[0]
    assert "ST_DWithin" in sql
    assert args == ("d1", "attraction", 7.29, 80.63, 5000.0, 40)


async def test_search_listings_by_district_no_pool_raises_data_unavailable(monkeypatch):
    monkeypatch.setattr(db_tool, "get_pool", AsyncMock(return_value=None))
    with pytest.raises(DataUnavailable):
        await db_tool.search_listings_by_district("d1", "hotel")


async def test_search_listings_by_district_query_error_raises_data_unavailable(monkeypatch):
    pool = _FakePool([], raise_on_query=RuntimeError("boom"))
    monkeypatch.setattr(db_tool, "get_pool", AsyncMock(return_value=pool))
    with pytest.raises(DataUnavailable):
        await db_tool.search_listings_by_district("d1", "hotel")


_DISTRICT_SEARCH_EVENT_ROW = {
    "id": "event-1", "name": "Kandy Perahera", "description": "Annual festival",
    "start_datetime": datetime(2026, 10, 1), "end_datetime": datetime(2026, 10, 3),
    "venue_name": "Temple of the Tooth", "price_min": 0.0, "price_max": None, "currency": "LKR",
    "tags": ["culture"],
}


async def test_search_events_by_district_returns_items(monkeypatch):
    pool = _FakePool([_DISTRICT_SEARCH_EVENT_ROW])
    monkeypatch.setattr(db_tool, "get_pool", AsyncMock(return_value=pool))

    result = await db_tool.search_events_by_district("d1", "2026-10-01", "2026-10-05")

    assert result["total"] == 1
    assert result["items"][0]["id"] == "event-1"


async def test_search_events_by_district_no_pool_raises_data_unavailable(monkeypatch):
    monkeypatch.setattr(db_tool, "get_pool", AsyncMock(return_value=None))
    with pytest.raises(DataUnavailable):
        await db_tool.search_events_by_district("d1", "2026-10-01", "2026-10-05")
