# tests/test_react.py
# The shared ReAct executor (app/core/react.py) - no real LLM, no real
# tools. A fake chat model stands in for bind_tools()/ainvoke()/
# with_structured_output(), and fake StructuredTools return canned
# observations, so this tests the LOOP itself: bounded steps, tool budget
# with caching, wall-clock timeout, and the "every exit gets a structured
# answer" finalization guarantee - not any real agent's behaviour.

import asyncio
import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool
from pydantic import BaseModel

from app.config.settings import settings
from app.core.react import ReActConfig, ReActError, run_react


class _Answer(BaseModel):
    value: str


def _tool_call(name: str, args: dict, call_id: str) -> dict:
    return {"name": name, "args": args, "id": call_id}


class _FakeStructuredLLM:
    """What llm.with_structured_output(schema) returns - a runnable whose
    ainvoke() produces the final answer object directly."""

    def __init__(self, answer: _Answer = None, raises: Exception = None):
        self._answer = answer
        self._raises = raises
        self.calls = 0
        self.last_messages = None

    async def ainvoke(self, messages):
        self.calls += 1
        self.last_messages = messages
        if self._raises:
            raise self._raises
        return self._answer or _Answer(value="finalized")


class _FakeBoundLLM:
    """What llm.bind_tools(tools) returns. `turns` is a list of AIMessages
    to return in sequence, one per ainvoke() call - lets a test script
    exactly what the "model" does turn by turn."""

    def __init__(self, turns: list):
        self._turns = list(turns)
        self.calls = 0

    async def ainvoke(self, messages):
        self.calls += 1
        if not self._turns:
            raise AssertionError("bound LLM called more times than scripted")
        turn = self._turns.pop(0)
        if isinstance(turn, Exception):
            raise turn
        return turn


class _FakeLLM:
    """The top-level llm object passed to run_react - hands out the two
    sub-runnables above."""

    def __init__(self, turns: list, final_answer: _Answer = None, final_raises: Exception = None):
        self._bound = _FakeBoundLLM(turns)
        self._structured = _FakeStructuredLLM(answer=final_answer, raises=final_raises)

    def bind_tools(self, tools):
        return self._bound

    def with_structured_output(self, schema):
        return self._structured

    async def ainvoke(self, messages):
        # Real BaseChatModel instances always have a bare ainvoke() too -
        # run_react skips bind_tools() entirely when no tools are given
        # (app/core/react.py), so this fake must behave the same way a
        # real, un-bound chat model would.
        return await self._bound.ainvoke(messages)


def _make_tool(name: str, handler=None, sleep_s: float = 0.0):
    async def _impl(**kwargs):
        if sleep_s:
            await asyncio.sleep(sleep_s)
        if handler:
            return handler(**kwargs)
        return {"ok": True, "echo": kwargs}

    class _Args(BaseModel):
        x: int = 0

    return StructuredTool.from_function(coroutine=_impl, name=name, args_schema=_Args, description="test tool")


# ─────────────────────────── config default ─────────────────────────────────

def test_max_steps_defaults_to_the_single_settings_knob(monkeypatch):
    # This is what makes settings.react_max_steps (.env's REACT_MAX_STEPS)
    # the one real place to change every agent's step cap - no app/agents/
    # call site hardcodes its own max_steps, so they all pick this up.
    monkeypatch.setattr(settings, "react_max_steps", 3)
    assert ReActConfig().max_steps == 3

    monkeypatch.setattr(settings, "react_max_steps", 9)
    assert ReActConfig().max_steps == 9


def test_max_steps_can_still_be_overridden_explicitly():
    assert ReActConfig(max_steps=1).max_steps == 1


def test_tool_budget_defaults_to_the_single_settings_knob(monkeypatch):
    monkeypatch.setattr(settings, "react_tool_budget", 12)
    assert ReActConfig().tool_budget == 12

    monkeypatch.setattr(settings, "react_tool_budget", 4)
    assert ReActConfig().tool_budget == 4


# ─────────────────────────── happy path: no tools needed ───────────────────

async def test_answers_immediately_when_llm_calls_no_tools():
    final_msg = AIMessage(content="here you go", tool_calls=[])
    llm = _FakeLLM(turns=[final_msg], final_answer=_Answer(value="done"))

    result = await run_react(llm, tools=[], messages=[SystemMessage(content="sys")], output_schema=_Answer)

    assert result.stopped_by == "answer"
    assert result.output.value == "done"
    assert result.steps_used == 1
    assert result.tools_used == []


# ─────────────────────────── tool calling ───────────────────────────────────

async def test_calls_a_tool_then_answers():
    step1 = AIMessage(content="", tool_calls=[_tool_call("search", {"x": 1}, "call-1")])
    step2 = AIMessage(content="", tool_calls=[])
    llm = _FakeLLM(turns=[step1, step2], final_answer=_Answer(value="found it"))
    tool = _make_tool("search")

    result = await run_react(llm, tools=[tool], messages=[SystemMessage(content="sys")], output_schema=_Answer)

    assert result.stopped_by == "answer"
    assert result.steps_used == 2
    assert result.tools_used == ["search"]
    assert result.trace[0].tool_calls[0].tool == "search"
    assert result.trace[0].tool_calls[0].observation == {"ok": True, "echo": {"x": 1}}


async def test_parallel_tool_calls_in_one_step_run_together():
    step1 = AIMessage(content="", tool_calls=[
        _tool_call("search", {"x": 1}, "call-1"),
        _tool_call("weather", {"x": 2}, "call-2"),
    ])
    step2 = AIMessage(content="", tool_calls=[])
    llm = _FakeLLM(turns=[step1, step2], final_answer=_Answer(value="ok"))
    tools = [_make_tool("search"), _make_tool("weather")]

    result = await run_react(llm, tools=tools, messages=[SystemMessage(content="sys")], output_schema=_Answer)

    assert set(result.tools_used) == {"search", "weather"}
    assert len(result.trace[0].tool_calls) == 2


async def test_repeated_identical_tool_call_is_cached_and_free():
    step1 = AIMessage(content="", tool_calls=[_tool_call("search", {"x": 1}, "call-1")])
    step2 = AIMessage(content="", tool_calls=[_tool_call("search", {"x": 1}, "call-2")])  # same args again
    step3 = AIMessage(content="", tool_calls=[])
    llm = _FakeLLM(turns=[step1, step2, step3], final_answer=_Answer(value="ok"))
    calls = {"n": 0}

    def handler(**kwargs):
        calls["n"] += 1
        return {"n": calls["n"]}

    tool = _make_tool("search", handler=handler)
    # Budget is checked once per LLM turn, before that turn's calls are
    # known to be cache hits or not (app/core/react.py's pre-turn gate) -
    # so a budget of 2 (not 1) is what actually proves the SECOND, cached
    # call didn't consume a real budget slot: budget_used stays at 1
    # through both turns, well under 2, only because the repeat was free.
    config = ReActConfig(max_steps=6, tool_budget=2)

    result = await run_react(llm, tools=[tool], messages=[SystemMessage(content="sys")],
                              output_schema=_Answer, config=config)

    assert calls["n"] == 1   # the real tool only actually ran once
    assert result.trace[1].tool_calls[0].cached is True
    assert result.stopped_by == "answer"


async def test_unknown_tool_name_becomes_an_observation_not_a_crash():
    step1 = AIMessage(content="", tool_calls=[_tool_call("does_not_exist", {}, "call-1")])
    step2 = AIMessage(content="", tool_calls=[])
    llm = _FakeLLM(turns=[step1, step2], final_answer=_Answer(value="ok"))

    result = await run_react(llm, tools=[], messages=[SystemMessage(content="sys")], output_schema=_Answer)

    assert result.trace[0].tool_calls[0].error is not None
    assert result.stopped_by == "answer"


async def test_tool_that_raises_becomes_an_observation_not_a_crash():
    async def _boom(**kwargs):
        raise RuntimeError("tool exploded")

    class _Args(BaseModel):
        x: int = 0

    tool = StructuredTool.from_function(coroutine=_boom, name="boom", args_schema=_Args, description="")
    step1 = AIMessage(content="", tool_calls=[_tool_call("boom", {}, "call-1")])
    step2 = AIMessage(content="", tool_calls=[])
    llm = _FakeLLM(turns=[step1, step2], final_answer=_Answer(value="ok"))

    result = await run_react(llm, tools=[tool], messages=[SystemMessage(content="sys")], output_schema=_Answer)

    assert "tool exploded" in result.trace[0].tool_calls[0].error
    assert result.stopped_by == "answer"


# ─────────────────────────── bounds ──────────────────────────────────────────

async def test_max_steps_reached_still_produces_an_answer():
    always_calls_tool = AIMessage(content="", tool_calls=[_tool_call("search", {"x": 1}, "call-1")])
    llm = _FakeLLM(turns=[always_calls_tool] * 3, final_answer=_Answer(value="best effort"))
    tool = _make_tool("search")
    config = ReActConfig(max_steps=3, tool_budget=100)

    result = await run_react(llm, tools=[tool], messages=[SystemMessage(content="sys")],
                              output_schema=_Answer, config=config)

    assert result.stopped_by == "max_steps"
    assert result.steps_used == 3
    assert result.output.value == "best effort"


async def test_tool_budget_exhausted_stops_before_max_steps():
    always_calls_tool = AIMessage(content="", tool_calls=[
        _tool_call("search", {"x": i}, f"call-{i}") for i in range(3)
    ])
    llm = _FakeLLM(turns=[always_calls_tool] * 6, final_answer=_Answer(value="ok"))
    tool = _make_tool("search")
    config = ReActConfig(max_steps=6, tool_budget=3)   # exhausted after the first step's 3 distinct calls

    result = await run_react(llm, tools=[tool], messages=[SystemMessage(content="sys")],
                              output_schema=_Answer, config=config)

    assert result.stopped_by == "tool_budget"
    assert result.steps_used == 1


async def test_wall_clock_timeout_stops_the_loop():
    # Distinct args per step (x=i) - otherwise every call after the first is
    # served from run_react's own cache and the sleep never happens again,
    # which would defeat this test (a real regression caught while writing it).
    slow_tool = _make_tool("slow", sleep_s=0.05)
    turns = [
        AIMessage(content="", tool_calls=[_tool_call("slow", {"x": i}, f"call-{i}")])
        for i in range(100)
    ]
    llm = _FakeLLM(turns=turns, final_answer=_Answer(value="ok"))
    config = ReActConfig(max_steps=100, tool_budget=1000, wall_clock_s=0.08)

    result = await run_react(llm, tools=[slow_tool], messages=[SystemMessage(content="sys")],
                              output_schema=_Answer, config=config)

    assert result.stopped_by == "timeout"


async def test_llm_turn_error_still_produces_an_answer():
    llm = _FakeLLM(turns=[RuntimeError("provider down")], final_answer=_Answer(value="degraded"))

    result = await run_react(llm, tools=[], messages=[SystemMessage(content="sys")], output_schema=_Answer)

    assert result.stopped_by == "error"
    assert result.output.value == "degraded"


# ─────────────────────────── the finalization guarantee ────────────────────

async def test_finalization_failure_after_a_normal_stop_raises_reacterror():
    final_msg = AIMessage(content="", tool_calls=[])
    llm = _FakeLLM(turns=[final_msg], final_raises=RuntimeError("structured output rejected"))

    with pytest.raises(ReActError):
        await run_react(llm, tools=[], messages=[SystemMessage(content="sys")], output_schema=_Answer)


async def test_finalization_trims_large_item_lists_in_observations():
    # Regression: found live against real Gemini 2026-09-02 - a
    # finalization call carrying ~120 real candidate items (~40KB) got a
    # bare 400 INVALID_ARGUMENT from Gemini's structured-output endpoint,
    # even with the two other fixes in place; a handful of items succeeded.
    # The loop itself still sees the FULL result (this only shrinks what
    # goes into the finalization summary).
    many_items = {"items": [{"id": str(i)} for i in range(50)], "total": 50, "truncated": False}
    step1 = AIMessage(content="", tool_calls=[_tool_call("search", {"x": 1}, "call-1")])
    step2 = AIMessage(content="", tool_calls=[])
    llm = _FakeLLM(turns=[step1, step2], final_answer=_Answer(value="ok"))
    tool = _make_tool("search", handler=lambda **kw: many_items)

    result = await run_react(llm, tools=[tool], messages=[SystemMessage(content="sys")], output_schema=_Answer)

    assert result.trace[0].tool_calls[0].observation == many_items   # the loop's own record is untouched
    sent = llm._structured.last_messages
    payload = json.loads(sent[-2].content)   # -1 is the "no tools" nudge, -2 is the observations summary
    trimmed = payload["tool_observations"][0]["observation"]
    assert len(trimmed["items"]) == 15
    assert "note" in trimmed


async def test_finalization_is_attempted_exactly_once_regardless_of_stop_reason():
    llm = _FakeLLM(turns=[RuntimeError("boom")], final_answer=_Answer(value="ok"))

    await run_react(llm, tools=[], messages=[SystemMessage(content="sys")], output_schema=_Answer)

    assert llm._structured.calls == 1


async def test_finalization_never_replays_raw_tool_call_transcript():
    # Regression: found live against real Gemini 2026-09-02, two ways.
    # (1) A with_structured_output() call whose message list ends on the
    #     model's own final AIMessage (exactly what "stopped_by=answer"
    #     leaves behind) was rejected outright ("model prefilling ... must
    #     be a user message or a function response").
    # (2) Even mid-transcript, sending prior AIMessage.tool_calls/
    #     ToolMessage turns to a toolless runnable (with_structured_output
    #     binds no tools) produced a bare 400 INVALID_ARGUMENT - the model
    #     has no declared functions to reconcile those turns against.
    # Both are why finalization now sends a flat JSON summary instead of
    # the raw mutated transcript - this checks that promise structurally.
    step1 = AIMessage(content="", tool_calls=[_tool_call("search", {"x": 1}, "call-1")])
    step2 = AIMessage(content="here's my answer", tool_calls=[])
    llm = _FakeLLM(turns=[step1, step2], final_answer=_Answer(value="done"))
    tool = _make_tool("search")

    await run_react(llm, tools=[tool], messages=[SystemMessage(content="sys")], output_schema=_Answer)

    sent_messages = llm._structured.last_messages
    assert not any(isinstance(m, AIMessage) for m in sent_messages)
    assert not any(isinstance(m, ToolMessage) for m in sent_messages)
    assert isinstance(sent_messages[-1], HumanMessage)
