import asyncio
import unittest

from app.core.base_agent import AgentContext
from app.core.recommendation_agent import RecommendationAgent


class RecommendationAgentTests(unittest.TestCase):
    def test_recommendation_agent_returns_all_categories(self):
        context = AgentContext(
            session_id="s1",
            traveler_profile={"travel_interests": ["nature", "culture"]},
            trip_preferences={
                "destination": "Ella",
                "start_date": "2026-08-12",
                "end_date": "2026-08-14",
            },
        )

        async def run_test():
            result = await RecommendationAgent().run(context)
            self.assertTrue(result.success)
            self.assertEqual(result.data["destination"], "Ella")
            self.assertEqual(result.data["source"], "db_tool_mock")
            self.assertIn("hotels", result.data)
            self.assertIn("restaurants", result.data)
            self.assertIn("attractions", result.data)
            self.assertIn("events", result.data)
            self.assertEqual(result.data["count"], 4)
            self.assertEqual(len(result.data["listings"]), 4)

        asyncio.run(run_test())


if __name__ == "__main__":
    unittest.main()
