#Graph Edges validate → policy → location → calendar → context → recommend → plan → respond
# app/core/orchestrator.py
from langgraph.graph import StateGraph, END

from app.core.state import TripState

from app.utils.validators import validate_trip_state
from app.utils.policy_guard import check_policy
from app.utils.slot_filling import fill_slots
from app.tools.location_tool import resolve_start_location
from app.tools.calendar_tool import get_free_days
from app.tools.weather_tool import get_weather

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
    # Weather + disaster would run concurrently here via asyncio.gather.
    # disaster_tool.py is deprioritized for now — swap in gather() once
    # it exists:
    #   weather, disaster = await asyncio.gather(
    #       get_weather(lat, lon, dates), get_disaster_info(lat, lon)
    #   )
    if state.start_location and state.trip_dates:
        lat = state.start_location["lat"]
        lon = state.start_location["lon"]
        dates = [d["start_date"] for d in state.trip_dates]
        state.weather = await get_weather(lat, lon, dates)
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


async def _respond_node(state: TripState) -> TripState:
    if state.errors:
        state.final_response = "Sorry, I ran into an issue: " + "; ".join(state.errors)
    else:
        state.final_response = (
            f"Here's your trip plan for {state.destination or 'your destination'}: "
            f"{len(state.itinerary)} day(s) planned, "
            f"estimated cost {state.estimated_cost}."
        )
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