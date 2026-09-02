from __future__ import annotations

import json
from typing import Any

from app.core.base_agent import BaseAgent
from app.core.llm import get_llm
from app.core.result import AgentResult
from app.core.state import TripState
from app.prompts.planning_prompt import PLANNING_SYSTEM_PROMPT

class PlanningAgent(BaseAgent):
    name = "planning_agent"

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.llm = get_llm("plan")

    async def execute(self, state: TripState) -> AgentResult:
        if not state.recommendations:
            message = "Planning Agent has no recommendations to build an itinerary from."
            state.errors.append(message)
            return AgentResult(success=False, message=message)

        # 1. Resolve basic constraints
        duration_days = state.duration_days if state.duration_days and state.duration_days > 0 else 1
        travelers = state.travelers or 1
        budget = state.budget

        # 2. Build the payload of context for the LLM
        payload = {
            "destination": state.destination,
            "duration_days": duration_days,
            "budget": budget,
            "travelers": travelers,
            "weather_forecast": state.weather,
            "disaster_warnings": getattr(state, "disaster", {}), # Ingests disaster warnings per the build plan
            "candidate_hotels": state.hotels,
            "candidate_restaurants": state.restaurants,
            "candidate_attractions": state.attractions,
            "candidate_events": getattr(state, "events", []),
        }

        # 3. Construct the prompt
        prompt = (
            f"{PLANNING_SYSTEM_PROMPT}\n\n"
            f"Input Parameters:\n{json.dumps(payload, indent=2)}\n\n"
            "Return strictly JSON matching the required structure."
        )

        try:
            # 4. Execute LLM call
            llm_response = await self.llm.ainvoke(prompt)
            parsed_data = self._parse_json_response(llm_response.content)
            
            # 5. Save outputs back to the TripState
            state.itinerary = parsed_data.get("itinerary", [])
            state.estimated_cost = parsed_data.get("estimated_cost", 0.0)
            
            # Using final_response for budget_notes as noted in your mock comments
            state.final_response = parsed_data.get("budget_notes", "")

            return AgentResult(success=True, message=f"Itinerary built for {duration_days} day(s).")
            
        except Exception as e:
            state.errors.append(str(e))
            return AgentResult(success=False, message=f"Planning LLM failed: {str(e)}")

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
            return {"itinerary": [], "estimated_cost": 0.0, "budget_notes": "JSON parsing error."}