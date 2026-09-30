#Graph Edges validate -> policy -> slot_fill -> orchestrate -> recommend -> plan -> verify -> (repair|fallback) -> respond
#            slot_fill -> targeted_replan -> verify -> respond   (shape-only follow-up, no LLM - see app/core/followup.py)
#            slot_fill -> answer -> respond                      (intent="question" - RAG Q&A, no plan attempted - app/rag/)
#            verify -> answer -> respond                         (intent="both" - a plan AND a question in one turn)
#            slot_fill -> weather -> END                         (intent="weather" or a weather follow-up - live forecast, no LLM)
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
import re
from datetime import date as date_cls, timedelta

from langgraph.graph import StateGraph, END

from app.core.state import TripState

from app.utils.validators import validate_trip_state
from app.utils.policy_guard import check_policy
from app.utils.slot_filling import fill_slots

from app.agents.recommendation_agent import RecommendationAgent
from app.agents.planner_agent import PlannerAgent

from app.config.settings import settings
from app.core.context_resolver import resolve_trip_context
from app.core.itinerary import DAY_END, DEFAULT_MAX_SINGLE_HOP_MINUTES
from app.core.output_validator import ValidationContext, day_scoped_repair_target, validate
from app.core.fallback import PlanningContext, build_plan
from app.core.followup_replan import rebuild_targeted_days
from app.core.planner_shared import (
    assemble_planner_days, build_planner_human_message, enforce_budget_notes, fill_missing_days,
    resolve_day_context, resolve_items_per_day, resolve_planner_max_steps,
)
from app.core.react import ReActConfig, run_react
from app.core.llm import get_llm
from app.models.schemas import AnswerOutput, ItineraryDay, PlannerOutput, RepairedPlannerOutput
from app.prompts import get_prompt
from app.prompts._base import enforce_max_input_chars
from app.prompts.repair_prompt import (
    REPAIR_FINALIZE_SYSTEM, REPAIR_SYSTEM_PROMPT, build_repair_failures_message,
)
from app.rag.retrieve import Passage, best_vector_score, retrieve as retrieve_passages
from app.tools.geo_tool import resolve_place
from app.tools.weather_tool import get_weather
from app.utils.clock import today_local
from typing import Optional
from app.tools.registry import build_planning_tools
from app.agents.planner_agent import _build_item_store, _fetch_cost_table, _fetch_outdoor_tags
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
    # Deterministic, no LLM (app/core/context_resolver.py, "C2" in
    # docs/AI_BACKEND_OPTIMIZATION_PLAN.md) - resolves destination/district,
    # a date window, weather and disaster info, and the derived safety
    # notes. Was a 3-agent ReAct system; is now 2 (recommendation, planner)
    # plus this plumbing step, which never needed to be probabilistic.
    await resolve_trip_context(state)
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
        # Expand the full range, not just its two endpoints - a bare
        # {start, end} set silently rejected every middle day of any 3+ day
        # trip on dates_in_window whenever "dates" itself was missing (this
        # branch only exists for older/partial date_window shapes; the
        # normal path from context_resolver.py always includes "dates").
        start = date_cls.fromisoformat(date_window["start_date"])
        end = date_cls.fromisoformat(date_window["end_date"])
        valid_dates, d = set(), start
        while d <= end:
            valid_dates.add(d.isoformat())
            d += timedelta(days=1)

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
        # Part 4 (guardrails): the same real values the deterministic path
        # enforces by construction (app/core/itinerary.py), now also
        # checked against the LLM planner's own output.
        day_end=DAY_END,
        max_single_hop_minutes=DEFAULT_MAX_SINGLE_HOP_MINUTES,
        expected_items_per_day=resolve_items_per_day(state),
        excluded_categories=set(state.exclude_categories or []),
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


def _trim_previous_output(planner_output: dict) -> dict:
    """B3 (AI_BACKEND_OPTIMIZATION_PLAN.md): the repair call used to resend
    the ENTIRE previous itinerary verbatim - every item's name/lat/lon/
    currency/notes, all already present in the human message built by
    build_planner_human_message() (the same selections, by id). Strips each
    item to just what identifies it and what a repair might need to check
    (listing_id/type/time/end_time/est_cost) - name/lat/lon are recoverable
    from the selections already in context. Deliberately NOT trimmed to
    only the failing day(s): several L2 rules (day_count, cost_consistent,
    budget_honest, days_sequential) are plan-wide, so a repair for one of
    those genuinely needs every day's shape, not just the day a per-item
    rule happened to name."""
    days = planner_output.get("itinerary") or []
    return {
        "itinerary": [
            {
                "day": d.get("day"), "date": d.get("date"), "day_cost": d.get("day_cost"),
                "items": [
                    {"listing_id": i.get("listing_id"), "type": i.get("type"),
                     "time": i.get("time"), "end_time": i.get("end_time"), "est_cost": i.get("est_cost")}
                    for i in (d.get("items") or [])
                ],
            }
            for d in days
        ],
        "estimated_cost": planner_output.get("estimated_cost"),
    }


async def _repair_node(state: TripState) -> TripState:
    """Up to settings.max_repair_attempts repair attempts (AGENT_ARCHITECTURE.md
    §5's REPAIR_SPEC, raised from 1 to 2 by user decision 2026-09-26) -
    _route_after_verify enforces the cap by checking repair_attempts, and
    separately short-circuits to fallback the moment a repair reproduces the
    exact same failure set as the attempt before it (a systematic failure -
    see settings.repair_temperature_step's own comment - that a further
    identical attempt has no real chance of fixing)."""
    # Snapshotted BEFORE the increment/rebuild below touch anything -
    # state.validation_failures right now is exactly the failure set THIS
    # attempt is about to try fixing, and _route_after_verify's no-progress
    # guard compares the NEXT verify's failures against this snapshot (kept
    # here, not in the router itself, since a conditional-edge function
    # mutating state isn't a pattern this graph relies on elsewhere).
    state.previous_validation_failures = list(state.validation_failures)
    state.repair_attempts += 1

    cost_table = await _fetch_cost_table()

    # Tier 4 fast path (2026-09-26): when EVERY current failure is confined
    # to a specific day and is a rule build_day_plan already enforces by
    # construction (output_validator.day_scoped_repair_target's own
    # docstring has the exact list and reasoning), rebuild just those
    # day(s) deterministically - the same machinery fill_missing_days
    # already uses for an unbacked day - and skip the LLM call ENTIRELY if
    # that alone makes the plan valid again. Falls through to a real LLM
    # repair whenever any failure isn't safely localizable this way
    # (day_count, an L1 hallucinated id, a geo/budget/cost failure...).
    target_days = day_scoped_repair_target(state.validation_failures)
    if target_days is not None:
        try:
            current_days = [ItineraryDay.model_validate(d) for d in (state.itinerary or [])]
            patched_days, fill_warnings = fill_missing_days(
                state, current_days, cost_table, force_rebuild_days=list(target_days),
            )
            estimated_cost = round(sum(d.day_cost for d in patched_days), 2)
            patched_output = RepairedPlannerOutput(
                itinerary=patched_days,
                estimated_cost=estimated_cost,
                currency="LKR",
                budget_notes=enforce_budget_notes(
                    (state.planner_output or {}).get("budget_notes"), estimated_cost, state.budget,
                ),
            )
            recheck = validate(patched_output, _build_validation_context(state))
        except Exception as e:
            # A deterministic rebuild can still fail (a real DB hiccup
            # inside fill_missing_days, a malformed carried itinerary) -
            # degrade to the normal LLM repair below rather than let an
            # optimization crash the request.
            logger.warning(f"repair: deterministic day-scoped rebuild raised, falling through to LLM repair: {e}")
            recheck = None

        if recheck is not None and not recheck.failures:
            logger.info(f"repair: fixed day(s) {sorted(target_days)} deterministically, no LLM call needed")
            if fill_warnings:
                state.errors.append(f"planner_day_assembly: {'; '.join(fill_warnings)}")
            state.planner_output = patched_output.model_dump()
            state.itinerary = [d.model_dump() for d in patched_days]
            state.estimated_cost = patched_output.estimated_cost
            state.budget_notes = patched_output.budget_notes
            state.plan_source = "llm"
            state.react_traces["repair"] = {"steps_used": 0, "tools_used": [], "stopped_by": "deterministic"}
            state.completed_steps.append("repair")
            return state
        if recheck is not None:
            logger.info(
                f"repair: deterministic rebuild of day(s) {sorted(target_days)} "
                f"didn't fully resolve failures ({recheck.failures}) - falling through to LLM repair"
            )

    outdoor_tags = await _fetch_outdoor_tags()
    district_id = (state.trip_context or {}).get("district_id")
    day_context = resolve_day_context(state)
    tools = build_planning_tools(cost_table, _build_item_store(state), outdoor_tags, district_id, day_context)
    human = enforce_max_input_chars(get_prompt("repair"), build_planner_human_message(state))
    previous_output = _trim_previous_output(state.planner_output or {})
    messages = [
        SystemMessage(content=REPAIR_SYSTEM_PROMPT),
        HumanMessage(content=build_repair_failures_message(state.validation_failures)),
        HumanMessage(content=human),
        HumanMessage(content=f"Previous (invalid) output: {previous_output}"),
    ]

    # Escalating temperature per attempt (settings.repair_temperature_step,
    # user decision 2026-09-26): a repair re-sends the exact same prompt and
    # candidates as the failed attempt before it, so at the original
    # temperature it has a real chance of reproducing the identical wrong
    # answer verbatim. state.repair_attempts was already incremented above,
    # so attempt 1 gets +step, attempt 2 gets +2*step, etc.
    repair_temperature = settings.llm_temperature + settings.repair_temperature_step * state.repair_attempts

    try:
        result = await run_react(
            llm=get_llm("plan", temperature=repair_temperature), tools=tools, messages=messages,
            output_schema=RepairedPlannerOutput,
            config=ReActConfig(max_steps=resolve_planner_max_steps(state.duration_days)),
            finalize_system=REPAIR_FINALIZE_SYSTEM,
        )
    except Exception as e:
        # Broadened beyond ReActError - see app/agents/planner_agent.py's
        # identical fix (Phase 8, scenario 11).
        logger.warning(f"repair attempt failed: {e}")
        state.errors.append(f"repair_failed: {e}")
        state.completed_steps.append("repair")
        return state

    output = result.output
    assembled_days, warnings, unbacked_days = assemble_planner_days(result.trace, output.itinerary)
    assembled_days, fill_warnings = fill_missing_days(
        state, assembled_days, cost_table, force_rebuild_days=unbacked_days,
    )
    warnings = warnings + fill_warnings
    if warnings:
        logger.warning(f"repair day assembly: {'; '.join(warnings)}")
        state.errors.append(f"planner_day_assembly: {'; '.join(warnings)}")

    estimated_cost = round(sum(d.day_cost for d in assembled_days), 2)
    assembled_output = RepairedPlannerOutput(
        itinerary=assembled_days,
        estimated_cost=estimated_cost,
        currency="LKR",   # Part 5, server-side enforcement - see planner_agent.py's identical fix
        budget_notes=enforce_budget_notes(output.budget_notes, estimated_cost, state.budget),
    )
    state.planner_output = assembled_output.model_dump()
    state.itinerary = [d.model_dump() for d in assembled_days]
    state.estimated_cost = assembled_output.estimated_cost
    state.budget_notes = assembled_output.budget_notes
    # Pre-existing gap, found live (fallback investigation, 2026-09-25):
    # this node never set plan_source at all, unlike app/agents/
    # planner_agent.py's own "llm" on success. A successful repair used to
    # leave state.plan_source at whatever it was before (often still None,
    # when the original planner call raised outright rather than merely
    # failing validation) - so a real, valid, repaired plan could reach
    # _respond_node reporting no plan_source whatsoever. A repaired plan is
    # still an LLM-produced one, just corrected once.
    state.plan_source = "llm"
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
    # Live-found (fallback investigation, 2026-09-25): the LLM/repair plan's
    # own validation_failures were never surfaced anywhere - _verify_node
    # resets them to [] the moment it re-checks THIS node's own (always
    # valid) output, and DEBUG=false already strips the trace that would
    # have shown a repair even ran. The API response ended up saying
    # "fallback" with literally no way to tell why. Captured here, before
    # anything below overwrites it, since this is the one point in the
    # graph where the rejected plan's failures still exist on state.
    if state.validation_failures:
        state.errors.append(f"llm_plan_rejected: {', '.join(state.validation_failures)}")
    else:
        # ...but validation failure is only ONE of the two ways into this
        # node. _route_after_recommend sends a request straight here,
        # skipping plan/verify entirely, whenever the recommendation agent
        # produced no selections - so validation_failures is legitimately
        # empty and the branch above records nothing. The real reason is
        # sitting in state.errors as a HARD error (recommendation_failed /
        # planner_failed), and _respond_node only ever surfaces hard errors
        # when there's NO plan to show - so a fallback plan built after an
        # upstream agent failure reported "plan_source: fallback" with no
        # reason at all (live-found 2026-09-26, a 5-day Galle request).
        # Recorded as a soft note so it rides along with the real plan
        # instead of being swallowed by it.
        upstream = [
            e for e in state.errors
            if e.startswith(("recommendation_failed", "planner_failed", "repair_failed"))
        ]
        reason = "; ".join(upstream) if upstream else (
            "no recommendations were produced" if not state.recommendations
            else "the planner produced no usable plan"
        )
        state.errors.append(f"fallback_reason: {reason}")

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
        exclude_categories=state.exclude_categories,
        pace_items_per_day=resolve_items_per_day(state),
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
# llm_plan_rejected/planner_day_assembly (fallback investigation,
# 2026-09-25): both fire only alongside a real, complete plan (a fallback
# plan, or an LLM plan with some days assembled from tool output and others
# from the model's own answer) - never a reason to claim failure outright.
_SOFT_ERROR_PREFIXES = (
    "location_unresolved", "profile_unavailable", "safety_note",
    "llm_plan_rejected", "planner_day_assembly",
    # fallback_reason (2026-09-26): set by _fallback_node whenever it was
    # entered for a reason OTHER than a validation rejection. Soft for the
    # same reason llm_plan_rejected is - it always accompanies a real,
    # complete fallback plan, and is an explanation of how that plan was
    # built, not a claim that the request failed.
    "fallback_reason",
    # recommendation_hallucinated_drop (2026-09-26): app/agents/
    # recommendation_agent.py's own L1 check already removed the bad
    # selection before it could reach state.hotels/etc - the plan that
    # follows is built entirely from real, observed candidates. This is a
    # transparency note about what got corrected, not a failure.
    "recommendation_hallucinated_drop",
)


def _budget_breakdown_text(state: TripState) -> str:
    """Per-day and per-category costs read straight off the itinerary already
    on state. No re-planning, no LLM call: the numbers must match the plan the
    user is looking at, and the only way to guarantee that is to read them
    rather than recompute them from a fresh plan."""
    lines = [f"Budget breakdown for {state.destination or 'your trip'}:"]
    by_category: dict[str, float] = {}

    for day in state.itinerary:
        day_num = day.get("day", "?")
        day_cost = float(day.get("day_cost") or 0.0)
        lines.append(f"\nDay {day_num} - {day_cost:,.2f} LKR")
        for item in day.get("items") or []:
            cost = float(item.get("est_cost") or 0.0)
            kind = item.get("type") or "item"
            by_category[kind] = by_category.get(kind, 0.0) + cost
            # Items with no price are shown as such rather than as 0.00,
            # which would read as "free" when it means "unknown".
            shown = f"{cost:,.2f} LKR" if cost else "no price data"
            lines.append(f"  - {item.get('name', 'Unnamed')}: {shown}")

    if by_category:
        lines.append("\nBy category:")
        for kind, total in sorted(by_category.items(), key=lambda kv: kv[1], reverse=True):
            lines.append(f"  {kind}: {total:,.2f} LKR")

    if state.estimated_cost is not None:
        lines.append(f"\nTotal: {float(state.estimated_cost):,.2f} LKR")
    if state.budget:
        lines.append(f"Your budget: {float(state.budget):,.2f} LKR")
    if state.budget_notes:
        lines.append(f"\nNote: {state.budget_notes}")

    return "\n".join(lines)


_FORECAST_HORIZON_DAYS = 5   # OpenWeather's free /forecast covers ~5 days


def _umbrella_advice(rain_probability: float) -> str:
    if rain_probability >= 0.5:
        return "Yes, take an umbrella."
    if rain_probability >= 0.2:
        return "Maybe - pack a compact umbrella just in case."
    return "Probably not needed."


def _weather_dates(state: TripState) -> list[str]:
    today = today_local()
    when = state.weather_when
    if when == "today":
        return [today.isoformat()]
    if when == "tomorrow":
        return [(today + timedelta(days=1)).isoformat()]
    if when == "day_after_tomorrow":
        return [(today + timedelta(days=2)).isoformat()]
    if when == "trip_dates" or (when is None and state.trip_context):
        window = (state.trip_context or {}).get("date_window") or {}
        if window.get("dates"):
            return list(window["dates"])
    return [(today + timedelta(days=i)).isoformat() for i in range(3)]


async def _weather_place(state: TripState) -> tuple[Optional[str], Optional[dict]]:
    """(label, {lat, lon}) for this weather question, in order: the place
    named in THIS message, the planned trip's destination, the traveller's
    own location (GPS, else IP). (None, None) when there's nothing to go on."""
    named = state.weather_place
    if named:
        # As typed ("colombo") - capitalised for display, lookup unaffected.
        named = named if any(ch.isupper() for ch in named) else named.title()
        try:
            place = await resolve_place(named)
        except Exception as e:
            logger.warning(f"weather: could not resolve {named!r}: {e}")
            place = None
        return (named, {"lat": place["lat"], "lon": place["lon"]}) if place else (named, None)

    ctx = state.trip_context or {}
    if ctx.get("lat") is not None and ctx.get("lon") is not None:
        return ctx.get("destination_name") or state.destination, {"lat": ctx["lat"], "lon": ctx["lon"]}
    if state.destination:
        try:
            place = await resolve_place(state.destination)
        except Exception:
            place = None
        if place:
            return state.destination, {"lat": place["lat"], "lon": place["lon"]}

    if state.start_location:
        return "your location", {"lat": state.start_location["lat"], "lon": state.start_location["lon"]}
    return None, None


async def _weather_node(state: TripState) -> TripState:
    """Live weather questions (intent="weather"): "will it rain tomorrow in
    Colombo, should I take an umbrella?". Answered from OpenWeather via
    get_weather - no LLM call, no plan built or changed. A trip's stored
    forecast (_weather_text) is only the fallback when the live call fails."""
    label, point = await _weather_place(state)
    if point is None:
        state.final_response = (
            f"I couldn't find {label} - which place in Sri Lanka should I check?" if label
            else "Which place should I check the weather for?"
        )
        state.completed_steps.append("weather")
        return state

    dates = _weather_dates(state)
    horizon = (today_local() + timedelta(days=_FORECAST_HORIZON_DAYS - 1)).isoformat()
    in_range = [d for d in dates if d <= horizon]

    result = await get_weather(point["lat"], point["lon"], in_range) if in_range else None
    forecast = (result or {}).get("forecast") or []

    if not forecast:
        if not in_range:
            state.final_response = (
                f"Those dates are too far ahead to forecast for {label} - the forecast only covers "
                f"the next {_FORECAST_HORIZON_DAYS} days. Ask me again closer to the time."
            )
        elif (state.trip_context or {}).get("per_day_weather"):
            state.final_response = _weather_text(state)
        else:
            state.final_response = "The weather service isn't available right now - please try again shortly."
        state.completed_steps.append("weather")
        return state

    today = today_local()
    names = {today.isoformat(): "Today", (today + timedelta(days=1)).isoformat(): "Tomorrow"}
    lines = [f"Weather for {label}:"]
    for day in forecast:
        pct = round(float(day["rain_probability"]) * 100)
        lines.append(
            f"  {names.get(day['date'], day['date'])} ({day['date']}): {day['condition']}, "
            f"{day['temp_min']:.0f}-{day['temp_max']:.0f}°C, {pct}% chance of rain - "
            f"{_umbrella_advice(float(day['rain_probability']))}"
        )
    skipped = [d for d in dates if d > horizon]
    if skipped:
        lines.append(f"\n{', '.join(skipped)} are beyond the {_FORECAST_HORIZON_DAYS}-day forecast.")
    state.final_response = "\n".join(lines)
    state.completed_steps.append("weather")
    return state


def _weather_text(state: TripState) -> str:
    """The forecast already stored with the trip, read straight off
    trip_context - same rule as _budget_breakdown_text: no tool or LLM
    call, so the answer matches the plan on screen. It is the forecast as
    of when the trip was planned, and says so."""
    forecast = (state.trip_context or {}).get("per_day_weather") or []
    if not forecast:
        return ("I don't have a weather forecast saved for this trip - it may be too far out "
                "for the forecast service, or it was unavailable when the plan was made.")
    lines = [f"Weather for {state.destination or 'your trip'} (forecast from when your plan was made):"]
    for day in forecast:
        pct = round(float(day.get("rain_probability") or 0.0) * 100)
        lines.append(
            f"  {day.get('date')}: {day.get('condition')}, "
            f"{day.get('temp_min'):.0f}-{day.get('temp_max'):.0f}°C, {pct}% chance of rain"
        )
    rainy = [d["date"] for d in forecast if float(d.get("rain_probability") or 0.0) >= 0.5]
    lines.append(f"\nLikely rainy: {', '.join(rainy)}." if rainy else "\nRain looks unlikely on these days.")
    return "\n".join(lines)


def _cited_passage_indices(answer_text: str, passage_count: int) -> list[int]:
    """[N] markers in `answer_text`, deduped, in first-appearance order,
    filtered to a real 1-based passage index - never trusts the model's
    citation numbers blindly (a hallucinated [7] against 3 real passages
    must not turn into an IndexError, or a citation to nothing).

    Matches both a single citation ("[1]") and a grouped list the model
    writes despite the prompt asking for one number per bracket
    (live-observed 2026-09-30: "[1, 2, 4]") - handling that shape here is
    more robust than trusting every model to follow the format instruction
    exactly every time."""
    seen: list[int] = []
    for group in re.finditer(r"\[([\d,\s]+)\]", answer_text):
        for raw in group.group(1).split(","):
            raw = raw.strip()
            if not raw.isdigit():
                continue
            n = int(raw)
            if 1 <= n <= passage_count and n not in seen:
                seen.append(n)
    return seen


def _source_dict(passage: Passage) -> dict:
    return {"title": passage.title, "url": passage.url, "section": passage.section, "license": passage.license}


async def _answer_node(state: TripState) -> TripState:
    """RAG Q&A (app/rag/) - reached for intent "question" (routed straight
    here from slot_fill, no plan ever attempted) or "both" (routed here
    from _route_after_verify, once a real plan already exists). Retrieval
    is deterministic; the one LLM call is grounded strictly in what it
    returned. Never raises: a knowledge-base problem degrades to a plain
    "I don't know" rather than failing the whole turn - on a "both" turn
    there's a real plan riding on this same response."""
    question = state.question or state.user_input

    # A "both" turn already resolved a district via orchestrate; a bare
    # "question" turn never ran orchestrate at all, so try the destination
    # directly if one was named ("is Kandy safe at night?"). Neither
    # existing is fine too - retrieve() with district_id=None searches
    # every district plus national-level content, which is the right
    # default for a general question ("do I need a visa?").
    district_id = (state.trip_context or {}).get("district_id")
    if district_id is None and state.destination:
        try:
            place = await resolve_place(state.destination)
            district_id = place.get("district_id") if place else None
        except Exception as e:
            logger.warning(f"answer: destination resolution failed, searching all districts: {e}")

    try:
        passages = await retrieve_passages(question, district_id=district_id)
    except Exception as e:
        logger.warning(f"answer: retrieval failed: {e}")
        passages = []

    top_score = best_vector_score(passages)
    if not passages or (top_score is not None and top_score < settings.rag_min_score):
        # No passages, or the best one isn't a confident match - answering
        # anyway would mean paraphrasing weak/irrelevant material into
        # something that reads more authoritative than it is.
        state.answer = "I don't have reliable information on that."
        state.completed_steps.append("answer")
        return state

    numbered = "\n\n".join(
        f"[{i}] {p.title}" + (f" — {p.section}" if p.section else "") + f":\n{p.content}"
        for i, p in enumerate(passages, start=1)
    )
    prompt = get_prompt("answer")
    human = enforce_max_input_chars(prompt, f"Question: {question}\n\nPassages:\n{numbered}")

    try:
        structured_llm = get_llm("answer").with_structured_output(prompt.output_schema)
        result: AnswerOutput = await structured_llm.ainvoke([("system", prompt.system), ("human", human)])
        answer_text = result.answer
    except Exception as e:
        # Deterministic fallback: the strongest single passage, verbatim,
        # with its own citation - degrades quality, never availability.
        logger.warning(f"answer: LLM call failed, falling back to the top passage verbatim: {e}")
        answer_text = f"{passages[0].content} [1]"

    cited = _cited_passage_indices(answer_text, len(passages))
    state.answer = answer_text
    state.sources = [_source_dict(passages[i - 1]) for i in cited] or [_source_dict(passages[0])]
    state.completed_steps.append("answer")
    return state


async def _respond_node(state: TripState) -> TripState:

    if state.clarification_needed:
        state.final_response = state.clarification_needed
        # Live-found 2026-09-06: on a follow-up, slot_filling.py deliberately
        # lets a newly-mentioned destination overwrite the carried-over one
        # ("actually let's go to Paris instead" is a real feature) - but
        # when that new destination gets rejected (out-of-country, or any
        # other clarification trigger), the PREVIOUS turn's itinerary was
        # still sitting on state from session carry-over, and nothing
        # cleared it. The response ended up presenting last turn's plan
        # under the new (rejected) destination's name - e.g. "New York - 2
        # days" showing Matara's actual stops. A clarification response
        # should never carry a stale plan under whatever destination the
        # user just asked about.
        state.itinerary = []
        state.estimated_cost = None
        state.budget_notes = None
        state.plan_source = None
        state.completed_steps.append("respond")
        return state

    hard_errors = [e for e in state.errors if not e.startswith(_SOFT_ERROR_PREFIXES)]
    soft_notes = [e for e in state.errors if e.startswith(_SOFT_ERROR_PREFIXES)]

    if state.intent == "question" and state.followup_scope != "informational":
        # (An informational follow-up - budget/weather about the existing
        # plan - is also tagged intent="question" by the slot-filling LLM,
        # but is answered from state below, not by the RAG answer node.)
        # A pure question never builds a plan - has_real_content below is
        # always False for it, which would otherwise read as "the planner
        # failed" and ask "which destination?" instead of answering.
        state.final_response = state.answer or "I don't have reliable information on that."
        state.completed_steps.append("respond")
        return state

    # A day entry with an empty items list is not a plan - live-found
    # 2026-09-06: a follow-up that lost its destination mid-conversation
    # produced itinerary=[{"day": 1, "items": [], "day_cost": 0.0}] with no
    # hard_errors logged, and the old `hard_errors and not state.itinerary`
    # check let that fall through to "Here's your trip plan... 0 day(s)
    # planned" - claiming success on an empty plan is worse than the
    # clarifying question this should have asked instead.
    has_real_content = any(day.get("items") for day in state.itinerary)

    if not has_real_content:
        if hard_errors:
            state.final_response = "Sorry, I ran into an issue: " + "; ".join(hard_errors)
        elif not state.destination:
            state.final_response = "I wasn't able to put together a plan - which destination would you like to visit?"
        else:
            state.final_response = (
                f"I couldn't find enough options to plan a trip to {state.destination} right now. "
                "Could you try adjusting the destination, dates, or budget?"
            )
    elif state.followup_scope == "informational":
        state.final_response = (
            _weather_text(state) if state.followup_info == "weather" else _budget_breakdown_text(state)
        )
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
        # Only notes a traveller can act on reach the reply. How the plan was
        # assembled (planner_day_assembly, llm_plan_rejected, fallback_reason,
        # hallucinated-drop) is a developer diagnostic - still in `errors`
        # and the API response, just not in the chat bubble.
        traveller_notes = [
            n for n in soft_notes if n.startswith(("location_unresolved", "profile_unavailable", "safety_note"))
        ]
        if traveller_notes:
            state.final_response += "\n\nNote: " + "; ".join(traveller_notes)

    # intent="both" ("plan Kandy, and any scams to watch out for?") owes
    # the question half too, regardless of which branch above ran - even a
    # failed plan attempt still leaves a real question to answer.
    if state.intent == "both" and state.answer:
        state.final_response += f"\n\n{state.answer}"

    state.completed_steps.append("respond")
    return state


def _route_after_validate(state: TripState) -> str:
    return "policy" if not state.errors else "respond"


def _route_after_policy(state: TripState) -> str:
    return "slot_fill" if not state.errors else "respond"


def _route_after_slot_fill(state: TripState) -> str:
    if state.clarification_needed:
        return "respond"
    # Weather questions - a fresh one ("will it rain tomorrow in Colombo?")
    # or a follow-up about the planned trip ("will it rain on those days?")
    # - are answered from the live forecast, never by re-planning.
    if state.intent == "weather" or (
        state.is_followup and state.followup_scope == "informational" and state.followup_info == "weather"
    ):
        return "weather"
    # A question about the existing plan is answered from the plan itself.
    # Routing it anywhere else rebuilds the itinerary, which is how "show
    # budget breakdown" used to come back with different stops and a
    # different total - and burned a full planning cycle to do it.
    if state.is_followup and state.followup_scope == "informational" and state.itinerary:
        return "respond"
    # A PURE travel question ("do I need a visa?") never builds or changes
    # a plan - routing it into orchestrate/recommend/plan would burn a full
    # planning cycle to answer something that doesn't need one, the exact
    # mistake the informational-followup case above already exists to
    # avoid. "both" ("plan Kandy AND is tap water safe?") still needs a
    # real plan, so it falls through to the normal pipeline below and gets
    # its answer appended later - see _route_after_verify.
    if state.intent == "question":
        return "answer"
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
        # intent="both" ("plan 2 days in Kandy, and are there scams to
        # watch out for?") still owes an answer to the question half, once
        # the plan half is done - whichever path got here (a clean LLM
        # plan, a repaired one, or fallback's deterministic build, which
        # also routes through this same edge). "question"-only never
        # reaches this node at all (routed straight to "answer" from
        # slot_fill), and "plan" has nothing for _answer_node to do.
        return "answer" if state.intent == "both" else "respond"
    # No-progress guard (2026-09-26): a repair that reproduces the EXACT
    # same failure set as the attempt right before it didn't fix anything -
    # most often a systematic failure (e.g. the plan_source literal finding
    # in docs/ITINERARY_QUALITY_AND_TOKEN_PLAN.md) that a further identical
    # attempt will just repeat. Only meaningful once at least one repair has
    # actually run (repair_attempts > 0); previous_validation_failures is
    # empty before that, which would otherwise vacuously match an equally
    # empty list and never does since validation_failures is non-empty here.
    if state.repair_attempts > 0 and sorted(state.validation_failures) == sorted(state.previous_validation_failures):
        return "fallback"
    if state.repair_attempts >= settings.max_repair_attempts:
        return "fallback"
    return "repair"


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
    graph.add_node("answer", _answer_node)
    graph.add_node("weather", _weather_node)
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
    graph.add_edge("answer", "respond")
    graph.add_edge("weather", END)
    graph.add_edge("repair", "verify")
    graph.add_edge("fallback", "verify")
    graph.add_edge("respond", END)

    return graph.compile()


orchestrator = build_orchestrator_graph()
