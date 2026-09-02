"""
The shared bounded ReAct executor (docs/master_plan/AGENT_ARCHITECTURE.md §2,
PROJECT_MASTER_PLAN.md Phase 6, project concern #2). One implementation,
reused by the orchestrator/recommendation/planner agents in app/agents/ -
before this module, none of those had an actual reasoning loop: they were
each a single `self.llm.ainvoke(prompt)` call with hand-rolled JSON parsing,
so "the model chose to call a tool, saw the result, and adjusted" was not
something this codebase could do at all.

Bounded by max_steps (LLM turns, not tool calls), tool_budget (total real
tool executions - a repeated identical call is served from this run's cache
and does not count), and wall_clock_s. Every exit is a valid state: whatever
stopped the loop (a real final answer, max_steps, tool_budget, timeout, or
an LLM error), the executor always makes ONE more call - the same messages,
no tools bound, through with_structured_output(output_schema) - to force a
structured answer from whatever was actually observed. Only if that last
call itself fails does run_react raise, and that's deliberate: it means the
caller (an app/agents/ node) has nothing usable and must degrade to
app/core/fallback.py, not silently return an empty/wrong plan.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Literal, Optional

from langchain_core.messages import BaseMessage, HumanMessage, ToolMessage
from langchain_core.tools import StructuredTool
from pydantic import BaseModel

from app.config.settings import settings

logger = logging.getLogger(__name__)

StopReason = Literal["answer", "max_steps", "tool_budget", "timeout", "error"]


@dataclass
class ReActConfig:
    # Default reads settings.react_max_steps (one place to change - .env's
    # REACT_MAX_STEPS, or settings.py's default - propagates to every agent
    # that doesn't explicitly override it; none of app/agents/ do). A
    # default_factory (not a bare `= settings.react_max_steps`) so this stays
    # live if settings is monkeypatched in a test, rather than freezing the
    # value in at class-definition time.
    max_steps: int = field(default_factory=lambda: settings.react_max_steps)   # LLM turns, not tool calls
    # Same single-knob pattern as max_steps (settings.react_tool_budget /
    # .env's REACT_TOOL_BUDGET) - total tool executions, cached repeats free.
    tool_budget: int = field(default_factory=lambda: settings.react_tool_budget)
    wall_clock_s: float = 25.0
    per_tool_timeout_s: float = 8.0


@dataclass
class ToolCallTrace:
    tool: str
    args: dict
    observation: Any
    cached: bool = False
    error: Optional[str] = None


@dataclass
class TraceStep:
    step: int
    ai_content: str = ""
    tool_calls: list[ToolCallTrace] = field(default_factory=list)


@dataclass
class ReActResult:
    output: BaseModel
    trace: list[TraceStep]
    steps_used: int
    tools_used: list[str]
    stopped_by: StopReason


class ReActError(Exception):
    """Raised only when even the no-tools structured-answer fallback failed -
    the caller has no STRUCTURED output and must degrade (app/core/fallback.py),
    not retry run_react again. Carries `trace` (steps_used/stopped_by/tools_used
    are on the exception too) so a caller can still salvage whatever real tool
    observations the loop DID gather before the final call failed - Phase 8
    found live that discarding this on failure meant the fallback planner had
    to work with empty candidate lists, producing an item-less plan, on the
    single most common failure path in this codebase today (see TODO.md)."""

    def __init__(self, message: str, trace: Optional[list["TraceStep"]] = None,
                 tools_used: Optional[list[str]] = None, stopped_by: Optional[str] = None):
        super().__init__(message)
        self.trace = trace or []
        self.tools_used = tools_used or []
        self.stopped_by = stopped_by


def _cache_key(name: str, args: dict) -> tuple[str, str]:
    return name, json.dumps(args, sort_keys=True, default=str)


# Every db_search_* tool (app/tools/registry.py) returns {"items": [...],
# "total": int, "truncated": bool} - shared enough across this codebase's
# tools to trim generically here without react.py needing to know what a
# "listing" or "event" actually is. Found live 2026-09-02: a finalization
# call carrying ~120 real candidate items (~40KB of JSON) got a bare 400
# INVALID_ARGUMENT from Gemini's structured-output endpoint, while the same
# call with a handful of items succeeded - the schema-constrained decoding
# path has a real, much tighter payload limit than the model's normal
# context window advertises. The agent already saw the untruncated result
# during the loop itself (it's what it reasoned and picked from); only the
# FINALIZATION copy needs shrinking, since that's what recreates the 400.
_MAX_OBSERVATION_ITEMS = 15


def _trim_observation(obs: Any) -> Any:
    if not isinstance(obs, dict) or not isinstance(obs.get("items"), list):
        return obs
    items = obs["items"]
    if len(items) <= _MAX_OBSERVATION_ITEMS:
        return obs
    return {**obs, "items": items[:_MAX_OBSERVATION_ITEMS],
            "note": f"trimmed from {len(items)} to {_MAX_OBSERVATION_ITEMS} items for the final answer call"}


async def _execute_tool_call(
    call: dict, tools_by_name: dict[str, StructuredTool], cache: dict[tuple[str, str], Any],
    per_tool_timeout_s: float,
) -> tuple[str, str, dict, Any, bool, Optional[str]]:
    """Runs one tool call, or serves it from `cache` if this run has already
    made an identical call. Never raises - a tool failure becomes an
    observation the LLM can see and react to, per every existing tool's own
    "never raises, returns a typed error" convention."""
    call_id = call.get("id") or ""
    name = call.get("name", "")
    args = call.get("args") or {}
    key = _cache_key(name, args)

    if key in cache:
        return call_id, name, args, cache[key], True, None

    tool = tools_by_name.get(name)
    if tool is None:
        err = f"unknown tool '{name}'"
        return call_id, name, args, {"error": err}, False, err

    try:
        obs = await asyncio.wait_for(tool.ainvoke(args), timeout=per_tool_timeout_s)
        cache[key] = obs
        return call_id, name, args, obs, False, None
    except asyncio.TimeoutError:
        err = f"tool '{name}' timed out after {per_tool_timeout_s}s"
        return call_id, name, args, {"error": err}, False, err
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        return call_id, name, args, {"error": err}, False, err


async def run_react(
    llm,
    tools: list[StructuredTool],
    messages: list[BaseMessage],
    output_schema: type[BaseModel],
    config: Optional[ReActConfig] = None,
) -> ReActResult:
    config = config or ReActConfig()
    tools_by_name = {t.name: t for t in tools}
    bound_llm = llm.bind_tools(tools) if tools else llm

    msgs: list[BaseMessage] = list(messages)
    trace: list[TraceStep] = []
    tools_used: list[str] = []
    tool_budget_used = 0
    cache: dict[tuple[str, str], Any] = {}
    start = time.monotonic()
    stopped_by: StopReason = "max_steps"

    turns_used = 0
    for step in range(1, config.max_steps + 1):
        if time.monotonic() - start > config.wall_clock_s:
            stopped_by = "timeout"
            break
        if tool_budget_used >= config.tool_budget:
            stopped_by = "tool_budget"
            break

        try:
            ai_msg = await bound_llm.ainvoke(msgs)
        except Exception as e:
            logger.warning(f"run_react: LLM turn {step} failed: {e}")
            stopped_by = "error"
            break

        turns_used = step
        msgs.append(ai_msg)
        tool_calls = list(getattr(ai_msg, "tool_calls", None) or [])

        if not tool_calls:
            stopped_by = "answer"
            break

        step_trace = TraceStep(
            step=step,
            ai_content=ai_msg.content if isinstance(ai_msg.content, str) else "",
        )

        results = await asyncio.gather(*[
            _execute_tool_call(call, tools_by_name, cache, config.per_tool_timeout_s)
            for call in tool_calls
        ])

        for call_id, name, args, obs, cached, err in results:
            if not cached:
                tool_budget_used += 1
            tools_used.append(name)
            step_trace.tool_calls.append(
                ToolCallTrace(tool=name, args=args, observation=obs, cached=cached, error=err)
            )
            msgs.append(ToolMessage(content=json.dumps(obs, default=str), tool_call_id=call_id))

        trace.append(step_trace)
    else:
        stopped_by = "max_steps"

    # The finalization call is built from the ORIGINAL messages plus a plain
    # summary of what the tools actually returned - never the raw mutated
    # `msgs` transcript (interleaved AIMessage.tool_calls / ToolMessage
    # turns). Two real, live-found reasons (2026-09-02, real Gemini calls):
    #   1. That transcript's last turn is the model's own AIMessage on the
    #      most common stop reason ("answer") - "model prefilling" isn't
    #      allowed even for a structured-output call.
    #   2. with_structured_output() here uses a DIFFERENT runnable (no tools
    #      bound) - sending it prior tool_calls/ToolMessage turns that
    #      reference function declarations it was never given produced a
    #      bare 400 INVALID_ARGUMENT with no further detail.
    # A flat JSON summary sidesteps both: it's always a HumanMessage (a real
    # user turn) and declares no function-call structure the model has to
    # reconcile against tools it doesn't have here.
    observations = [
        {"tool": c.tool, "args": c.args, "observation": _trim_observation(c.observation), "error": c.error}
        for step in trace for c in step.tool_calls
    ]
    # Also found live 2026-09-02, harder to spot: every agent's own system
    # prompt tells it it "MUST call" a specific tool before answering
    # (RECOMMENDATION_SYSTEM_PROMPT rule 2, PLANNER_SYSTEM_PROMPT rule 1,
    # etc.) - reused verbatim here, that instruction pushed the model to
    # attempt an actual tool/function call at THIS toolless call too. Gemini
    # rejected the whole request outright (400 INVALID_ARGUMENT, no further
    # detail); Groq's error was explicit - "attempted to call tool
    # 'score_candidates' which was not in request.tools". An explicit
    # "no tools here, don't try" instruction is what stops the model from
    # attempting one.
    finalize_msgs: list[BaseMessage] = list(messages)
    if observations:
        finalize_msgs.append(HumanMessage(content=json.dumps({"tool_observations": observations}, default=str)))
    finalize_msgs.append(HumanMessage(
        content="No tools are available in this message - do not attempt any tool or function call here. "
                "Using only the tool observations above (if any), return the final structured object "
                "described in your instructions."
    ))

    try:
        structured_llm = llm.with_structured_output(output_schema)
        output = await structured_llm.ainvoke(finalize_msgs)
    except Exception as e:
        raise ReActError(
            f"run_react could not produce a structured {output_schema.__name__} "
            f"even after the no-tools fallback (loop stopped_by={stopped_by}): {e}",
            trace=trace, tools_used=tools_used, stopped_by=stopped_by,
        ) from e

    return ReActResult(
        output=output, trace=trace, steps_used=turns_used,
        tools_used=tools_used, stopped_by=stopped_by,
    )
