# tests/test_orchestrator.py
# The graph is validate->policy->slot_fill->orchestrate->recommend->plan->
# verify->(repair|fallback)->respond. This file tests the GRAPH'S ROUTING
# AND STATE LOGIC in isolation - every node's real work is faked so no real
# LLM/tool/DB call happens; each real piece has its own dedicated test
# coverage (test_context_resolver.py, test_agents.py, etc.). "orchestrate"
# is now app/core/context_resolver.py's deterministic resolve_trip_context()
# rather than a ReAct agent - see that node's own tests for its behavior;
# here it's just faked like every other node.

from unittest.mock import AsyncMock

import app.core.orchestrator as orchestrator_module
from app.core.output_validator import ValidationResult
from app.core.planner_shared import resolve_planner_max_steps
from app.core.react import ReActError, ReActResult, ToolCallTrace, TraceStep
from app.core.state import TripState
from app.core.orchestrator import _build_validation_context, _trim_previous_output, orchestrator
from app.models.schemas import ItineraryDay, ItineraryItem, RepairedPlannerOutput


def test_trim_previous_output_strips_bulky_fields_but_keeps_repair_relevant_ones():
    # B3 (AI_BACKEND_OPTIMIZATION_PLAN.md): name/lat/lon/currency/notes are
    # already in the human message via build_planner_human_message - only
    # what identifies an item and what a repair might check needs to
    # survive here.
    planner_output = {
        "itinerary": [{
            "day": 1, "date": "2026-10-01", "day_cost": 1500.0,
            "items": [{
                "time": "09:00", "end_time": "10:00", "type": "attraction",
                "listing_id": "a1", "name": "Temple", "lat": 7.29, "lon": 80.63,
                "est_cost": 500.0, "currency": "LKR", "notes": "arrive early",
            }],
        }],
        "estimated_cost": 1500.0, "currency": "LKR", "budget_notes": None, "plan_source": "llm",
    }

    trimmed = _trim_previous_output(planner_output)

    assert trimmed == {
        "itinerary": [{
            "day": 1, "date": "2026-10-01", "day_cost": 1500.0,
            "items": [{"listing_id": "a1", "type": "attraction",
                       "time": "09:00", "end_time": "10:00", "est_cost": 500.0}],
        }],
        "estimated_cost": 1500.0,
    }


def test_trim_previous_output_handles_empty_input():
    assert _trim_previous_output({}) == {"itinerary": [], "estimated_cost": None}


def test_validation_context_expands_start_end_only_date_window_into_full_range():
    # date_window shapes without "dates" (older/partial callers) used to
    # collapse valid_dates to just {start, end} - which rejected every
    # middle day of a 3+ day trip on dates_in_window. Every real day in the
    # range must be present, not just its two endpoints.
    state = TripState(
        user_input="x", duration_days=3,
        trip_context={"date_window": {"start_date": "2026-10-01", "end_date": "2026-10-03"}},
    )

    ctx = _build_validation_context(state)

    assert ctx.valid_dates == {"2026-10-01", "2026-10-02", "2026-10-03"}


async def _passthrough(state):
    return state


async def _no_destination(state):
    state.clarification_needed = "Which destination would you like to visit?"
    return state


class _FakeAgentResult:
    def __init__(self, success=True, message=None):
        self.success = success
        self.message = message


def _fake_context_resolver(context=None, error=None):
    """Stands in for the real resolve_trip_context(). `context` controls
    whether resolution "succeeded" (sets trip_context/weather/disaster) or
    "failed" (leaves them unset and records an advisory/hard error, per the
    real function's own error-handling) - matches resolve_trip_context's
    own contract: mutates state in place, returns None."""

    async def _resolve(state):
        if error:
            state.errors.append(error)
        if context:
            state.trip_context = context
            state.weather = {"forecast": []}
            state.disaster = {"safe": True, "active_events": []}

    return _resolve


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


class _FakeDuplicateAttractionPlannerAgent:
    """Produces a day 1 with the SAME attraction listed twice - a real
    no_duplicates violation the REAL output_validator.validate() will
    catch, and one output_validator.day_scoped_repair_target recognizes as
    day-scoped and deterministically fixable (see the test this backs).
    Uses state.attractions[0] (the recommendation agent's own real
    candidate) so fill_missing_days' rebuild has genuine data to work
    with, not a second invented id."""
    async def execute(self, state):
        attr_id = state.attractions[0]["id"] if state.attractions else "11111111-1111-1111-1111-111111111111"
        item = {"time": "09:00", "end_time": "10:00", "type": "attraction", "listing_id": attr_id,
                "name": "Test Attraction", "lat": 7.29, "lon": 80.63, "est_cost": 0.0, "currency": "LKR", "notes": ""}
        state.planner_output = {
            "itinerary": [{"day": 1, "date": "2026-10-01", "items": [item, dict(item)], "day_cost": 0.0}],
            "estimated_cost": 0.0, "currency": "LKR", "budget_notes": None,
        }
        state.itinerary = state.planner_output["itinerary"]
        state.estimated_cost = 0.0
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


def _patch_agents(monkeypatch, *, fill_slots_fn=_passthrough, context_resolver=None,
                   recommendation_agent=None, planner_agent=None, validation_ok=True,
                   patch_validate=True):
    monkeypatch.setattr(orchestrator_module, "fill_slots", AsyncMock(side_effect=fill_slots_fn))
    monkeypatch.setattr(
        orchestrator_module, "resolve_trip_context",
        context_resolver or _fake_context_resolver(
            context={"destination_name": "Kandy", "district_id": "d1", "lat": 7.29, "lon": 80.63}),
    )
    monkeypatch.setattr(orchestrator_module, "RecommendationAgent",
                         lambda: recommendation_agent or _FakeRecommendationAgent())
    monkeypatch.setattr(orchestrator_module, "PlannerAgent",
                         lambda: planner_agent or _FakePlannerAgent())
    if patch_validate:
        monkeypatch.setattr(
            orchestrator_module, "validate",
            lambda plan, ctx: ValidationResult(ok=validation_ok, failures=[] if validation_ok else ["L2.day_count: failed"]),
        )
    # else: the REAL validate() runs - needed by tests that exercise
    # _repair_node's Tier 4 deterministic fast path, since its own internal
    # re-validation (`recheck`) reads the same module-level `validate` name
    # a blanket mock would also poison.
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


async def _rejects_new_destination_with_stale_plan(state):
    # Simulates slot_filling.py's real out-of-country branch: a follow-up
    # deliberately let a newly-mentioned destination overwrite the carried-
    # over one, but this one gets rejected.
    state.clarification_needed = (
        "SmartJourney currently covers destinations within Sri Lanka only. "
        "New York is in United States - is there a Sri Lankan destination I can help you plan instead?"
    )
    return state


async def test_clarification_clears_stale_carried_over_itinerary(monkeypatch):
    # Regression (live-found 2026-09-06): a follow-up asking for "New York"
    # (rejected as out-of-country) came back with the *previous* turn's
    # real itinerary (a Matara plan) still attached, mislabeled under
    # "New York - 2 days" - session restoration carries itinerary over for
    # legitimate follow-ups ("make it cheaper"), but nothing cleared it
    # when the turn was rejected outright instead of actually replanning.
    _patch_agents(monkeypatch, fill_slots_fn=_rejects_new_destination_with_stale_plan)

    state = TripState(
        user_input="Plan a 2-day trip to New York budget 60000 LKR, culture and history",
        destination="New York",
        itinerary=[{"day": 1, "date": "2026-10-01", "items": [{"name": "Turtle Bay"}], "day_cost": 0.0}],
        estimated_cost=0.0,
        budget_notes="40 item(s) had no price data and are excluded from the total.",
        plan_source="fallback",
    )
    result = await orchestrator.ainvoke(state)

    assert result["itinerary"] == []
    assert result["estimated_cost"] is None
    assert result["budget_notes"] is None
    assert result["plan_source"] is None
    assert "Sri Lanka" in result["final_response"]


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


async def test_context_resolution_failure_is_advisory_not_blocking(monkeypatch):
    # resolve_trip_context failed to resolve anything (e.g. geocoding down) -
    # the failure is recorded, and the request is answered rather than
    # crashing. (Since 2026-10-01 recommend/plan no longer run without a
    # trip_context - see _route_after_orchestrate.)
    _patch_agents(monkeypatch, context_resolver=_fake_context_resolver(error="orchestrator_failed: geocoding unavailable"))

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


async def test_verify_failure_with_no_progress_falls_back_after_one_repair(monkeypatch):
    """A repair that reproduces the EXACT same failure set as the attempt
    right before it (this fixture's `validate` mock always returns the same
    fixed failure) triggers the no-progress guard immediately after just
    ONE repair, well before settings.max_repair_attempts (2) is reached -
    this is the common real case, a systematic failure a retry can't fix
    (see settings.repair_temperature_step's own comment). The genuine
    "both attempts allowed, no progress guard" case is covered separately
    below (test_verify_failure_gets_two_repair_attempts_before_fallback).

    `_repair_node` itself is real here (only its internal run_react call is
    faked to fail) - monkeypatching the node function on the module wouldn't
    affect the already-compiled graph, since `orchestrator =
    build_orchestrator_graph()` bound the real function object into the
    graph at import time; module-global lookups INSIDE a node body
    (RecommendationAgent(), validate(), run_react()) still work because
    Python resolves those at call time, not bind time."""
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


async def test_verify_failure_gets_two_repair_attempts_before_fallback(monkeypatch):
    """settings.max_repair_attempts (2, user decision 2026-09-26) is honored
    in FULL when each attempt makes genuine progress (a different failure
    set each time) - the no-progress guard only short-circuits an IDENTICAL
    repeat, never merely "still invalid"."""
    _patch_agents(monkeypatch)
    call_count = {"n": 0}

    def _validate(plan, ctx):
        call_count["n"] += 1
        # A different failure every call - real (if insufficient) progress,
        # so the no-progress guard never fires and the full cap is used.
        return ValidationResult(ok=False, failures=[f"L2.day_count: attempt {call_count['n']}"])

    monkeypatch.setattr(orchestrator_module, "validate", _validate)
    monkeypatch.setattr(
        orchestrator_module, "run_react",
        AsyncMock(side_effect=ReActError("repair call failed")),
    )

    state = TripState(user_input="x", destination="Kandy", duration_days=1)
    result = await orchestrator.ainvoke(state)

    assert result["completed_steps"] == [
        "validate", "policy", "slot_fill", "orchestrate", "recommend", "plan",
        "verify", "repair", "verify", "repair", "verify", "fallback", "verify", "respond",
    ]
    assert result["repair_attempts"] == 2
    assert result["plan_source"] == "fallback"
    # Fallback investigation (2026-09-25): the rejected LLM plan's own
    # validation failures used to vanish the moment _fallback_node's own
    # (always-valid) output got re-verified - _fallback_node now captures
    # them into state.errors before that happens, as a soft (non-hard) note.
    assert any("llm_plan_rejected" in e and "L2.day_count" in e for e in result["errors"])


async def test_repair_fixes_a_day_scoped_failure_deterministically_without_an_llm_call(monkeypatch):
    """Tier 4 (2026-09-26): a real no_duplicates failure (day 1 lists the
    same attraction twice) is day-scoped and a rule build_day_plan already
    enforces by construction - output_validator.day_scoped_repair_target
    recognizes it, so _repair_node should rebuild JUST day 1 via
    fill_missing_days and skip the LLM entirely. `validate` is left REAL
    here (patch_validate=False) since this fast path's own correctness
    depends on the recheck being genuine, not a mock that would pass or
    fail regardless of what got rebuilt."""
    context = {
        "destination_name": "Kandy", "district_id": "d1", "lat": 7.29, "lon": 80.63,
        "date_window": {"start_date": "2026-10-01", "end_date": "2026-10-01",
                         "source": "user", "dates": ["2026-10-01"]},
    }
    _patch_agents(
        monkeypatch,
        context_resolver=_fake_context_resolver(context=context),
        planner_agent=_FakeDuplicateAttractionPlannerAgent(),
        patch_validate=False,
    )
    run_react_mock = AsyncMock(side_effect=AssertionError("the LLM repair path should never be reached"))
    monkeypatch.setattr(orchestrator_module, "run_react", run_react_mock)

    state = TripState(
        user_input="x", destination="Kandy", duration_days=1,
        # app/api/trip.py always resolves this from real client_gps/client_ip
        # before the graph runs (see context_resolver.py's own docstring) -
        # set here so fill_missing_days' rebuild has a real anchor, not the
        # (0.0, 0.0) degenerate fallback it'd otherwise use with no hotels
        # either, which would put every real candidate over the anchor
        # leash's distance limit and rebuild an empty day.
        start_location={"lat": 7.29, "lon": 80.63},
    )
    # The real RecommendationAgent sets this from its own db_search_*
    # observations (app/core/output_validator.py's L1 referential check
    # reads it) - the fake used here only sets state.attractions/
    # recommendations, so it's set directly here to the same id the fake
    # planner then (correctly) reuses, keeping L1 out of this test's way.
    state.candidate_listing_ids = ["11111111-1111-1111-1111-111111111111"]
    result = await orchestrator.ainvoke(state)

    run_react_mock.assert_not_called()
    assert result["completed_steps"] == [
        "validate", "policy", "slot_fill", "orchestrate", "recommend", "plan",
        "verify", "repair", "verify", "respond",
    ]
    assert result["repair_attempts"] == 1
    assert result["plan_source"] == "llm"
    day1_attractions = [i for i in result["itinerary"][0]["items"] if i["type"] == "attraction"]
    assert len(day1_attractions) == 1   # the duplicate is gone


async def test_empty_fallback_itinerary_does_not_claim_success(monkeypatch):
    # Regression (live-found 2026-09-06): a day with an empty items list
    # used to still hit the "Here's your trip plan... 0 day(s) planned"
    # success message whenever there were no *hard* errors logged - the
    # old check only looked at `hard_errors and not state.itinerary`, which
    # never triggers for a non-empty list of day dicts. recommend "failing"
    # via produce=False (not validation_ok=False) is what reaches fallback
    # with zero hard errors logged - the exact combination that slipped
    # through before. Confirmed a real destination is still known here, so
    # this should explain the gap rather than ask a question already answered.
    _patch_agents(monkeypatch, recommendation_agent=_FakeRecommendationAgent(produce=False))

    state = TripState(user_input="x", destination="Kandy", duration_days=1)
    result = await orchestrator.ainvoke(state)

    # Still zero HARD errors - which is the combination this regression is
    # about. _fallback_node's own fallback_reason note (2026-09-26) is soft
    # by construction and doesn't change that.
    assert result["errors"] == ["fallback_reason: no recommendations were produced"]
    assert "Here's your trip plan" not in result["final_response"]
    assert "Kandy" in result["final_response"]


async def test_empty_itinerary_with_no_destination_asks_for_one(monkeypatch):
    _patch_agents(
        monkeypatch,
        fill_slots_fn=_passthrough,
        recommendation_agent=_FakeRecommendationAgent(produce=False),
    )

    state = TripState(user_input="x", duration_days=1)
    result = await orchestrator.ainvoke(state)

    assert result["errors"] == ["fallback_reason: no recommendations were produced"]
    assert "Here's your trip plan" not in result["final_response"]
    assert "destination" in result["final_response"].lower()


async def test_repair_node_assembles_days_from_its_own_tool_trace(monkeypatch):
    # Fallback investigation (2026-09-25): a real repair's finalize step was
    # found to mistranscribe its own build_day_plan observation (duplicate
    # items, whole-trip cost repeated per day). This proves the fix end to
    # end: the repaired day_cost in the final response comes from the tool
    # observation, not from the (deliberately wrong) RepairedPlannerOutput
    # the fake run_react returns. It also exercises the exact line that
    # regressed live (resolve_planner_max_steps referenced but not
    # imported) by asserting run_react was actually called with a
    # duration-scaled max_steps, not the flat default.
    _patch_agents(monkeypatch, validation_ok=False)
    # The original planner output must fail verify (to reach repair at all),
    # but the REPAIRED output must then pass - `_patch_agents`'
    # validation_ok=False fails unconditionally, which would route straight
    # to fallback even after a successful repair.
    validate_calls = {"n": 0}

    def _fails_once_then_passes(plan, ctx):
        validate_calls["n"] += 1
        if validate_calls["n"] == 1:
            return ValidationResult(ok=False, failures=["L2.day_count: failed"])
        return ValidationResult(ok=True, failures=[])

    monkeypatch.setattr(orchestrator_module, "validate", _fails_once_then_passes)

    wrong_item = ItineraryItem(
        time="09:00", end_time="10:00", type="hotel",
        listing_id="11111111-1111-1111-1111-111111111111", name="Wrong Hotel",
        lat=7.29, lon=80.63, est_cost=99999.0, currency="LKR",
    )
    wrong_output = RepairedPlannerOutput(
        itinerary=[ItineraryDay(day=1, date="2026-10-01", items=[wrong_item, wrong_item], day_cost=99999.0)],
        estimated_cost=99999.0, budget_notes=None,
    )
    real_trace = [TraceStep(step=1, tool_calls=[ToolCallTrace(
        tool="build_day_plan", args={"day": 1, "date": "2026-10-01"},
        observation={
            "items": [{"time": "09:00", "end_time": "10:00", "type": "attraction",
                       "listing_id": "22222222-2222-2222-2222-222222222222", "name": "Temple",
                       "lat": 7.30, "lon": 80.64, "est_cost": 500.0, "currency": "LKR", "notes": ""}],
            "day_cost": 500.0, "total_km": 1.0, "total_travel_min": 5, "dropped": [],
        },
    )])]
    run_react_mock = AsyncMock(return_value=ReActResult(
        output=wrong_output, trace=real_trace, steps_used=1, tools_used=["build_day_plan"], stopped_by="answer",
    ))
    monkeypatch.setattr(orchestrator_module, "run_react", run_react_mock)

    # start_location near the trace's own anchor (7.29, 80.63) - so
    # fill_missing_days (Part 5, server-side enforcement) has somewhere real
    # to build days 2-5 from, using _FakeRecommendationAgent's one
    # attraction (also at 7.29, 80.63). Without it, every filled day would
    # come out anchor-less-and-empty - a fixture artifact, not something
    # this test means to exercise.
    state = TripState(user_input="x", destination="Kandy", duration_days=5,
                      start_location={"lat": 7.29, "lon": 80.63})
    result = await orchestrator.ainvoke(state)

    assert result["plan_source"] == "llm"
    # day_count always holds now - only day 1 came from the model's own
    # (real) tool trace; days 2-5 had no observation within the model's turn
    # budget and are filled deterministically rather than dropped (the old
    # behavior this test used to assert: a 5-day request silently coming
    # back as a 1-day plan).
    assert len(result["itinerary"]) == 5
    day1 = result["itinerary"][0]
    assert day1["day_cost"] == 500.0   # the tool's real cost, not the model's 99999.0
    assert len(day1["items"]) == 1     # not the duplicated hotel
    # cost_table is mocked empty ({}), so the filled days' items all cost
    # 0.0 - the total is still just day 1's real cost.
    assert result["estimated_cost"] == 500.0

    run_react_mock.assert_awaited_once()
    used_config = run_react_mock.call_args.kwargs["config"]
    assert used_config.max_steps == resolve_planner_max_steps(5)
    assert used_config.max_steps != 3   # would be the flat default if the scaling call silently no-op'd


async def test_repair_node_sets_plan_source_on_success_even_if_original_planner_never_ran(monkeypatch):
    # Regression, found live (fallback investigation, 2026-09-25): _repair_node
    # never set state.plan_source at all. That was invisible as long as the
    # ORIGINAL planner call had already set it to "llm" before failing
    # validation - but when the original planner call fails OUTRIGHT (an
    # exception, never reaching its own `state.plan_source = "llm"` line),
    # a subsequently successful repair used to leave plan_source at its
    # TripState default (None) even though it had just assembled a real,
    # valid, tool-backed plan. `produce=False` reproduces the "outright
    # failure" case: no plan_source is set anywhere before repair runs.
    # produce=False means the ORIGINAL planner call never sets
    # planner_output/itinerary at all - _verify_node's "no plan produced"
    # branch handles that first failure without ever calling validate(), so
    # the only real validate() call in this test is the one AFTER repair,
    # which must pass for repair's output to be accepted rather than
    # falling through to the deterministic fallback.
    _patch_agents(monkeypatch, planner_agent=_FakePlannerAgent(produce=False), validation_ok=True)

    good_item = ItineraryItem(
        time="09:00", end_time="10:00", type="attraction",
        listing_id="22222222-2222-2222-2222-222222222222", name="Temple",
        lat=7.30, lon=80.64, est_cost=500.0, currency="LKR",
    )
    repaired_output = RepairedPlannerOutput(
        itinerary=[ItineraryDay(day=1, date="2026-10-01", items=[good_item], day_cost=500.0)],
        estimated_cost=500.0, budget_notes=None,
    )
    trace = [TraceStep(step=1, tool_calls=[ToolCallTrace(
        tool="build_day_plan", args={"day": 1, "date": "2026-10-01"},
        observation={"items": [good_item.model_dump()], "day_cost": 500.0,
                     "total_km": 1.0, "total_travel_min": 5, "dropped": []},
    )])]
    monkeypatch.setattr(orchestrator_module, "run_react", AsyncMock(return_value=ReActResult(
        output=repaired_output, trace=trace, steps_used=1, tools_used=["build_day_plan"], stopped_by="answer",
    )))

    state = TripState(user_input="x", destination="Kandy", duration_days=1)
    result = await orchestrator.ainvoke(state)

    assert result["plan_source"] == "llm"
    assert result["itinerary"][0]["day_cost"] == 500.0


async def test_repair_node_degrades_when_get_llm_itself_raises(monkeypatch):
    # Regression (Phase 8, scenario 11) - see app/agents/planner_agent.py's
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
