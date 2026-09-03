"""
The Orchestrator agent (AGENT_ARCHITECTURE.md §3.2) - turns a
half-specified request into a fully grounded TripContext. Nothing else: it
never recommends a place or builds an itinerary.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from langchain_core.messages import HumanMessage, SystemMessage

from app.core.base_agent import BaseAgent
from app.core.llm import get_llm
from app.core.react import ReActConfig, run_react
from app.core.result import AgentResult
from app.core.state import TripState
from app.models.schemas import TripContext
from app.prompts import get_prompt
from app.prompts.orchestrator_prompt import ORCHESTRATOR_FINALIZE_SYSTEM
from app.tools.registry import CONTEXT_TOOLS

logger = logging.getLogger(__name__)


class OrchestratorAgent(BaseAgent):
    name = "orchestrator"

    async def execute(self, state: TripState) -> AgentResult:
        spec = get_prompt("orchestrator")
        human = json.dumps({
            "today": datetime.now(timezone.utc).date().isoformat(),
            "destination": state.destination,
            "duration_days": state.duration_days,
            "start_location": state.start_location,
            "user_id": state.user_id,
            "trip_dates": state.trip_dates,
        })
        messages = [SystemMessage(content=spec.system), HumanMessage(content=human)]

        try:
            result = await run_react(
                llm=get_llm("orchestrator"), tools=CONTEXT_TOOLS, messages=messages,
                output_schema=TripContext, config=ReActConfig(),
                finalize_system=ORCHESTRATOR_FINALIZE_SYSTEM,
            )
        except Exception as e:
            # Catches ReActError (run_react's own failure) AND anything
            # get_llm() itself can raise (no provider configured, quota
            # exhausted before the first call even starts) - found live
            # (Phase 8, scenario 11): get_llm(...) is evaluated as part of
            # this call expression, inside the try, but a bare
            # `except ReActError` let a RuntimeError from get_llm() escape
            # uncaught, crashing the whole request with an unhandled 500 -
            # exactly what AGENT_ARCHITECTURE.md §6's degradation matrix
            # says must never happen for "Gemini quota exhausted / key
            # invalid". Never crashes the request; always degrades.
            logger.warning(f"OrchestratorAgent failed: {e}")
            state.errors.append(f"orchestrator_failed: {e}")
            return AgentResult(success=False, error=str(e))

        ctx: TripContext = result.output
        state.trip_context = ctx.model_dump()
        state.trip_dates = [{
            "start_date": ctx.date_window.start_date, "end_date": ctx.date_window.end_date,
        }]
        state.weather = {"forecast": [w.model_dump() for w in ctx.per_day_weather]}
        state.disaster = ctx.disaster.model_dump()
        if not state.start_location and ctx.start_location:
            state.start_location = ctx.start_location.model_dump()

        # safety_notes is deliberately NOT trusted to the LLM alone - found
        # live (Phase 8, golden scenario 8, 2026-09-03): the orchestrator
        # correctly fetched a real red-severity disaster observation into
        # `ctx.disaster` every time, but did not reliably also write a note
        # about it into `ctx.safety_notes` (the free-text field
        # ORCHESTRATOR_SYSTEM_PROMPT rule 5 asks it to fill) - meaning the
        # warning silently never reached the user despite the RULE ITSELF
        # saying "do not silently omit it". Computed deterministically here
        # from the same disaster data instead of depending on the model to
        # remember, matching this codebase's general rule: never let LLM
        # unreliability hide a safety-relevant signal that can be derived
        # from structured data already in hand.
        safety_notes = list(ctx.safety_notes)
        red_events = [e for e in ctx.disaster.active_events if e.severity == "red"]
        if red_events and not any("red-level hazard" in n for n in safety_notes):
            titles = ", ".join(e.title for e in red_events)
            safety_notes.append(f"Active red-level hazard(s) near your destination: {titles}.")
        if safety_notes:
            state.errors.extend(f"safety_note: {n}" for n in safety_notes)

        state.react_traces["orchestrator"] = {
            "steps_used": result.steps_used, "tools_used": result.tools_used,
            "stopped_by": result.stopped_by,
        }
        return AgentResult(success=True, message=f"resolved {ctx.destination_name} (district {ctx.district_id})")
