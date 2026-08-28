# tests/test_db_tool.py
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


async def test_get_user_profile_currently_always_returns_defaults():
    # get_user_profile isn't wired to real (Supabase) user data yet - it's
    # a stub that always returns the same empty defaults regardless of
    # user_id. This documents that current state so a future implementer
    # notices this test needs updating once real profiles land, rather
    # than the change going unnoticed.
    profile = await db_tool.get_user_profile("any-user-id")
    assert profile == {"interests": [], "travel_style": None, "budget": None, "home_location": None}
