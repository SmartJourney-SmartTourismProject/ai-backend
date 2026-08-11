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
            profile = await db_tool.get_user_profile("user-1")
            assert isinstance(profile, dict)
            assert profile.get("user_id") == "user-1"
            assert "interests" in profile and "budget" in profile

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
