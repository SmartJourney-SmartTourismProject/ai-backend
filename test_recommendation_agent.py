import asyncio
import unittest
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name(".env"))

from app.workflows.recommendation_agent import RecommendationAgent
from app.core.state import TripState


class RecommendationAgentTests(unittest.TestCase):
    def test_recommendation_agent_populates_state(self):
        state = TripState(
            user_input="Plan a 3-day trip to Ella",
            destination="Ella",
            interests=["nature", "culture"],
        )

        async def run_test():
            result = await RecommendationAgent().execute(state)
            self.assertTrue(result.success, msg=result.message)
            self.assertTrue(len(state.hotels) > 0)
            self.assertTrue(len(state.restaurants) > 0)
            self.assertTrue(len(state.attractions) > 0)
            self.assertTrue(len(state.recommendations) > 0)
            # Every recommendation item should be tagged with its category
            # since TripState.recommendations is a flat List[dict].
            self.assertIn("category", state.recommendations[0])
            self.assertEqual(state.errors, [])

        asyncio.run(run_test())

    def test_recommendation_agent_requires_destination(self):
        state = TripState(user_input="Plan a trip")  # destination stays None

        async def run_test():
            result = await RecommendationAgent().execute(state)
            self.assertFalse(result.success)
            self.assertTrue(len(state.errors) > 0)

        asyncio.run(run_test())


if __name__ == "__main__":
    unittest.main()