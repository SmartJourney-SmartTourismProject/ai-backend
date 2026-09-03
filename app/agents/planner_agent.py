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
from app.core.planner_shared import build_planner_human_message
from app.core.result import AgentResult
from app.core.state import TripState
from app.models.schemas import PlannerOutput
from app.prompts import get_prompt
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


class PlannerAgent(BaseAgent):
    name = "planner"

    async def execute(self, state: TripState) -> AgentResult:
        cost_table = await _fetch_cost_table()
        tools = build_planning_tools(cost_table)

        spec = get_prompt("planner")
        human = build_planner_human_message(state)
        messages = [SystemMessage(content=spec.system), HumanMessage(content=human)]

        try:
            result = await run_react(
                llm=get_llm("plan"), tools=tools, messages=messages,
                output_schema=PlannerOutput, config=ReActConfig(),
                finalize_system=PLANNER_FINALIZE_SYSTEM,
            )
        except Exception as e:
            # Broadened beyond ReActError - see orchestrator_agent.py's
            # identical fix for why (Phase 8, scenario 11: get_llm() itself
            # can raise before run_react is ever entered, and that must
            # degrade the same way a real ReAct failure does, not crash
            # the request).
            logger.warning(f"PlannerAgent failed: {e}")
            state.errors.append(f"planner_failed: {e}")
            return AgentResult(success=False, error=str(e))

        output: PlannerOutput = result.output
        state.planner_output = output.model_dump()
        state.itinerary = [d.model_dump() for d in output.itinerary]
        state.estimated_cost = output.estimated_cost
        state.budget_notes = output.budget_notes
        state.plan_source = "llm"

        state.react_traces["planner"] = {
            "steps_used": result.steps_used, "tools_used": result.tools_used,
            "stopped_by": result.stopped_by,
        }
        return AgentResult(success=True, message=f"{len(state.itinerary)} day(s) planned")
