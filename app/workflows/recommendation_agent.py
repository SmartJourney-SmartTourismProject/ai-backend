from __future__ import annotations

from app.core.base_agent import BaseAgent
from app.core.result import AgentResult
from app.core.state import TripState
from app.tools import db_tool


class RecommendationAgent(BaseAgent):
    
    name = "recommendation_agent"

    async def execute(self, state: TripState) -> AgentResult:
        destination = state.destination
        interests = state.interests or []

        if not destination:
            state.errors.append("Recommendation Agent requires a destination.")
            return AgentResult(success=False, message="No destination provided.")

        hotels = await db_tool.get_hotels(destination, interests)
        restaurants = await db_tool.get_restaurants(destination, interests)
        attractions = await db_tool.get_attractions(destination, interests)

    
        events: list[dict] = []

        if not (hotels or restaurants or attractions):
            message = f"No verified listings found for '{destination}'."
            state.errors.append(message)
            return AgentResult(success=False, message=message)

        state.hotels = hotels
        state.restaurants = restaurants
        state.attractions = attractions
        state.events = events

        state.recommendations = (
            [{"category": "hotel", **h} for h in hotels]
            + [{"category": "restaurant", **r} for r in restaurants]
            + [{"category": "attraction", **a} for a in attractions]
            + [{"category": "event", **e} for e in events]
        )

        return AgentResult(
            success=True,
            message=f"Found {len(state.recommendations)} candidates for {destination}.",
        )