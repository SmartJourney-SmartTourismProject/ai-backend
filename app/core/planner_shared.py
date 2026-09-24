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

# Single source of truth (itinerary-quality/token-reduction pass) - this
# exact dict used to be copied in app/core/followup_replan.py and inlined
# again in app/core/orchestrator.py's _fallback_node, and
# app/prompts/planner_prompt.py's RULE 1 separately claimed "packed=4-5",
# disagreeing with the code's flat 5. One place to change now; the prompt
# interpolates from PACE_ITEMS too, so it cannot drift again.
PACE_ITEMS: dict[str, int] = {"relaxed": 2, "balanced": 3, "packed": 5}


def resolve_items_per_day(state: TripState) -> int:
    """The one place that turns a traveler's pace/itinerary-density request
    into a concrete daily item count. Currently just PACE_ITEMS keyed by
    state.pace (default "balanced"=3) - Part 3 (follow-up "fewer
    destinations per day") extends this to prefer an explicit
    state.items_per_day when the traveler has set one directly, since
    pace's 3-value enum can't express "one fewer than before"."""
    if state.items_per_day:
        return state.items_per_day
    return PACE_ITEMS.get(state.pace or "balanced", 3)

# O2 (AI_BACKEND_OPTIMIZATION_PLAN.md): exactly the fields the planner
# prompt's own RULES reference. Previously every selection dict went out at
# full width (all 15 db_tool.py fields, including description/photo_url/
# opening_hours) AND the entire candidate_items map was sent alongside it -
# the same data, twice, at full width. build_day_plan gets its real item
# data from the tool call it makes, not from this prompt, so trimming here
# costs the planner nothing it actually uses.
_PLANNER_FIELDS = ("id", "name", "tags", "lat", "lon", "price_level", "rating")


def _strip(items: list[dict]) -> list[dict]:
    return [{k: item[k] for k in _PLANNER_FIELDS if k in item} for item in items]


def build_planner_human_message(state: TripState) -> str:
    return json.dumps({
        "trip_context": state.trip_context or {},
        "hotels": _strip(state.hotels),
        "restaurants": _strip(state.restaurants),
        "attractions": _strip(state.attractions),
        "events": _strip(state.events),
        "budget": state.budget,
        "travelers": state.travelers,
        "duration_days": state.duration_days,
        "pace": state.pace,
        "pace_items_per_day": resolve_items_per_day(state),
    })
