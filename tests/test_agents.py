# tests/test_agents.py
# The three Phase 6 ReAct agents (app/agents/) - app/core/react.py's own
# loop already has dedicated coverage (test_react.py), so these tests mock
# run_react itself and check what each agent does with its result: how it
# maps a structured output back onto TripState, and how it degrades on a
# ReActError. No real LLM, no real tools, no real database.

from unittest.mock import AsyncMock

import app.agents.orchestrator_agent as orchestrator_agent_module
import app.agents.recommendation_agent as recommendation_agent_module
import app.agents.planner_agent as planner_agent_module
from app.agents.orchestrator_agent import OrchestratorAgent
from app.agents.planner_agent import PlannerAgent
from app.agents.recommendation_agent import RecommendationAgent
from app.core.react import ReActError, ReActResult, TraceStep, ToolCallTrace
from app.core.state import TripState
from app.models.schemas import (
    DateWindow, DisasterEvent, DisasterSummary, DroppedItem, ItineraryDay, ItineraryItem,
    PlannerOutput, RecommendationOutput, Selection, TripContext,
)


def _react_result(output, trace=None, stopped_by="answer") -> ReActResult:
    return ReActResult(output=output, trace=trace or [], steps_used=len(trace or []),
                        tools_used=[], stopped_by=stopped_by)


# ─────────────────────────── orchestrator agent ─────────────────────────────

async def test_orchestrator_agent_populates_trip_context_on_success(monkeypatch):
    ctx = TripContext(
        destination_name="Kandy", district_id="d1", lat=7.29, lon=80.63, start_location=None,
        date_window=DateWindow(start_date="2026-10-01", end_date="2026-10-02", source="default", dates=["2026-10-01", "2026-10-02"]),
        per_day_weather=[], disaster=DisasterSummary(safe=True, active_events=[]),
        safety_notes=[], context_confidence="high",
    )
    monkeypatch.setattr(orchestrator_agent_module, "run_react", AsyncMock(return_value=_react_result(ctx)))

    state = TripState(user_input="x", destination="Kandy")
    result = await OrchestratorAgent().execute(state)

    assert result.success is True
    assert state.trip_context["district_id"] == "d1"
    assert state.trip_dates == [{"start_date": "2026-10-01", "end_date": "2026-10-02"}]
    assert state.react_traces["orchestrator"]["stopped_by"] == "answer"


async def test_orchestrator_agent_records_safety_notes_as_soft_errors(monkeypatch):
    ctx = TripContext(
        destination_name="Kandy", district_id="d1", lat=7.29, lon=80.63, start_location=None,
        date_window=DateWindow(start_date="2026-10-01", end_date="2026-10-01", source="default", dates=["2026-10-01"]),
        per_day_weather=[], disaster=DisasterSummary(safe=False, max_severity="red", active_events=[]),
        safety_notes=["flooding reported 20km from destination"], context_confidence="medium",
    )
    monkeypatch.setattr(orchestrator_agent_module, "run_react", AsyncMock(return_value=_react_result(ctx)))

    state = TripState(user_input="x", destination="Kandy")
    await OrchestratorAgent().execute(state)

    assert any("safety_note" in e for e in state.errors)


async def test_orchestrator_agent_synthesizes_safety_note_when_llm_forgot_to(monkeypatch):
    # Regression: found live (Phase 8, golden scenario 8, 2026-09-03) - the
    # orchestrator correctly fetched a real red-severity disaster
    # observation into ctx.disaster, but the LLM left ctx.safety_notes
    # empty despite the rule telling it to fill it - the warning silently
    # never reached the user. safety_notes is now derived deterministically
    # from ctx.disaster, not trusted to the model alone.
    ctx = TripContext(
        destination_name="Kandy", district_id="d1", lat=7.29, lon=80.63, start_location=None,
        date_window=DateWindow(start_date="2026-10-01", end_date="2026-10-01", source="default", dates=["2026-10-01"]),
        per_day_weather=[],
        disaster=DisasterSummary(
            safe=False, max_severity="red",
            active_events=[DisasterEvent(type="flood", severity="red", title="Test flood event",
                                          source="test", distance_km=5.0)],
        ),
        safety_notes=[],   # the LLM left this empty - the bug this test guards against
        context_confidence="high",
    )
    monkeypatch.setattr(orchestrator_agent_module, "run_react", AsyncMock(return_value=_react_result(ctx)))

    state = TripState(user_input="x", destination="Kandy")
    await OrchestratorAgent().execute(state)

    safety_errors = [e for e in state.errors if "safety_note" in e]
    assert safety_errors, "a red disaster event must always produce a safety_note, even if the LLM forgot"
    assert "Test flood event" in safety_errors[0]


async def test_orchestrator_agent_no_duplicate_safety_note_when_llm_already_wrote_one(monkeypatch):
    ctx = TripContext(
        destination_name="Kandy", district_id="d1", lat=7.29, lon=80.63, start_location=None,
        date_window=DateWindow(start_date="2026-10-01", end_date="2026-10-01", source="default", dates=["2026-10-01"]),
        per_day_weather=[],
        disaster=DisasterSummary(
            safe=False, max_severity="red",
            active_events=[DisasterEvent(type="flood", severity="red", title="Test flood event",
                                          source="test", distance_km=5.0)],
        ),
        safety_notes=["Active red-level hazard(s) near your destination: Test flood event."],
        context_confidence="high",
    )
    monkeypatch.setattr(orchestrator_agent_module, "run_react", AsyncMock(return_value=_react_result(ctx)))

    state = TripState(user_input="x", destination="Kandy")
    await OrchestratorAgent().execute(state)

    safety_errors = [e for e in state.errors if "safety_note" in e]
    assert len(safety_errors) == 1


async def test_orchestrator_agent_no_safety_note_when_no_red_disaster(monkeypatch):
    ctx = TripContext(
        destination_name="Kandy", district_id="d1", lat=7.29, lon=80.63, start_location=None,
        date_window=DateWindow(start_date="2026-10-01", end_date="2026-10-01", source="default", dates=["2026-10-01"]),
        per_day_weather=[], disaster=DisasterSummary(safe=True, active_events=[]),
        safety_notes=[], context_confidence="high",
    )
    monkeypatch.setattr(orchestrator_agent_module, "run_react", AsyncMock(return_value=_react_result(ctx)))

    state = TripState(user_input="x", destination="Kandy")
    await OrchestratorAgent().execute(state)

    assert not any("safety_note" in e for e in state.errors)


async def test_orchestrator_agent_degrades_on_react_error(monkeypatch):
    monkeypatch.setattr(orchestrator_agent_module, "run_react", AsyncMock(side_effect=ReActError("no key configured")))

    state = TripState(user_input="x", destination="Kandy")
    result = await OrchestratorAgent().execute(state)

    assert result.success is False
    assert state.trip_context is None
    assert any("orchestrator_failed" in e for e in state.errors)


async def test_orchestrator_agent_degrades_when_get_llm_itself_raises(monkeypatch):
    # Regression (Phase 8, scenario 11 - "Gemini unavailable"): get_llm() is
    # evaluated as part of the run_react(...) call expression, still inside
    # the try block, but a bare `except ReActError` let a plain RuntimeError
    # from get_llm() (no provider configured) escape uncaught - crashing the
    # whole /trip-plan request with an unhandled 500 instead of degrading,
    # exactly what AGENT_ARCHITECTURE.md §6 says must never happen.
    def _raise(*a, **kw):
        raise RuntimeError("No LLM provider has a configured API key.")

    monkeypatch.setattr(orchestrator_agent_module, "get_llm", _raise)

    state = TripState(user_input="x", destination="Kandy")
    result = await OrchestratorAgent().execute(state)   # must not raise

    assert result.success is False
    assert any("orchestrator_failed" in e for e in state.errors)


# ─────────────────────────── recommendation agent ────────────────────────────

_HOTEL_ID = "11111111-1111-1111-1111-111111111111"
_ATTR_ID = "22222222-2222-2222-2222-222222222222"


def _recommendation_trace() -> list[TraceStep]:
    step = TraceStep(step=1, ai_content="")
    step.tool_calls.append(ToolCallTrace(
        tool="db_search_listings", args={"category": "hotel"},
        observation={"items": [{"id": _HOTEL_ID, "name": "Hotel A", "lat": 7.29, "lon": 80.63, "tags": ["stay"]}]},
    ))
    step.tool_calls.append(ToolCallTrace(
        tool="db_search_listings", args={"category": "attraction"},
        observation={"items": [{"id": _ATTR_ID, "name": "Temple", "lat": 7.30, "lon": 80.64, "tags": ["culture"]}]},
    ))
    return [step]


async def test_recommendation_agent_maps_output_onto_state(monkeypatch):
    output = RecommendationOutput(
        hotels=[Selection(listing_id=_HOTEL_ID, category="hotel", rank=1, score=0.8, reason="central")],
        restaurants=[], attractions=[Selection(listing_id=_ATTR_ID, category="attraction", rank=1, score=0.7, reason="popular")],
        events=[], dropped=[], coverage_notes=[],
    )
    monkeypatch.setattr(
        recommendation_agent_module, "run_react",
        AsyncMock(return_value=_react_result(output, trace=_recommendation_trace())),
    )

    state = TripState(user_input="x", destination="Kandy")
    result = await RecommendationAgent().execute(state)

    assert result.success is True
    assert state.candidate_listing_ids == sorted([_HOTEL_ID, _ATTR_ID])
    assert state.candidate_items[_HOTEL_ID]["name"] == "Hotel A"
    assert state.hotels[0]["id"] == _HOTEL_ID
    assert state.hotels[0]["name"] == "Hotel A"   # merged in from the observed candidate, not just id+reason
    assert state.attractions[0]["id"] == _ATTR_ID
    assert len(state.recommendations) == 2


async def test_recommendation_agent_records_dropped_items_but_still_succeeds(monkeypatch):
    output = RecommendationOutput(
        hotels=[], restaurants=[], attractions=[], events=[],
        dropped=[DroppedItem(listing_id=_HOTEL_ID, reason_code="unsafe_area")],
        coverage_notes=["no verified hotels in this district"],
    )
    monkeypatch.setattr(
        recommendation_agent_module, "run_react",
        AsyncMock(return_value=_react_result(output, trace=_recommendation_trace())),
    )

    state = TripState(user_input="x", destination="Kandy")
    result = await RecommendationAgent().execute(state)

    assert result.success is True
    assert state.hotels == []
    assert "1 dropped" in result.message


async def test_recommendation_agent_degrades_on_react_error(monkeypatch):
    monkeypatch.setattr(recommendation_agent_module, "run_react", AsyncMock(side_effect=ReActError("quota exhausted")))

    state = TripState(user_input="x", destination="Kandy")
    result = await RecommendationAgent().execute(state)

    assert result.success is False
    assert any("recommendation_failed" in e for e in state.errors)


async def test_recommendation_agent_degrades_when_get_llm_itself_raises(monkeypatch):
    # Same regression as orchestrator_agent's - see that test's comment.
    def _raise(*a, **kw):
        raise RuntimeError("No LLM provider has a configured API key.")

    monkeypatch.setattr(recommendation_agent_module, "get_llm", _raise)

    state = TripState(user_input="x", destination="Kandy")
    result = await RecommendationAgent().execute(state)   # must not raise

    assert result.success is False
    # Nothing to salvage - the loop never even started - but _observed_pools
    # still returns the full category shape with empty lists, not {}.
    assert all(items == [] for items in state.candidate_pools.values())


async def test_recommendation_agent_salvages_candidate_pools_on_react_error(monkeypatch):
    # Phase 8 fix: the loop can genuinely succeed at gathering real DB
    # candidates even when the FINAL structured-output call fails - that
    # data used to be thrown away entirely, leaving the fallback planner
    # with nothing to build a real itinerary from (the dominant failure
    # path per TODO.md). ReActError now carries the trace so it survives.
    error = ReActError("structured output rejected", trace=_recommendation_trace())
    monkeypatch.setattr(recommendation_agent_module, "run_react", AsyncMock(side_effect=error))

    state = TripState(user_input="x", destination="Kandy")
    result = await RecommendationAgent().execute(state)

    assert result.success is False
    assert state.candidate_pools["hotel"] == [
        {"id": _HOTEL_ID, "name": "Hotel A", "lat": 7.29, "lon": 80.63, "tags": ["stay"]}
    ]
    assert state.candidate_pools["attraction"][0]["id"] == _ATTR_ID
    assert state.candidate_listing_ids == sorted([_HOTEL_ID, _ATTR_ID])
    # state.hotels/etc are NOT populated on failure - those need a real
    # Selection (rank/score/reason), which a failed structured call never
    # produced. _fallback_node reads candidate_pools, not these.
    assert state.hotels == []


async def test_recommendation_agent_populates_candidate_pools_on_success(monkeypatch):
    output = RecommendationOutput(
        hotels=[Selection(listing_id=_HOTEL_ID, category="hotel", rank=1, score=0.8, reason="central")],
        restaurants=[], attractions=[], events=[], dropped=[], coverage_notes=[],
    )
    monkeypatch.setattr(
        recommendation_agent_module, "run_react",
        AsyncMock(return_value=_react_result(output, trace=_recommendation_trace())),
    )

    state = TripState(user_input="x", destination="Kandy")
    await RecommendationAgent().execute(state)

    assert state.candidate_pools["hotel"][0]["id"] == _HOTEL_ID
    assert state.candidate_pools["attraction"][0]["id"] == _ATTR_ID
    assert state.candidate_pools["restaurant"] == []


# ─────────────────────────── planner agent ──────────────────────────────────

def _planner_output() -> PlannerOutput:
    item = ItineraryItem(
        time="09:00", end_time="10:00", type="attraction", listing_id=_ATTR_ID,
        name="Temple", lat=7.30, lon=80.64, est_cost=500.0, currency="LKR",
    )
    day = ItineraryDay(day=1, date="2026-10-01", items=[item], day_cost=500.0)
    return PlannerOutput(itinerary=[day], estimated_cost=500.0, budget_notes=None)


async def test_planner_agent_populates_itinerary_on_success(monkeypatch):
    monkeypatch.setattr(planner_agent_module, "_fetch_cost_table", AsyncMock(return_value={}))
    monkeypatch.setattr(planner_agent_module, "run_react", AsyncMock(return_value=_react_result(_planner_output())))

    state = TripState(user_input="x", destination="Kandy", duration_days=1, budget=1000)
    result = await PlannerAgent().execute(state)

    assert result.success is True
    assert state.plan_source == "llm"
    assert len(state.itinerary) == 1
    assert state.estimated_cost == 500.0


async def test_planner_agent_degrades_on_react_error(monkeypatch):
    monkeypatch.setattr(planner_agent_module, "_fetch_cost_table", AsyncMock(return_value={}))
    monkeypatch.setattr(planner_agent_module, "run_react", AsyncMock(side_effect=ReActError("timed out")))

    state = TripState(user_input="x", destination="Kandy", duration_days=1)
    result = await PlannerAgent().execute(state)

    assert result.success is False
    assert state.planner_output is None


async def test_planner_agent_degrades_when_get_llm_itself_raises(monkeypatch):
    # Same regression as orchestrator_agent's - see that test's comment.
    def _raise(*a, **kw):
        raise RuntimeError("No LLM provider has a configured API key.")

    monkeypatch.setattr(planner_agent_module, "_fetch_cost_table", AsyncMock(return_value={}))
    monkeypatch.setattr(planner_agent_module, "get_llm", _raise)

    state = TripState(user_input="x", destination="Kandy", duration_days=1)
    result = await PlannerAgent().execute(state)   # must not raise

    assert result.success is False
    assert state.planner_output is None
    assert any("planner_failed" in e for e in state.errors)


async def test_planner_agent_degrades_to_empty_cost_table_when_db_unreachable(monkeypatch):
    async def _boom():
        raise RuntimeError("db down")

    # _fetch_cost_table itself never raises (it catches internally) - this
    # verifies that promise, not the agent's own error handling.
    monkeypatch.setattr(planner_agent_module, "get_pool", AsyncMock(side_effect=RuntimeError("db down")))
    result = await planner_agent_module._fetch_cost_table()
    assert result == {}
