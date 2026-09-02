from __future__ import annotations

import json
from typing import Any, List

from app.core.base_agent import BaseAgent
from app.core.llm import get_llm
from app.core.result import AgentResult
from app.core.state import TripState
from app.tools import db_tool
from app.rag.rag_service import rag_service

from app.prompts.recommendation_planning_prompt import RECOMMENDATION_PLANNING_SYSTEM_PROMPT


class RecommendationAgent(BaseAgent):
    """
    Curates candidate hotels/restaurants/attractions/events AND builds the
    day-by-day itinerary in a single LLM call. Combined with PlanningAgent's
    job (see planning_agent.py, still available standalone e.g. for a
    "regenerate my itinerary" action) to cut LLM calls per trip-plan request
    from 3 to 2 - recommendation and planning both need the same candidate
    data, weather, and disaster context, so there's nothing planning needs
    that isn't already available at this point in the graph.
    """
    name = "recommendation_agent"

    # Candidates retrieved per category before LLM curation.
    RETRIEVAL_K = 6

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        # get_llm() is the single construction point (app/core/llm.py) - carries
        # the API key, temperature, and provider failover chain consistently,
        # unlike constructing ChatGoogleGenerativeAI(...) directly here.
        self.llm = get_llm("recommend")
        self.system_prompt = RECOMMENDATION_PLANNING_SYSTEM_PROMPT

    async def execute(self, state: TripState) -> AgentResult:
        destination = state.destination
        interests = state.interests or []

        if not destination:
            state.errors.append("Recommendation Agent requires a destination.")
            return AgentResult(success=False, message="Missing destination.")

        # 1. Fetch raw mock candidates from db_tool
        hotels = await db_tool.get_hotels(destination, interests)
        restaurants = await db_tool.get_restaurants(destination, interests)
        attractions = await db_tool.get_attractions(destination, interests)

        # Hardcoding dates for the mock phase to grab events
        events = await db_tool.get_events(destination, "2026-08-20", "2026-08-23")

        if not (hotels or restaurants or attractions):
            message = f"No verified listings found for '{destination}'."
            state.errors.append(message)
            return AgentResult(success=False, message=message)

        # Raw candidates, pre-ranking (see TripState field docs).
        state.candidate_hotels = hotels
        state.candidate_restaurants = restaurants
        state.candidate_attractions = attractions
        state.candidate_events = events

        # 1b. RAG: index today's candidate pool, then retrieve the subset
        # most semantically relevant to the traveler's interests/style
        # before handing anything to the LLM for final curation.
        rag_service.index_candidate_data(
            {
                "hotel": hotels,
                "restaurant": restaurants,
                "attraction": attractions,
                "event": events,
            },
            destination=destination,
        )
        query_terms = list(interests)
        if state.travel_style:
            query_terms.append(state.travel_style)
        query = " ".join(query_terms) or destination

        hotels = self._retrieve_or_fallback("hotel", query, destination, hotels)
        restaurants = self._retrieve_or_fallback("restaurant", query, destination, restaurants)
        attractions = self._retrieve_or_fallback("attraction", query, destination, attractions)
        events = self._retrieve_or_fallback("event", query, destination, events)

        # 2. Build the payload for the LLM - candidates plus the planning
        # constraints PlanningAgent would otherwise need a second call for.
        duration_days = state.duration_days if state.duration_days and state.duration_days > 0 else 1
        payload = {
            "destination": destination,
            "interests": interests,
            "duration_days": duration_days,
            "budget": state.budget,
            "travelers": state.travelers or 1,
            "weather_forecast": state.weather,
            "disaster_warnings": state.disaster,
            "candidate_hotels": hotels,
            "candidate_restaurants": restaurants,
            "candidate_attractions": attractions,
            "candidate_events": events,
            # The traveler's own words, on every call - structured fields
            # (destination/budget/interests) miss anything that doesn't map
            # to a tag, e.g. "no hiking, my knees are bad" or "vegetarian
            # only". Passing the raw message lets the LLM pick up on
            # special requirements slot_filling was never meant to catch.
            "traveler_request": state.user_input,
        }

        # Multi-turn: on a follow-up message ("swap the temple for something
        # indoors", "make day 2 cheaper"), hand the LLM the itinerary it
        # already built too, so it refines that plan instead of silently
        # rebuilding a fresh one from scratch. See
        # RECOMMENDATION_PLANNING_SYSTEM_PROMPT's "MODIFICATION REQUESTS" rule.
        if state.is_followup and state.itinerary:
            payload["previous_itinerary"] = state.itinerary
            payload["modification_request"] = state.user_input

        prompt = (
            f"{self.system_prompt}\n\n"
            f"Candidate Data:\n{json.dumps(payload, indent=2)}\n\n"
            "Return valid JSON containing keys 'hotels', 'restaurants', 'attractions', 'events', "
            "'itinerary', 'estimated_cost', and 'budget_notes'."
        )

        try:
            # 3. Single call: curate candidates AND build the itinerary
            llm_response = await self.llm.ainvoke(prompt)
            parsed = self._parse_json_response(llm_response.content)

            # 4. Assign recommendation outputs back to state
            state.hotels = parsed.get("hotels", hotels)
            state.restaurants = parsed.get("restaurants", restaurants)
            state.attractions = parsed.get("attractions", attractions)
            state.events = parsed.get("events", events)

            # Populate flat recommendations list
            state.recommendations = (
                [{"category": "hotel", **h} for h in state.hotels]
                + [{"category": "restaurant", **r} for r in state.restaurants]
                + [{"category": "attraction", **a} for a in state.attractions]
                + [{"category": "event", **e} for e in state.events]
            )

            # 5. Assign planning outputs back to state (previously PlanningAgent's job)
            state.itinerary = parsed.get("itinerary", [])
            state.estimated_cost = parsed.get("estimated_cost", 0.0)
            state.budget_notes = parsed.get("budget_notes") or None

            return AgentResult(success=True, message=f"Recommendations and itinerary built for {destination}.")

        except Exception as e:
            state.errors.append(str(e))
            return AgentResult(success=False, message=f"Recommendation LLM failed: {str(e)}")

    def _retrieve_or_fallback(
        self, category: str, query: str, destination: str, fallback: List[dict]
    ) -> List[dict]:
        """
        Retrieve the top-k RAG matches for a category, unwrapped back to raw
        item dicts. Falls back to the unfiltered candidate list if retrieval
        finds nothing (e.g. the query is empty or nothing scored).
        """
        hits = rag_service.retrieve_candidates(
            category, query, k=self.RETRIEVAL_K, destination=destination
        )
        items = [hit["item"] for hit in hits if hit.get("item")]
        return items or fallback

    def _parse_json_response(self, text: Any) -> dict[str, Any]:
        if isinstance(text, list):
            parts: list[str] = []
            for item in text:
                if isinstance(item, dict) and "text" in item:
                    parts.append(str(item["text"]))
                else:
                    parts.append(str(item))
            text = "".join(parts)
        elif not isinstance(text, str):
            text = str(text)

        text = text.strip()
        if text.startswith("```json"):
            text = text[7:]
        if text.startswith("```"):
            text = text[3:]
        if text.endswith("```"):
            text = text[:-3]
        try:
            return json.loads(text.strip())
        except json.JSONDecodeError:
            return {}