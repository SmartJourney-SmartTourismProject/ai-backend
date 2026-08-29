#Graph Edges validate → policy → location → calendar → context → recommend → plan → respond
# app/core/orchestrator.py
import asyncio
from datetime import datetime, timedelta, timezone

from langgraph.graph import StateGraph, END

from app.core.state import TripState

from app.utils.validators import validate_trip_state
from app.utils.policy_guard import check_policy
from app.utils.slot_filling import fill_slots
from app.tools.location_tool import resolve_start_location
from app.tools.calendar_tool import get_free_days
from app.tools.weather_tool import get_weather
from app.tools.disaster_tool import get_disaster_info
from app.tools.geocode_tool import geocode_destination


# Phase 4 wiring: Member B's real Recommendation/Planner agents, replacing
# the fixed-dict stubs this file used during Phase 2. RecommendationAgent
# curates candidates from db_tool/RAG AND builds the itinerary in one LLM
# call (see its own docstring) - so PlannerAgent is only invoked as a
# fallback if that combined call didn't produce one, not on every run.
from app.workflows.recommendation_agent import RecommendationAgent
from app.workflows.planning_agent import PlanningAgent


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
        today = datetime.now(timezone.utc).date()
        dates = [(today + timedelta(days=i)).isoformat() for i in range(span)]

    state.weather, state.disaster = await asyncio.gather(
        get_weather(lat, lon, dates),
        get_disaster_info(lat, lon),
    )
    state.completed_steps.append("context")
    return state



async def _recommend_node(state: TripState) -> TripState:
    # RecommendationAgent mutates `state` in place and, on failure, already
    # appends its own reason to state.errors before returning (see
    # app/workflows/recommendation_agent.py) - don't append result.message
    # again here or every failure shows up twice in state.errors.
    await RecommendationAgent().execute(state)
    state.completed_steps.append("recommend")
    return state


async def _plan_node(state: TripState) -> TripState:
    # RecommendationAgent already builds state.itinerary in the same call
    # (merged to save an LLM call per request) - only fall back to a
    # standalone PlanningAgent call if that didn't happen. PlanningAgent
    # also self-reports failures into state.errors - see note above.
    if state.itinerary:
        state.completed_steps.append("plan")
        return state

    await PlanningAgent().execute(state)
    state.completed_steps.append("plan")
    return state


# Prefixes that mark an error as advisory (degrade gracefully, don't hide
# a real result behind them) rather than a reason the whole plan failed.
# Add to this list as more soft-failure paths are identified (see BUILD_PLAN.md §8).
_SOFT_ERROR_PREFIXES = ("location_unresolved",)


async def _respond_node(state: TripState) -> TripState:
    
    if state.clarification_needed:
        state.final_response = state.clarification_needed
        state.completed_steps.append("respond")
        return state    
    
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

def _route_after_slot_fill(state: TripState) -> str:
    return "respond" if state.clarification_needed else "location"



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
    graph.add_conditional_edges("slot_fill", _route_after_slot_fill)

    graph.add_edge("location", "calendar")
    graph.add_edge("calendar", "context")
    graph.add_edge("context", "recommend")
    graph.add_edge("recommend", "plan")
    graph.add_edge("plan", "respond")
    graph.add_edge("respond", END)

    return graph.compile()


orchestrator = build_orchestrator_graph()