# tests/test_db_tool.py
import struct
from types import SimpleNamespace
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


async def test_get_user_profile_without_supabase_configured_returns_defaults(monkeypatch):
    # settings.supabase_url/key aren't set in this test environment, so
    # this exercises the fallback path - same as it would for a fresh
    # clone with no .env configured yet.
    monkeypatch.setattr(db_tool.settings, "supabase_url", "")
    monkeypatch.setattr(db_tool.settings, "supabase_key", "")

    profile = await db_tool.get_user_profile("any-user-id")

    assert profile == {"interests": [], "travel_style": None, "budget": None, "home_location": None}


class _FakeQuery:
    def __init__(self, data):
        self._data = data

    def select(self, *args, **kwargs):
        return self

    def eq(self, *args, **kwargs):
        return self

    def ilike(self, *args, **kwargs):
        return self

    def limit(self, *args, **kwargs):
        return self

    async def execute(self):
        return SimpleNamespace(data=self._data)


class _FakeSupabaseClient:
    def __init__(self, table_data: dict):
        self._table_data = table_data

    def table(self, name: str):
        return _FakeQuery(self._table_data.get(name, []))


def _make_ewkb_point_hex(lon: float, lat: float) -> str:
    """Builds a little-endian EWKB Point hex string matching what a real
    PostGIS geography(Point,4326) column returns via supabase-py, so
    _parse_ewkb_point has real input to decode."""
    return struct.pack("<BIIdd", 1, 0x20000001, 4326, lon, lat).hex()


def _patch_supabase(monkeypatch, table_data: dict):
    monkeypatch.setattr(db_tool, "_SUPABASE_AVAILABLE", True)
    monkeypatch.setattr(db_tool.settings, "supabase_url", "https://fake.supabase.co")
    monkeypatch.setattr(db_tool.settings, "supabase_key", "fake-key")
    monkeypatch.setattr(db_tool, "_get_client", AsyncMock(return_value=_FakeSupabaseClient(table_data)))


async def test_get_user_profile_from_real_supabase_row(monkeypatch):
    _patch_supabase(monkeypatch, {
        "traveler_profile": [{
            "user_id": "user-1",
            "travel_interests": ["culture", "food"],
            "travel_style": "budget",
            "default_budget": 300.0,
            "home_location": _make_ewkb_point_hex(lon=79.8612, lat=6.9271),  # Colombo
        }],
    })

    profile = await db_tool.get_user_profile("user-1")

    assert profile["interests"] == ["culture", "food"]
    assert profile["travel_style"] == "budget"
    assert profile["budget"] == 300.0
    assert profile["home_location"]["lat"] == 6.9271
    assert profile["home_location"]["lon"] == 79.8612


async def test_get_user_profile_unknown_user_in_real_supabase_returns_defaults(monkeypatch):
    _patch_supabase(monkeypatch, {"traveler_profile": []})

    profile = await db_tool.get_user_profile("nobody")

    assert profile == {"interests": [], "travel_style": None, "budget": None, "home_location": None}


async def test_get_user_profile_supabase_error_falls_back_to_defaults(monkeypatch):
    monkeypatch.setattr(db_tool, "_SUPABASE_AVAILABLE", True)
    monkeypatch.setattr(db_tool.settings, "supabase_url", "https://fake.supabase.co")
    monkeypatch.setattr(db_tool.settings, "supabase_key", "fake-key")
    monkeypatch.setattr(db_tool, "_get_client", AsyncMock(side_effect=RuntimeError("connection refused")))

    profile = await db_tool.get_user_profile("user-1")

    assert profile == {"interests": [], "travel_style": None, "budget": None, "home_location": None}


async def test_get_hotels_from_real_supabase(monkeypatch):
    # _resolve_id looks up district/category ids by name before the
    # travel_listing query itself.
    _patch_supabase(monkeypatch, {
        "district": [{"id": "district-kandy"}],
        "category": [{"id": "category-hotel"}],
        "travel_listing": [{
            "id": "listing-1", "name": "Real Supabase Hotel", "description": "A real listing",
            "price_range": "$$", "location": _make_ewkb_point_hex(lon=80.6337, lat=7.2906),
            "rating": 4.5, "photo_url": "https://example.com/photo.jpg",
            "opening_hours": "24 hours", "has_public_transit": True,
            "nearest_transit_stop": "Kandy Station", "pickme_available": True,
        }],
    })

    hotels = await db_tool.get_hotels("Kandy")

    assert len(hotels) == 1
    assert hotels[0]["name"] == "Real Supabase Hotel"
    assert hotels[0]["lat"] == 7.2906
    assert hotels[0]["lon"] == 80.6337


async def test_get_hotels_supabase_error_falls_back_to_mock_data(monkeypatch):
    monkeypatch.setattr(db_tool, "_SUPABASE_AVAILABLE", True)
    monkeypatch.setattr(db_tool.settings, "supabase_url", "https://fake.supabase.co")
    monkeypatch.setattr(db_tool.settings, "supabase_key", "fake-key")
    monkeypatch.setattr(db_tool, "_get_client", AsyncMock(side_effect=RuntimeError("connection refused")))

    hotels = await db_tool.get_hotels("Kandy")

    # Falls through to the same mock data test_get_hotels_shape checks.
    assert len(hotels) > 0
    assert all("id" in h and "name" in h for h in hotels)


async def test_get_hotels_supabase_returns_nothing_falls_back_to_mock_data(monkeypatch):
    # District/category resolved fine, but zero verified listings for them -
    # a legitimately empty real result, not an error.
    _patch_supabase(monkeypatch, {
        "district": [{"id": "district-kandy"}],
        "category": [{"id": "category-hotel"}],
        "travel_listing": [],
    })

    hotels = await db_tool.get_hotels("Kandy")

    assert len(hotels) > 0  # mock fallback, not an empty list
