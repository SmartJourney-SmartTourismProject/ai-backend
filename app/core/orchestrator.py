#Graph Edges validate -> policy -> slot_fill -> orchestrate -> recommend -> plan -> verify -> (repair|fallback) -> respond
#            slot_fill -> targeted_replan -> verify -> respond   (shape-only follow-up, no LLM - see app/core/followup.py)
# app/core/orchestrator.py
#
# Phase 6 rewrite (docs/master_plan/AGENT_ARCHITECTURE.md §1, PROJECT_MASTER_PLAN.md
# Phase 6): replaces the old linear validate->policy->slot_fill->location->
# calendar->context->recommend->plan->respond chain, where "recommend"/"plan"
# were each a single un-reasoning LLM call, with the target graph: three
# ReAct agents (orchestrate/recommend/plan, app/agents/) plus a pure-Python
# verify/repair/fallback loop that makes an invalid or failed LLM plan
# degrade to a deterministic one instead of erroring out.
import logging
from datetime import date as date_cls

from langgraph.graph import StateGraph, END

from app.core.state import TripState

from app.utils.validators import validate_trip_state
from app.utils.policy_guard import check_policy
from app.utils.slot_filling import fill_slots

from app.agents.orchestrator_agent import OrchestratorAgent
from app.agents.recommendation_agent import RecommendationAgent
from app.agents.planner_agent import PlannerAgent

from app.core.output_validator import ValidationContext, validate
from app.core.fallback import PlanningContext, build_plan
from app.core.followup_replan import rebuild_targeted_days
from app.core.planner_shared import build_planner_human_message
from app.core.react import ReActConfig, run_react
from app.core.llm import get_llm
from app.models.schemas import PlannerOutput, RepairedPlannerOutput
from app.prompts.repair_prompt import build_repair_finalize_system, build_repair_prompt
from app.tools.registry import build_planning_tools
from app.agents.planner_agent import _fetch_cost_table
from langchain_core.messages import HumanMessage, SystemMessage

logger = logging.getLogger(__name__)


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


async def _orchestrate_node(state: TripState) -> TripState:
    await OrchestratorAgent().execute(state)
    state.completed_steps.append("orchestrate")
    return state


async def _targeted_replan_node(state: TripState) -> TripState:
    """Deterministic, no LLM (app/core/followup_replan.py) -
    AGENT_ARCHITECTURE.md §5's "recommendation agent skipped entirely" path
    for a shape-only follow-up (app/core/followup.py's classifier decided
    this in slot_filling.py). Reuses the carried trip_context/itinerary
    from session_store.py rather than re-resolving the destination or
    re-running weather/disaster - nothing about WHERE or WHEN changed, only
    the plan's shape did. May itself degrade `state.followup_scope` to
    "full" (e.g. no district_id carried, or the DB is unreachable) - the
    routing after this node checks for that and falls through to the
    normal orchestrate->recommend->plan pipeline instead of silently
    returning nothing."""
    await rebuild_targeted_days(state)
    state.completed_steps.append("targeted_replan")
    return state


async def _recommend_node(state: TripState) -> TripState:
    await RecommendationAgent().execute(state)
    state.completed_steps.append("recommend")
    return state


async def _plan_node(state: TripState) -> TripState:
    await PlannerAgent().execute(state)
    state.completed_steps.append("plan")
    return state


def _build_validation_context(state: TripState) -> ValidationContext:
    ctx = state.trip_context or {}
    date_window = ctx.get("date_window") or {}
    valid_dates = set(date_window.get("dates") or [])
    if not valid_dates and date_window.get("start_date") and date_window.get("end_date"):
        valid_dates = {date_window["start_date"], date_window["end_date"]}

    per_day_rain = {
        w["date"]: w["rain_probability"]
        for w in (ctx.get("per_day_weather") or [])
    }
    must_avoid_ids = {
        item_id for item_id, item in state.candidate_items.items()
        if set(item.get("tags") or []) & set(state.must_avoid)
    }

    return ValidationContext(
        duration_days=state.duration_days or len(state.itinerary) or 1,
        valid_dates=valid_dates,
        budget=state.budget,
        destination={"lat": ctx.get("lat", 0.0), "lon": ctx.get("lon", 0.0)},
        candidate_listing_ids=set(state.candidate_listing_ids),
        outdoor_listing_ids=set(),   # see verify_node docstring - no per-item outdoor tag lookup wired here yet
        disaster_red_zones=[],       # disaster_tool never returns per-event coordinates, only distance_km - see AGENT_ARCHITECTURE.md §4's disaster tool row
        must_avoid_listing_ids=must_avoid_ids,
        per_day_rain_probability=per_day_rain,
        cost_lookup={},              # empty -> cost_recomputes is a no-op per its own documented behaviour, not a false pass
    )


async def _verify_node(state: TripState) -> TripState:
    """Pure Python, no LLM (AGENT_ARCHITECTURE.md §3.5). Runs only when a
    planner_output exists - a fallback plan is valid by construction and
    also routes through here (its own docstring's promise), so this
    doesn't special-case the source."""
    if not state.planner_output and not state.itinerary:
        state.validation_failures = ["no plan produced by planner or fallback"]
        state.completed_steps.append("verify")
        return state

    if state.plan_source == "fallback":
        # Deterministic by construction - re-running L1/L2 against it would
        # just be re-proving app/core/fallback.py's own test suite on every
        # request for no benefit.
        state.validation_failures = []
        state.completed_steps.append("verify")
        return state

    plan = PlannerOutput.model_validate(state.planner_output)
    result = validate(plan, _build_validation_context(state))
    state.validation_failures = result.failures
    state.completed_steps.append("verify")
    return state


async def _repair_node(state: TripState) -> TripState:
    """One repair attempt (AGENT_ARCHITECTURE.md §5's REPAIR_SPEC) - a
    second failure routes to fallback, never a second repair
    (_route_after_verify enforces this by checking repair_attempts)."""
    state.repair_attempts += 1
    cost_table = await _fetch_cost_table()
    tools = build_planning_tools(cost_table)
    repair_prompt = build_repair_prompt(state.validation_failures)
    human = build_planner_human_message(state)
    messages = [
        SystemMessage(content=repair_prompt),
        HumanMessage(content=human),
        HumanMessage(content=f"Previous (invalid) output: {state.planner_output}"),
    ]

    try:
        result = await run_react(
            llm=get_llm("plan"), tools=tools, messages=messages,
            output_schema=RepairedPlannerOutput, config=ReActConfig(),
            finalize_system=build_repair_finalize_system(state.validation_failures),
        )
    except Exception as e:
        # Broadened beyond ReActError - see app/agents/orchestrator_agent.py's
        # identical fix (Phase 8, scenario 11).
        logger.warning(f"repair attempt failed: {e}")
        state.errors.append(f"repair_failed: {e}")
        state.completed_steps.append("repair")
        return state

    output = result.output
    state.planner_output = output.model_dump()
    state.itinerary = [d.model_dump() for d in output.itinerary]
    state.estimated_cost = output.estimated_cost
    state.budget_notes = output.budget_notes
    state.react_traces["repair"] = {
        "steps_used": result.steps_used, "tools_used": result.tools_used, "stopped_by": result.stopped_by,
    }
    state.completed_steps.append("repair")
    return state


async def _fallback_node(state: TripState) -> TripState:
    """Zero-LLM deterministic plan (app/core/fallback.py) - reached when the
    planner LLM errored outright, or failed validation twice. Constructs
    from real candidate data already gathered by the recommendation agent,
    so no repeated tool work."""
    ctx_dict = state.trip_context or {}
    date_window = ctx_dict.get("date_window") or {}
    try:
        start_date = date_cls.fromisoformat(date_window["start_date"])
    except (KeyError, ValueError):
        start_date = date_cls.today()

    per_day_rain = {
        w["date"]: w["rain_probability"] for w in (ctx_dict.get("per_day_weather") or [])
    }

    planning_ctx = PlanningContext(
        destination_name=ctx_dict.get("destination_name") or state.destination or "your destination",
        district_id=ctx_dict.get("district_id"),
        duration_days=state.duration_days or 1,
        start_date=start_date,
        budget=state.budget,
        travelers=state.travelers or 1,
        travel_style=state.travel_style,
        interests=state.interests,
        must_avoid=state.must_avoid,
        pace_items_per_day={"relaxed": 2, "balanced": 3, "packed": 5}.get(state.pace or "balanced", 3),
        start_location=state.start_location,
        per_day_rain_probability=per_day_rain,
        disaster=state.disaster,
    )

    # candidate_pools (Phase 8 fix) is the raw db_search_* result set the
    # recommendation agent observed - populated whether or not its own
    # structured RecommendationOutput call succeeded (RecommendationAgent
    # salvages it from ReActError.trace on failure). This is what
    # build_plan_core actually ranks from scratch; state.hotels/etc only
    # ever hold the recommendation agent's SELECTED short list (empty on
    # failure), which used to leave this call with nothing to build from -
    # exactly the common case per TODO.md, not an edge case.
    result = await build_plan(
        planning_ctx,
        state.candidate_pools.get("hotel") or state.hotels,
        state.candidate_pools.get("restaurant") or state.restaurants,
        state.candidate_pools.get("attraction") or state.attractions,
        state.candidate_pools.get("event") or state.events,
    )
    state.itinerary = result.itinerary
    state.estimated_cost = result.estimated_cost
    state.budget_notes = result.budget_notes
    state.plan_source = "fallback"
    state.completed_steps.append("fallback")
    return state


# Prefixes that mark an error as advisory (degrade gracefully, don't hide
# a real result behind them) rather than a reason the whole plan failed.
_SOFT_ERROR_PREFIXES = ("location_unresolved", "profile_unavailable", "safety_note")


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
        if state.plan_source:
            state.final_response += f" (plan_source: {state.plan_source})"
        if state.budget_notes:
            state.final_response += f"\n\nBudget note: {state.budget_notes}"
        if soft_notes:
            state.final_response += "\n\nNote: " + "; ".join(soft_notes)

    state.completed_steps.append("respond")
    return state


def _route_after_validate(state: TripState) -> str:
    return "policy" if not state.errors else "respond"


def _route_after_policy(state: TripState) -> str:
    return "slot_fill" if not state.errors else "respond"


def _route_after_slot_fill(state: TripState) -> str:
    if state.clarification_needed:
        return "respond"
    if state.is_followup and state.followup_scope == "shape_only":
        return "targeted_replan"
    return "orchestrate"


def _route_after_targeted_replan(state: TripState) -> str:
    # rebuild_targeted_days() itself may have degraded followup_scope to
    # "full" (no carried district_id/itinerary, or a real DB failure) -
    # that's the signal to fall through to the normal pipeline instead of
    # treating an empty/unrebuilt state as done.
    return "orchestrate" if state.followup_scope == "full" else "verify"


def _route_after_recommend(state: TripState) -> str:
    return "plan" if state.recommendations else "fallback"


def _route_after_verify(state: TripState) -> str:
    if not state.validation_failures:
        return "respond"
    if state.repair_attempts == 0:
        return "repair"
    return "fallback"


def build_orchestrator_graph():
    graph = StateGraph(TripState)

    graph.add_node("validate", _validate_node)
    graph.add_node("policy", _policy_node)
    graph.add_node("slot_fill", _slot_fill_node)
    graph.add_node("orchestrate", _orchestrate_node)
    graph.add_node("targeted_replan", _targeted_replan_node)
    graph.add_node("recommend", _recommend_node)
    graph.add_node("plan", _plan_node)
    graph.add_node("verify", _verify_node)
    graph.add_node("repair", _repair_node)
    graph.add_node("fallback", _fallback_node)
    graph.add_node("respond", _respond_node)

    graph.set_entry_point("validate")

    graph.add_conditional_edges("validate", _route_after_validate)
    graph.add_conditional_edges("policy", _route_after_policy)
    graph.add_conditional_edges("slot_fill", _route_after_slot_fill)
    graph.add_conditional_edges("targeted_replan", _route_after_targeted_replan)

    graph.add_edge("orchestrate", "recommend")
    graph.add_conditional_edges("recommend", _route_after_recommend)
    graph.add_edge("plan", "verify")
    graph.add_conditional_edges("verify", _route_after_verify)
    graph.add_edge("repair", "verify")
    graph.add_edge("fallback", "verify")
    graph.add_edge("respond", END)

    return graph.compile()


orchestrator = build_orchestrator_graph()
