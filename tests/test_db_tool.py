# tests/test_db_tool.py
# The original version of this test queried "Ella", but this branch's
# db_tool.py mock only has data for "kandy" - updated to match what's
# actually in the mock (see app/tools/db_tool.py's _MOCK_LISTINGS).

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


async def test_get_user_profile_known_user():
    profile = await db_tool.get_user_profile("demo-user-1")
    assert profile["interests"] == ["culture", "food"]
    assert profile["travel_style"] == "budget"
    assert profile["budget"] == 300.0


async def test_get_user_profile_unknown_user_returns_defaults():
    profile = await db_tool.get_user_profile("nobody")
    assert profile == {"interests": [], "travel_style": None, "budget": None, "home_location": None}
