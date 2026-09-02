"""
The planner agent's human-message payload, factored out so
app/agents/planner_agent.py's normal run and app/core/orchestrator.py's
_repair_node build the exact same context from TripState - a repair attempt
that saw a DIFFERENT view of the candidates/budget than the original planning
call would be reasoning about a request that was never actually made.
"""
from __future__ import annotations

import json

from app.core.state import TripState

_PACE_ITEMS = {"relaxed": 2, "balanced": 3, "packed": 5}


def build_planner_human_message(state: TripState) -> str:
    return json.dumps({
        "trip_context": state.trip_context or {},
        "hotels": state.hotels,
        "restaurants": state.restaurants,
        "attractions": state.attractions,
        "events": state.events,
        "candidate_items": state.candidate_items,
        "budget": state.budget,
        "travelers": state.travelers,
        "duration_days": state.duration_days,
        "pace": state.pace,
        "pace_items_per_day": _PACE_ITEMS.get(state.pace or "balanced", 3),
    })
