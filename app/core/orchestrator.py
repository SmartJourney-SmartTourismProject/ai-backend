#Graph Edges validate → policy → location → calendar → context → recommend → plan → respond
# app/core/orchestrator.py
import asyncio
from datetime import datetime, timedelta

from langgraph.graph import StateGraph, END

from app.core.state import TripState
from app.core.base_agent import BaseAgent
from app.core.result import AgentResult

from app.utils.validators import validate_trip_state
from app.utils.policy_guard import check_policy
from app.utils.slot_filling import fill_slots
from app.tools.location_tool import resolve_start_location
from app.tools.calendar_tool import get_free_days
from app.tools.weather_tool import get_weather
from app.tools.disaster_tool import get_disaster_info
from app.tools.geocode_tool import geocode_destination



# --- Stub agents for Recommendation/Planner --------------------------
# TODO(Phase 4): replace with real calls into Member B's Recommendation
# and Planner agents once those exist. Wrapped as BaseAgent subclasses
# since these are the genuine "swap in a real implementation later" case.
class RecommendationAgent(BaseAgent):
    async def execute(self, state: TripState) -> AgentResult:
        state.attractions = state.candidate_attractions[:3]
        state.hotels = state.candidate_hotels[:1]
        state.restaurants = state.candidate_restaurants[:2]
        state.events = state.candidate_events[:1]
        state.completed_steps.append("recommend")
        return AgentResult(success=True, state=state)


class PlannerAgent(BaseAgent):
    async def execute(self, state: TripState) -> AgentResult:
        state.itinerary = [{"day": 1, "items": state.attractions}]
        state.estimated_cost = state.budget or 0.0
        state.completed_steps.append("plan")
        return AgentResult(success=True, state=state)
# ------------------------------------------------------------------------


async def _validate_node(state: TripState) -> TripState:
    state = validate_trip_state(state)
    state.completed_steps.append("validate")
    return state


async def _policy_node(state: TripState) -> TripState:
    state = check_policy(state)
    state.completed_steps.append("policy")
    return state


async def _slot_fill_node(state: TripState) -> TripState:
    state = await fill_slots(state)
    state.completed_steps.append("slot_fill")
    return state


async def _location_node(state: TripState) -> TripState:
    # If the API layer already resolved this (it has direct access to the
    # real HTTP request's IP/GPS, which TripState doesn't carry as fields),
    # don't re-resolve.
    if state.start_location:
        state.completed_steps.append("location")
        return state

    # Deliberately None/None here: this node is a fallback for callers that
    # invoke the orchestrator directly (tests, scripts) without going
    # through app/api/trip.py, which already resolves start_location from
    # the real request before building TripState. In the normal HTTP path,
    # this branch is never reached because state.start_location is already
    # set - see the early return above.
    location = await resolve_start_location(client_gps=None, client_ip=None)
    if location:
        state.start_location = location
    else:
        state.errors.append("location_unresolved: need to ask user for start location")
    state.completed_steps.append("location")
    return state


async def _calendar_node(state: TripState) -> TripState:
    if state.user_id:
        state.trip_dates = await get_free_days(state.user_id)
    state.completed_steps.append("calendar")
    return state


async def _context_node(state: TripState) -> TripState:
    if not state.destination:
        state.completed_steps.append("context")
        return state

    coords = await geocode_destination(state.destination)
    if not coords:
        # Couldn't resolve the destination to coordinates - degrade
        # gracefully rather than guessing, same as every other tool's
        # failure mode (BUILD_PLAN.md §8).
        state.completed_steps.append("context")
        return state

    lat, lon = coords["lat"], coords["lon"]

    if state.trip_dates:
        dates = [d["start_date"] for d in state.trip_dates]
    else:
        # No calendar connected - default to today + duration_days (or 1
        # day) so weather/disaster still get checked for *some* dates.
        span = state.duration_days or 1
        today = datetime.utcnow().date()
        dates = [(today + timedelta(days=i)).isoformat() for i in range(span)]

    state.weather, state.disaster = await asyncio.gather(
        get_weather(lat, lon, dates),
        get_disaster_info(lat, lon),
    )
    state.completed_steps.append("context")
    return state



async def _recommend_node(state: TripState) -> TripState:
    result = await RecommendationAgent().execute(state)
    if not result.success:
        result.state.errors.append(result.error or "recommendation failed")
    return result.state


async def _plan_node(state: TripState) -> TripState:
    result = await PlannerAgent().execute(state)
    if not result.success:
        result.state.errors.append(result.error or "planning failed")
    return result.state


# Prefixes that mark an error as advisory (degrade gracefully, don't hide
# a real result behind them) rather than a reason the whole plan failed.
# Add to this list as more soft-failure paths are identified (see BUILD_PLAN.md §8).
_SOFT_ERROR_PREFIXES = ("location_unresolved",)


async def _respond_node(state: TripState) -> TripState:
    hard_errors = [e for e in state.errors if not e.startswith(_SOFT_ERROR_PREFIXES)]
    soft_notes = [e for e in state.errors if e.startswith(_SOFT_ERROR_PREFIXES)]

    if hard_errors and not state.itinerary:
        state.final_response = "Sorry, I ran into an issue: " + "; ".join(hard_errors)
    else:
        state.final_response = (
            f"Here's your trip plan for {state.destination or 'your destination'}: "
            f"{len(state.itinerary)} day(s) planned, "
            f"estimated cost {state.estimated_cost}."
        )
        if soft_notes:
            state.final_response += "\n\nNote: " + "; ".join(soft_notes)

    state.completed_steps.append("respond")
    return state


def _route_after_validate(state: TripState) -> str:
    return "policy" if not state.errors else "respond"


def _route_after_policy(state: TripState) -> str:
    return "slot_fill" if not state.errors else "respond"


def build_orchestrator_graph():
    graph = StateGraph(TripState)

    graph.add_node("validate", _validate_node)
    graph.add_node("policy", _policy_node)
    graph.add_node("slot_fill", _slot_fill_node)
    graph.add_node("location", _location_node)
    graph.add_node("calendar", _calendar_node)
    graph.add_node("context", _context_node)
    graph.add_node("recommend", _recommend_node)
    graph.add_node("plan", _plan_node)
    graph.add_node("respond", _respond_node)

    graph.set_entry_point("validate")

    graph.add_conditional_edges("validate", _route_after_validate)
    graph.add_conditional_edges("policy", _route_after_policy)

    graph.add_edge("slot_fill", "location")
    graph.add_edge("location", "calendar")
    graph.add_edge("calendar", "context")
    graph.add_edge("context", "recommend")
    graph.add_edge("recommend", "plan")
    graph.add_edge("plan", "respond")
    graph.add_edge("respond", END)

    return graph.compile()


orchestrator = build_orchestrator_graph()