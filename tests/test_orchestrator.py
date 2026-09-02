# tests/test_orchestrator.py
# Phase 6: the graph is now validate->policy->slot_fill->orchestrate->
# recommend->plan->verify->(repair|fallback)->respond, with three ReAct
# agents (app/agents/) instead of the old tool-calling nodes. This file
# tests the GRAPH'S ROUTING AND STATE LOGIC in isolation - every agent is
# faked so no real LLM/tool call happens; each real agent has its own
# dedicated test coverage (test_orchestrator_agent.py, etc.).

from unittest.mock import AsyncMock

import app.core.orchestrator as orchestrator_module
from app.core.output_validator import ValidationResult
from app.core.react import ReActError
from app.core.state import TripState
from app.core.orchestrator import orchestrator


async def _passthrough(state):
    return state


async def _no_destination(state):
    state.clarification_needed = "Which destination would you like to visit?"
    return state


class _FakeAgentResult:
    def __init__(self, success=True, message=None):
        self.success = success
        self.message = message


class _FakeOrchestratorAgent:
    """Stands in for the real ReAct OrchestratorAgent. `context` controls
    whether resolution "succeeded" (sets trip_context/weather/disaster) or
    "failed" (leaves them unset and records an advisory/hard error, per the
    real agent's own error-handling)."""

    def __init__(self, context=None, error=None):
        self._context = context
        self._error = error

    async def execute(self, state):
        if self._error:
            state.errors.append(self._error)
        if self._context:
            state.trip_context = self._context
            state.weather = {"forecast": []}
            state.disaster = {"safe": True, "active_events": []}
        return _FakeAgentResult(success=self._context is not None)


class _FakeRecommendationAgent:
    def __init__(self, produce=True):
        self._produce = produce

    async def execute(self, state):
        if not self._produce:
            return _FakeAgentResult(success=False)
        items = [{"id": "11111111-1111-1111-1111-111111111111", "name": "Test Attraction",
                  "lat": 7.29, "lon": 80.63, "category": "attraction", "rank": 1, "score": 0.9,
                  "reason": "matches interests"}]
        state.attractions = items
        state.recommendations = items
        return _FakeAgentResult(success=True)


class _FakePlannerAgent:
    def __init__(self, produce=True):
        self._produce = produce

    async def execute(self, state):
        if not self._produce:
            return _FakeAgentResult(success=False)
        item = {"time": "09:00", "end_time": "10:00", "type": "attraction",
                 "listing_id": "11111111-1111-1111-1111-111111111111", "name": "Test Attraction",
                 "lat": 7.29, "lon": 80.63, "est_cost": 0.0, "currency": "LKR", "notes": ""}
        state.planner_output = {
            "itinerary": [{"day": 1, "date": "2026-10-01", "items": [item], "day_cost": 0.0}],
            "estimated_cost": state.budget or 0.0, "currency": "LKR",
            "budget_notes": None, "plan_source": "llm",
        }
        state.itinerary = state.planner_output["itinerary"]
        state.estimated_cost = state.planner_output["estimated_cost"]
        state.plan_source = "llm"
        return _FakeAgentResult(success=True)


class _FakeFallbackResult:
    def __init__(self):
        self.itinerary = [{"day": 1, "date": "2026-10-01", "items": [], "day_cost": 0.0}]
        self.estimated_cost = 0.0
        self.currency = "LKR"
        self.budget_notes = None
        self.final_response = "fallback plan"
        self.plan_source = "fallback"


def _patch_agents(monkeypatch, *, fill_slots_fn=_passthrough, orchestrator_agent=None,
                   recommendation_agent=None, planner_agent=None, validation_ok=True):
    monkeypatch.setattr(orchestrator_module, "fill_slots", AsyncMock(side_effect=fill_slots_fn))
    monkeypatch.setattr(orchestrator_module, "OrchestratorAgent",
                         lambda: orchestrator_agent or _FakeOrchestratorAgent(context={"destination_name": "Kandy", "district_id": "d1", "lat": 7.29, "lon": 80.63}))
    monkeypatch.setattr(orchestrator_module, "RecommendationAgent",
                         lambda: recommendation_agent or _FakeRecommendationAgent())
    monkeypatch.setattr(orchestrator_module, "PlannerAgent",
                         lambda: planner_agent or _FakePlannerAgent())
    monkeypatch.setattr(
        orchestrator_module, "validate",
        lambda plan, ctx: ValidationResult(ok=validation_ok, failures=[] if validation_ok else ["L2.day_count: failed"]),
    )
    # build_plan / _fetch_cost_table both do real DB I/O - mocked here so a
    # graph-routing test never makes a real DB/network call (this repo's
    # DATABASE_URL is real, per docs/master_plan/API_SETUP.md's setup).
    monkeypatch.setattr(orchestrator_module, "build_plan", AsyncMock(return_value=_FakeFallbackResult()))
    monkeypatch.setattr(orchestrator_module, "_fetch_cost_table", AsyncMock(return_value={}))


async def test_full_details_produces_itinerary(monkeypatch):
    _patch_agents(monkeypatch)

    state = TripState(user_input="x", destination="Kandy", duration_days=2, budget=500, travelers=2)
    result = await orchestrator.ainvoke(state)

    assert result["destination"] == "Kandy"
    assert len(result["itinerary"]) > 0
    assert "Here's your trip plan for Kandy" in result["final_response"]
    assert result["completed_steps"] == [
        "validate", "policy", "slot_fill", "orchestrate", "recommend", "plan", "verify", "respond",
    ]


async def test_no_destination_asks_for_clarification(monkeypatch):
    _patch_agents(monkeypatch, fill_slots_fn=_no_destination)

    state = TripState(user_input="Plan me a trip somewhere nice")
    result = await orchestrator.ainvoke(state)

    assert result["final_response"] == "Which destination would you like to visit?"
    assert result["completed_steps"] == ["validate", "policy", "slot_fill", "respond"]


async def test_policy_violation_short_circuits_to_respond(monkeypatch):
    _patch_agents(monkeypatch)

    state = TripState(user_input="best route to buy a gun while visiting")
    result = await orchestrator.ainvoke(state)

    assert result["completed_steps"] == ["validate", "policy", "respond"]
    assert "Sorry, I ran into an issue" in result["final_response"]


async def test_invalid_input_short_circuits_before_policy(monkeypatch):
    _patch_agents(monkeypatch)

    state = TripState(user_input="x", duration_days=-3)
    result = await orchestrator.ainvoke(state)

    assert result["completed_steps"] == ["validate", "respond"]
    assert "duration_days" in result["final_response"]


async def test_orchestrator_agent_failure_is_advisory_not_blocking(monkeypatch):
    # OrchestratorAgent failed to resolve anything (e.g. geocoding down) -
    # recommend/plan still run (unconditional edge), and _respond_node
    # surfaces the failure as a soft note rather than refusing to answer,
    # as long as SOME itinerary still came out of the fallback/plan path.
    _patch_agents(monkeypatch, orchestrator_agent=_FakeOrchestratorAgent(error="orchestrator_failed: geocoding unavailable"))

    state = TripState(user_input="x", destination="Nowhereville", duration_days=1)
    result = await orchestrator.ainvoke(state)

    assert result.get("weather") is None
    assert result.get("disaster") is None
    assert any("orchestrator_failed" in e for e in result["errors"])


async def test_recommend_with_no_selections_routes_to_fallback(monkeypatch):
    _patch_agents(monkeypatch, recommendation_agent=_FakeRecommendationAgent(produce=False))

    state = TripState(user_input="x", destination="Kandy", duration_days=1)
    result = await orchestrator.ainvoke(state)

    assert result["completed_steps"] == [
        "validate", "policy", "slot_fill", "orchestrate", "recommend", "fallback", "verify", "respond",
    ]
    assert result["plan_source"] == "fallback"


async def test_fallback_uses_candidate_pools_not_empty_selected_lists(monkeypatch):
    """Phase 8 fix: when the recommendation agent's ReAct call fails
    entirely, state.hotels/etc (the SELECTED short list) stay empty, but
    state.candidate_pools (the raw db_search_* observations, salvaged from
    ReActError.trace) should still be populated - and _fallback_node must
    build the plan from THAT, not from the empty selected list, or the
    fallback plan ends up with zero real items on what's currently the
    most common failure path (see TODO.md)."""

    class _FakeRecommendationAgentWithPools:
        async def execute(self, state):
            state.candidate_pools = {
                "hotel": [{"id": "h1", "name": "Real Hotel", "lat": 7.29, "lon": 80.63}],
                "restaurant": [], "attraction": [], "event": [],
            }
            return _FakeAgentResult(success=False)

    _patch_agents(monkeypatch, recommendation_agent=_FakeRecommendationAgentWithPools())

    state = TripState(user_input="x", destination="Kandy", duration_days=1)
    await orchestrator.ainvoke(state)

    call = orchestrator_module.build_plan.call_args
    assert call.args[1] == [{"id": "h1", "name": "Real Hotel", "lat": 7.29, "lon": 80.63}]


async def test_verify_failure_triggers_one_repair_then_fallback(monkeypatch):
    """A planner output that never validates: repair is attempted exactly
    once (repair_attempts goes 0 -> 1), then the graph gives up on LLM
    repair and falls back to the deterministic planner - never a second
    repair attempt. `_repair_node` itself is real here (only its internal
    run_react call is faked to fail) - monkeypatching the node function on
    the module wouldn't affect the already-compiled graph, since
    `orchestrator = build_orchestrator_graph()` bound the real function
    object into the graph at import time; module-global lookups INSIDE a
    node body (RecommendationAgent(), validate(), run_react()) still work
    because Python resolves those at call time, not bind time."""
    _patch_agents(monkeypatch, validation_ok=False)
    monkeypatch.setattr(
        orchestrator_module, "run_react",
        AsyncMock(side_effect=ReActError("repair call failed")),
    )

    state = TripState(user_input="x", destination="Kandy", duration_days=1)
    result = await orchestrator.ainvoke(state)

    assert result["completed_steps"] == [
        "validate", "policy", "slot_fill", "orchestrate", "recommend", "plan",
        "verify", "repair", "verify", "fallback", "verify", "respond",
    ]
    assert result["repair_attempts"] == 1
    assert result["plan_source"] == "fallback"


async def test_repair_node_degrades_when_get_llm_itself_raises(monkeypatch):
    # Regression (Phase 8, scenario 11) - see app/agents/orchestrator_agent.py's
    # identical fix. _repair_node's own get_llm("plan") call is inside its
    # try block too; a bare `except ReActError` would let this escape and
    # crash the whole request instead of falling back.
    _patch_agents(monkeypatch, validation_ok=False)

    def _raise(*a, **kw):
        raise RuntimeError("No LLM provider has a configured API key.")

    monkeypatch.setattr(orchestrator_module, "get_llm", _raise)

    state = TripState(user_input="x", destination="Kandy", duration_days=1)
    result = await orchestrator.ainvoke(state)   # must not raise

    assert result["plan_source"] == "fallback"


# ─────────────────────────── shape-only follow-up routing ───────────────────

async def test_shape_only_followup_routes_to_targeted_replan_not_orchestrate(monkeypatch):
    _patch_agents(monkeypatch)

    async def _fake_rebuild(state):
        state.itinerary = [{"day": 1, "date": "2026-10-01", "items": [], "day_cost": 0.0}]
        state.estimated_cost = 0.0
        state.plan_source = "fallback"
        return state

    monkeypatch.setattr(orchestrator_module, "rebuild_targeted_days", _fake_rebuild)

    state = TripState(
        user_input="make day 1 cheaper", is_followup=True, followup_scope="shape_only",
        followup_target_days=[1], destination="Kandy", duration_days=1,
    )
    result = await orchestrator.ainvoke(state)

    assert result["completed_steps"] == [
        "validate", "policy", "slot_fill", "targeted_replan", "verify", "respond",
    ]
    # orchestrate/recommend/plan never ran - the whole point of this path.
    assert "orchestrate" not in result["completed_steps"]
    assert "recommend" not in result["completed_steps"]


async def test_targeted_replan_degrading_to_full_falls_through_to_orchestrate(monkeypatch):
    _patch_agents(monkeypatch)

    async def _fake_rebuild_degrades(state):
        state.followup_scope = "full"   # e.g. no carried district_id
        return state

    monkeypatch.setattr(orchestrator_module, "rebuild_targeted_days", _fake_rebuild_degrades)

    state = TripState(
        user_input="make it cheaper", is_followup=True, followup_scope="shape_only",
        destination="Kandy", duration_days=1,
    )
    result = await orchestrator.ainvoke(state)

    assert result["completed_steps"] == [
        "validate", "policy", "slot_fill", "targeted_replan",
        "orchestrate", "recommend", "plan", "verify", "respond",
    ]


async def test_full_scope_followup_routes_to_orchestrate_normally(monkeypatch):
    _patch_agents(monkeypatch)

    state = TripState(
        user_input="actually let's go to Galle", is_followup=True, followup_scope="full",
        destination="Galle", duration_days=1,
    )
    result = await orchestrator.ainvoke(state)

    assert "targeted_replan" not in result["completed_steps"]
    assert result["completed_steps"][:4] == ["validate", "policy", "slot_fill", "orchestrate"]
