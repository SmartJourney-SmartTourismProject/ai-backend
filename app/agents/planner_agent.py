"""
The Planner agent (AGENT_ARCHITECTURE.md §3.4) - sets the SHAPE of the trip
(how many items/day from pace, which day gets which theme, weather-driven
day swaps) and hands that as constraints to build_day_plan, which does all
routing/timing/arithmetic deterministically. The planner never computes a
cost, travel time, or stop sequence itself.
"""
from __future__ import annotations

import logging

from langchain_core.messages import HumanMessage, SystemMessage

from app.core.base_agent import BaseAgent
from app.core.budget import CostReferenceTable
from app.core.llm import get_llm
from app.core.react import ReActConfig, run_react
from app.core.planner_shared import (
    assemble_planner_days, build_planner_human_message, enforce_budget_notes, fill_missing_days,
    resolve_day_context, resolve_planner_max_steps,
)
from app.core.result import AgentResult
from app.core.state import TripState
from app.models.schemas import PlannerOutput
from app.prompts import get_prompt
from app.prompts._base import enforce_max_input_chars
from app.prompts.planner_prompt import PLANNER_FINALIZE_SYSTEM
from app.tools.registry import build_planning_tools
from app.utils.db_pool import get_pool

logger = logging.getLogger(__name__)


async def _fetch_cost_table() -> CostReferenceTable:
    """Same fetch app/core/fallback.py's build_plan() already does for the
    zero-LLM path - duplicated rather than shared, since this one is a
    small, self-contained read with its own failure mode (degrade to an
    empty table, never raise) and refactoring tested fallback code for a
    second caller isn't worth the risk here."""
    cost_table: CostReferenceTable = {}
    try:
        pool = await get_pool()
    except Exception as e:
        logger.warning(f"planner_agent: get_pool() failed unexpectedly, degrading to empty table: {e}")
        return cost_table
    if pool is None:
        return cost_table
    try:
        rows = await pool.fetch(
            "SELECT district_id, category, price_level, unit, typical_cost, currency FROM cost_reference"
        )
        cost_table = {
            (str(r["district_id"]) if r["district_id"] else None, r["category"], r["price_level"]):
                {"unit": r["unit"], "typical_cost": r["typical_cost"], "currency": r["currency"]}
            for r in rows
        }
    except Exception as e:
        logger.warning(f"planner_agent: cost_reference fetch failed, degrading to empty table: {e}")
    return cost_table


async def _fetch_outdoor_tags() -> frozenset[str]:
    """Same fetch app/core/fallback.py's build_plan() already does - see
    _fetch_cost_table's own docstring for why this is a deliberate
    duplicate rather than a shared helper. Needed now (B1,
    AI_BACKEND_OPTIMIZATION_PLAN.md) because build_day_plan's outdoor_tags
    argument was dropped: the model had no real way to know the actual tag
    vocabulary anyway, so it's resolved here and closed over instead."""
    try:
        pool = await get_pool()
    except Exception as e:
        logger.warning(f"planner_agent: get_pool() failed unexpectedly, degrading to no outdoor filtering: {e}")
        return frozenset()
    if pool is None:
        return frozenset()
    try:
        rows = await pool.fetch("SELECT tag FROM tag_vocabulary WHERE is_outdoor = true")
        return frozenset(r["tag"] for r in rows)
    except Exception as e:
        logger.warning(f"planner_agent: tag_vocabulary fetch failed, degrading to no outdoor filtering: {e}")
        return frozenset()


def _build_item_store(state: TripState) -> dict[str, dict]:
    """id -> full item dict, from the recommendation agent's own selected
    lists (already merged with real candidate data - see
    recommendation_agent.py's _flat()). This is what lets build_day_plan/
    estimate_costs take ids instead of forcing the model to re-emit full
    item dicts as output tokens (B1) - the server already has this data,
    it just wasn't being reused."""
    items = state.hotels + state.restaurants + state.attractions + state.events
    return {item["id"]: item for item in items if "id" in item}


class PlannerAgent(BaseAgent):
    name = "planner"

    async def execute(self, state: TripState) -> AgentResult:
        cost_table = await _fetch_cost_table()
        outdoor_tags = await _fetch_outdoor_tags()
        district_id = (state.trip_context or {}).get("district_id")
        day_context = resolve_day_context(state)
        tools = build_planning_tools(cost_table, _build_item_store(state), outdoor_tags, district_id, day_context)

        spec = get_prompt("planner")
        human = enforce_max_input_chars(spec, build_planner_human_message(state))
        messages = [SystemMessage(content=spec.system), HumanMessage(content=human)]

        try:
            result = await run_react(
                llm=get_llm("plan"), tools=tools, messages=messages,
                output_schema=PlannerOutput,
                config=ReActConfig(max_steps=resolve_planner_max_steps(state.duration_days)),
                finalize_system=PLANNER_FINALIZE_SYSTEM,
            )
        except Exception as e:
            # Broadened beyond ReActError - see recommendation_agent.py's
            # identical fix for why (Phase 8, scenario 11: get_llm() itself
            # can raise before run_react is ever entered, and that must
            # degrade the same way a real ReAct failure does, not crash
            # the request).
            logger.warning(f"PlannerAgent failed: {e}")
            state.errors.append(f"planner_failed: {e}")
            return AgentResult(success=False, error=str(e))

        output: PlannerOutput = result.output
        assembled_days, warnings, unbacked_days = assemble_planner_days(result.trace, output.itinerary)
        # unbacked_days (2026-09-26): a day assemble_planner_days accepted
        # from the model's OWN transcription, with no build_day_plan
        # observation behind it - fill_missing_days now rebuilds these the
        # same deterministic way it already rebuilds a genuinely missing
        # day, instead of letting the one day most likely to break a rule
        # sail through to validation completely unchecked.
        assembled_days, fill_warnings = fill_missing_days(
            state, assembled_days, cost_table, force_rebuild_days=unbacked_days,
        )
        warnings = warnings + fill_warnings
        if warnings:
            logger.warning(f"PlannerAgent day assembly: {'; '.join(warnings)}")
            state.errors.append(f"planner_day_assembly: {'; '.join(warnings)}")

        estimated_cost = round(sum(d.day_cost for d in assembled_days), 2)
        assembled_output = PlannerOutput(
            itinerary=assembled_days,
            estimated_cost=estimated_cost,
            # currency (Part 5, server-side enforcement): always LKR, never
            # the model's own claim - output_validator.py's currency rule
            # already requires this on every item; enforcing it at the plan
            # level too means a model that just forgets the field can no
            # longer fail that check over nothing more than an omission.
            currency="LKR",
            budget_notes=enforce_budget_notes(output.budget_notes, estimated_cost, state.budget),
        )
        state.planner_output = assembled_output.model_dump()
        state.itinerary = [d.model_dump() for d in assembled_days]
        state.estimated_cost = assembled_output.estimated_cost
        state.budget_notes = assembled_output.budget_notes
        state.plan_source = "llm"

        state.react_traces["planner"] = {
            "steps_used": result.steps_used, "tools_used": result.tools_used,
            "stopped_by": result.stopped_by,
        }
        return AgentResult(success=True, message=f"{len(state.itinerary)} day(s) planned")
