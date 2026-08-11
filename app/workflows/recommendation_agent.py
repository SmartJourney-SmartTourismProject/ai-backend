from __future__ import annotations

import json
from typing import Any
from langchain_google_genai import ChatGoogleGenerativeAI

from app.core.base_agent import BaseAgent
from app.core.result import AgentResult
from app.core.state import TripState
from app.tools import db_tool

# Ensure this prompt file exists in your project!
from app.prompts.recommendation_prompt import RECOMMENDATION_SYSTEM_PROMPT


class RecommendationAgent(BaseAgent):
    name = "recommendation_agent"

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.llm = ChatGoogleGenerativeAI(model="gemini-3.6-flash", temperature=0.2)
        self.system_prompt = RECOMMENDATION_SYSTEM_PROMPT

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

        # 2. Build the payload for the LLM
        payload = {
            "destination": destination,
            "interests": interests,
            "candidate_hotels": hotels,
            "candidate_restaurants": restaurants,
            "candidate_attractions": attractions,
            "candidate_events": events,
        }

        prompt = (
            f"{self.system_prompt}\n\n"
            f"Candidate Data:\n{json.dumps(payload, indent=2)}\n\n"
            "Return valid JSON containing keys 'hotels', 'restaurants', 'attractions', and 'events'."
        )

        try:
            # 3. Call Gemini to curate and rank the list
            llm_response = await self.llm.ainvoke(prompt)
            parsed = self._parse_json_response(llm_response.content)

            # 4. Assign outputs back to state
            state.hotels = parsed.get("hotels", hotels)
            state.restaurants = parsed.get("restaurants", restaurants)
            state.attractions = parsed.get("attractions", attractions)
            
            # Fulfilling the Phase 2 requirement to add the events key
            state.events = parsed.get("events", events) 
            
            # Populate flat recommendations list
            state.recommendations = (
                [{"category": "hotel", **h} for h in state.hotels]
                + [{"category": "restaurant", **r} for r in state.restaurants]
                + [{"category": "attraction", **a} for a in state.attractions]
                + [{"category": "event", **e} for e in state.events]
            )

            return AgentResult(success=True, message=f"LLM successfully curated candidates for {destination}.")
            
        except Exception as e:
            state.errors.append(str(e))
            return AgentResult(success=False, message=f"Recommendation LLM failed: {str(e)}")

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