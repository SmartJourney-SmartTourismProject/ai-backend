import asyncio
import unittest

from app.tools import db_tool


class DBToolTests(unittest.TestCase):
    def test_get_hotels_shape(self):
        async def run():
            hotels = await db_tool.get_hotels("Ella")
            assert isinstance(hotels, list)
            assert len(hotels) > 0
            h = hotels[0]
            assert "id" in h and "name" in h and "lat" in h and "lon" in h

        asyncio.run(run())

    def test_get_user_profile_shape(self):
        async def run():
            # Shape is the BUILD_PLAN.md §4 contract: interests/travel_style/
            # budget/home_location - no user_id key, so this must not assert one.
            profile = await db_tool.get_user_profile("user-1")
            assert isinstance(profile, dict)
            assert "interests" in profile and "travel_style" in profile
            assert "budget" in profile and "home_location" in profile

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
