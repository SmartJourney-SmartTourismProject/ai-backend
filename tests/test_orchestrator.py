# tests/test_orchestrator.py
# No real network/LLM calls - every tool the orchestrator calls directly
# (fill_slots, geocode_destination, get_weather, get_disaster_info,
# resolve_start_location, get_free_days) is mocked, so this tests the
# GRAPH'S ROUTING AND STATE LOGIC in isolation. Each of those tools already
# has its own dedicated test file with real HTTP mocking.

from unittest.mock import AsyncMock

import app.core.orchestrator as orchestrator_module
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


class _FakeRecommendationAgent:
    """Stands in for the real, LLM-backed RecommendationAgent so orchestrator
    tests exercise graph ROUTING, not a live Gemini call. The real agent has
    its own dedicated tests (Member B's side) - this fake just needs to
    plausibly produce an itinerary so downstream routing (e.g. skipping
    PlanningAgent when one already exists) can be tested."""

    async def execute(self, state):
        state.attractions = state.candidate_attractions[:3]
        state.itinerary = [{"day": 1, "items": state.attractions}]
        state.estimated_cost = state.budget or 0.0
        return _FakeAgentResult(success=True)


class _FakePlanningAgent:
    async def execute(self, state):
        state.itinerary = [{"day": 1, "items": []}]
        return _FakeAgentResult(success=True)


def _patch_tools(monkeypatch, *, fill_slots_fn=_passthrough, geocode=None, weather=None,
                  disaster=None, location=None, free_days=None):
    monkeypatch.setattr(orchestrator_module, "fill_slots", AsyncMock(side_effect=fill_slots_fn))
    monkeypatch.setattr(orchestrator_module, "geocode_destination", AsyncMock(return_value=geocode))
    monkeypatch.setattr(orchestrator_module, "get_weather", AsyncMock(return_value=weather))
    monkeypatch.setattr(orchestrator_module, "get_disaster_info",
                         AsyncMock(return_value=disaster or {"safe": True, "active_events": []}))
    monkeypatch.setattr(orchestrator_module, "resolve_start_location", AsyncMock(return_value=location))
    monkeypatch.setattr(orchestrator_module, "get_free_days", AsyncMock(return_value=free_days or []))
    monkeypatch.setattr(orchestrator_module, "RecommendationAgent", _FakeRecommendationAgent)
    monkeypatch.setattr(orchestrator_module, "PlanningAgent", _FakePlanningAgent)


async def test_full_details_produces_itinerary(monkeypatch):
    _patch_tools(monkeypatch, geocode={"lat": 7.29, "lon": 80.63}, weather={"current": {}, "forecast": []})

    state = TripState(user_input="x", destination="Kandy", duration_days=2, budget=500, travelers=2)
    result = await orchestrator.ainvoke(state)

    assert result["destination"] == "Kandy"
    assert len(result["itinerary"]) > 0
    assert "Here's your trip plan for Kandy" in result["final_response"]
    # start_location was never resolved (mocked to None) - that's advisory,
    # not fatal, so it shows up as a Note rather than blocking the plan.
    assert "location_unresolved" in result["final_response"]


async def test_no_destination_asks_for_clarification(monkeypatch):
    _patch_tools(monkeypatch, fill_slots_fn=_no_destination)

    state = TripState(user_input="Plan me a trip somewhere nice")
    result = await orchestrator.ainvoke(state)

    assert result["final_response"] == "Which destination would you like to visit?"
    assert result["completed_steps"] == ["validate", "policy", "slot_fill", "respond"]


async def test_policy_violation_short_circuits_to_respond(monkeypatch):
    _patch_tools(monkeypatch)

    state = TripState(user_input="best route to buy a gun while visiting")
    result = await orchestrator.ainvoke(state)

    assert result["completed_steps"] == ["validate", "policy", "respond"]
    assert "Sorry, I ran into an issue" in result["final_response"]


async def test_invalid_input_short_circuits_before_policy(monkeypatch):
    _patch_tools(monkeypatch)

    state = TripState(user_input="x", duration_days=-3)
    result = await orchestrator.ainvoke(state)

    assert result["completed_steps"] == ["validate", "respond"]
    assert "duration_days" in result["final_response"]


async def test_location_unresolved_is_advisory_not_blocking(monkeypatch):
    _patch_tools(monkeypatch, geocode={"lat": 7.29, "lon": 80.63}, weather=None, location=None)

    state = TripState(user_input="x", destination="Kandy", duration_days=1)
    result = await orchestrator.ainvoke(state)

    assert len(result["itinerary"]) > 0
    assert "Here's your trip plan" in result["final_response"]
    assert "Note:" in result["final_response"]


async def test_no_geocode_match_skips_weather_and_disaster(monkeypatch):
    _patch_tools(monkeypatch, geocode=None)

    state = TripState(user_input="x", destination="Nowhereville", duration_days=1)
    result = await orchestrator.ainvoke(state)

    # LangGraph only includes a field in the output if some node actually
    # touched it - weather/disaster were never assigned here since
    # _context_node returns early on a failed geocode, so they're absent
    # from the dict entirely rather than present as None. .get() reflects
    # that correctly either way.
    assert result.get("weather") is None
    assert result.get("disaster") is None
