from __future__ import annotations

from typing import Any

from app.core.state import TripState
from app.tools import db_tool


class RecommendationAgent:
    name = "recommendation_agent"

    async def execute(self, state: TripState) -> TripState:
        destination = state.destination
        interests = state.interests or []
        start_date = state.start_date
        end_date = state.end_date or start_date

        hotels = await db_tool.get_hotels(destination, interests)
        restaurants = await db_tool.get_restaurants(destination, interests)
        attractions = await db_tool.get_attractions(destination, interests)

        events: list[dict[str, Any]] = []
        if start_date and end_date:
            events = await db_tool.get_events(destination, start_date, end_date)

        if not (hotels or restaurants or attractions):
            state.errors.append(
                f"No verified listings found for '{destination}'. Recommendation Agent cannot proceed for this destination."
            )
            return state

        state.hotels = hotels
        state.restaurants = restaurants
        state.attractions = attractions
        state.events = events
        state.recommendations = {
            "hotels": hotels,
            "restaurants": restaurants,
            "attractions": attractions,
            "events": events,
        }

        return state
