# tests/test_db_tool.py
# No real database - the asyncpg pool is faked, so these test OUR query
# handling and mock-data fallbacks rather than PostgreSQL itself.
from datetime import datetime
from unittest.mock import AsyncMock

from app.tools import db_tool


async def test_get_hotels_shape():
    hotels = await db_tool.get_hotels("Kandy")
    assert isinstance(hotels, list)
    assert len(hotels) > 0
    h = hotels[0]
    assert "id" in h and "name" in h and "lat" in h and "lon" in h


async def test_get_hotels_unknown_destination_returns_empty_not_raises():
    hotels = await db_tool.get_hotels("Nowhereville")
    assert hotels == []


async def test_get_user_profile_without_database_configured_returns_defaults(monkeypatch):
    # DATABASE_URL isn't set in this test environment, so get_pool() returns
    # None - the same path a fresh clone with no .env takes.
    monkeypatch.setattr(db_tool, "get_pool", AsyncMock(return_value=None))

    profile = await db_tool.get_user_profile("any-user-id")

    assert profile == {"interests": [], "travel_style": None, "budget": None, "home_location": None}


class _FakePool:
    """Stands in for an asyncpg pool. `rows` is what fetch/fetchrow return;
    the SQL and its arguments are recorded so tests can assert on them."""

    def __init__(self, rows=None):
        self._rows = rows if rows is not None else []
        self.calls: list[tuple] = []

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        return list(self._rows)

    async def fetchrow(self, sql, *args):
        self.calls.append((sql, args))
        return self._rows[0] if self._rows else None


def _patch_pool(monkeypatch, rows=None) -> _FakePool:
    pool = _FakePool(rows)
    monkeypatch.setattr(db_tool, "get_pool", AsyncMock(return_value=pool))
    return pool


async def test_get_user_profile_from_real_row(monkeypatch):
    # home_lat/home_lon come from ST_Y/ST_X in the query itself, so the row
    # already carries plain floats - no geometry decoding in Python.
    _patch_pool(monkeypatch, [{
        "travel_interests": ["culture", "food"],
        "travel_style": "budget",
        "default_budget": 300.0,
        "home_lat": 6.9271,
        "home_lon": 79.8612,
    }])

    profile = await db_tool.get_user_profile("user-1")

    assert profile["interests"] == ["culture", "food"]
    assert profile["travel_style"] == "budget"
    assert profile["budget"] == 300.0
    assert profile["home_location"] == {"lat": 6.9271, "lon": 79.8612}


async def test_get_user_profile_with_no_home_location(monkeypatch):
    _patch_pool(monkeypatch, [{
        "travel_interests": None, "travel_style": None,
        "default_budget": None, "home_lat": None, "home_lon": None,
    }])

    profile = await db_tool.get_user_profile("user-1")

    assert profile == {"interests": [], "travel_style": None, "budget": None, "home_location": None}


async def test_get_user_profile_unknown_user_returns_defaults(monkeypatch):
    _patch_pool(monkeypatch, [])

    profile = await db_tool.get_user_profile("nobody")

    assert profile == {"interests": [], "travel_style": None, "budget": None, "home_location": None}


async def test_get_user_profile_db_error_falls_back_to_defaults(monkeypatch):
    monkeypatch.setattr(db_tool, "get_pool", AsyncMock(side_effect=RuntimeError("connection refused")))

    profile = await db_tool.get_user_profile("user-1")

    assert profile == {"interests": [], "travel_style": None, "budget": None, "home_location": None}


async def test_get_hotels_from_real_database(monkeypatch):
    # latitude/longitude are generated columns on travel_listing, so they
    # arrive as plain floats.
    pool = _patch_pool(monkeypatch, [{
        "id": "listing-1", "name": "Real DB Hotel", "description": "A real listing",
        "price_range": "$$", "latitude": 7.2906, "longitude": 80.6337,
        "rating": 4.5, "photo_url": "https://example.com/photo.jpg",
        "opening_hours": "24 hours", "has_public_transit": True,
        "nearest_transit_stop": "Kandy Station", "pickme_available": True,
    }])

    hotels = await db_tool.get_hotels("Kandy")

    assert len(hotels) == 1
    assert hotels[0]["name"] == "Real DB Hotel"
    assert hotels[0]["lat"] == 7.2906
    assert hotels[0]["lon"] == 80.6337
    # One JOIN query, parameterised by destination + category - not three
    # round trips resolving district/category ids first.
    assert len(pool.calls) == 1
    assert pool.calls[0][1] == ("Kandy", "hotel")


async def test_get_hotels_db_error_falls_back_to_mock_data(monkeypatch):
    monkeypatch.setattr(db_tool, "get_pool", AsyncMock(side_effect=RuntimeError("connection refused")))

    hotels = await db_tool.get_hotels("Kandy")

    # Falls through to the same mock data test_get_hotels_shape checks.
    assert len(hotels) > 0
    assert all("id" in h and "name" in h for h in hotels)


async def test_get_hotels_empty_result_falls_back_to_mock_data(monkeypatch):
    # Database reachable, but zero verified listings - a legitimately empty
    # real result, not an error.
    _patch_pool(monkeypatch, [])

    hotels = await db_tool.get_hotels("Kandy")

    assert len(hotels) > 0  # mock fallback, not an empty list


async def test_get_events_from_real_database(monkeypatch):
    pool = _patch_pool(monkeypatch, [{
        "id": "event-1", "name": "Kandy Esala Perahera", "description": "Festival",
        "start_datetime": "2026-08-20T19:00:00", "end_datetime": "2026-08-30T23:00:00",
        "venue_name": "Temple of the Tooth", "price_info": {"free": True},
    }])

    events = await db_tool.get_events("Kandy", "2026-08-20", "2026-08-23")

    assert len(events) == 1
    assert events[0]["name"] == "Kandy Esala Perahera"


async def test_get_events_binds_dates_as_datetimes_not_strings(monkeypatch):
    # Regression test. get_events' contract takes ISO strings, but the columns
    # are timestamptz and asyncpg refuses a str where it wants a datetime
    # ("invalid input for query argument $2") - unlike the Supabase REST layer,
    # which coerced silently. Caught only by running against a real database,
    # so pin it here.
    pool = _patch_pool(monkeypatch, [])

    await db_tool.get_events("Kandy", "2026-08-20", "2026-08-23")

    _, args = pool.calls[0]
    assert args[0] == "Kandy"
    assert args[1] == datetime(2026, 8, 20)
    assert args[2] == datetime(2026, 8, 23)


async def test_get_events_tolerates_unparseable_dates(monkeypatch):
    pool = _patch_pool(monkeypatch, [])

    await db_tool.get_events("Kandy", "not-a-date", "2026-08-23")

    _, args = pool.calls[0]
    assert args[1] is None


async def test_mock_events_use_the_name_key_like_real_rows(monkeypatch):
    # Mock events used to key the title as "title" while real rows use "name"
    # (BUILD_PLAN §4's contract), so RecommendationAgent silently saw a
    # different shape depending on whether the database was up.
    monkeypatch.setattr(db_tool, "get_pool", AsyncMock(return_value=None))

    events = await db_tool.get_events("Kandy", "2026-08-20", "2026-08-23")

    assert len(events) > 0
    assert all("name" in e and "title" not in e for e in events)


async def test_get_events_db_error_falls_back_to_mock_data(monkeypatch):
    monkeypatch.setattr(db_tool, "get_pool", AsyncMock(side_effect=RuntimeError("down")))

    events = await db_tool.get_events("Kandy", "2026-08-20", "2026-08-23")

    assert len(events) > 0
