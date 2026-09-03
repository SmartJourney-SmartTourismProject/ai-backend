# tests/test_trip_api.py
# Exercises the actual HTTP layer (app/api/trip.py) - previously only
# checked via ad-hoc manual scripts, never a committed test. Mocks the
# same orchestrator-level agents as test_orchestrator.py (this isn't the
# place to re-test graph routing, just the API's own contract: request/
# response shape, session handling, and the settings.debug gate).

from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.trip as trip_module
import app.core.orchestrator as orchestrator_module
from app.core.output_validator import ValidationResult
from app.config.settings import settings

app = FastAPI()
app.include_router(trip_module.router)
client = TestClient(app)


async def _extract_kandy_if_missing(state):
    # Stands in for real slot_filling.fill_slots(), which is mocked out in
    # these tests - without this, "Plan a trip to Kandy" never actually
    # sets state.destination, since the mock never parses the message text.
    if state.destination is None:
        state.destination = "Kandy"
    return state


class _FakeAgentResult:
    def __init__(self, success=True, message=None):
        self.success = success
        self.message = message


class _FakeOrchestratorAgent:
    def __init__(self, resolve=True):
        self._resolve = resolve

    async def execute(self, state):
        if not self._resolve:
            state.errors.append("orchestrator_failed: could not resolve destination")
            return _FakeAgentResult(success=False)
        state.trip_context = {"destination_name": state.destination or "Kandy", "district_id": "d1",
                               "lat": 7.29, "lon": 80.63}
        state.weather = {"current": {}, "forecast": []}
        state.disaster = {"safe": True, "active_events": []}
        return _FakeAgentResult(success=True)


class _FakeRecommendationAgent:
    async def execute(self, state):
        item = {"id": "11111111-1111-1111-1111-111111111111", "name": "Test Attraction",
                 "lat": 7.29, "lon": 80.63, "category": "attraction", "rank": 1, "score": 0.9,
                 "reason": "matches interests"}
        state.attractions = [item]
        state.recommendations = [item]
        return _FakeAgentResult(success=True)


class _FakePlannerAgent:
    async def execute(self, state):
        planner_item = {"time": "09:00", "end_time": "10:00", "type": "attraction",
                         "listing_id": "11111111-1111-1111-1111-111111111111", "name": "Test Attraction",
                         "lat": 7.29, "lon": 80.63, "est_cost": 0.0, "currency": "LKR", "notes": ""}
        state.planner_output = {
            "itinerary": [{"day": 1, "date": "2026-10-01", "items": [planner_item], "day_cost": 0.0}],
            "estimated_cost": state.budget or 100.0, "currency": "LKR",
            "budget_notes": "Fits within budget.", "plan_source": "llm",
        }
        state.itinerary = state.planner_output["itinerary"]
        state.estimated_cost = state.planner_output["estimated_cost"]
        state.budget_notes = state.planner_output["budget_notes"]
        state.plan_source = "llm"
        return _FakeAgentResult(success=True)


def _patch_everything(monkeypatch, *, fill_slots_fn=_extract_kandy_if_missing, resolve=True, resolved_location=None):
    monkeypatch.setattr(orchestrator_module, "fill_slots", AsyncMock(side_effect=fill_slots_fn))
    monkeypatch.setattr(orchestrator_module, "OrchestratorAgent", lambda: _FakeOrchestratorAgent(resolve=resolve))
    monkeypatch.setattr(orchestrator_module, "RecommendationAgent", _FakeRecommendationAgent)
    monkeypatch.setattr(orchestrator_module, "PlannerAgent", _FakePlannerAgent)
    monkeypatch.setattr(orchestrator_module, "validate", lambda plan, ctx: ValidationResult(ok=True, failures=[]))
    # trip.py imports resolve_start_location/get_data_freshness directly
    # into its own namespace - get_data_freshness otherwise makes a real
    # DB call (this repo's DATABASE_URL is real, see API_SETUP.md).
    monkeypatch.setattr(trip_module, "resolve_start_location", AsyncMock(return_value=resolved_location))
    monkeypatch.setattr(trip_module, "get_data_freshness", AsyncMock(return_value=None))


def test_request_uses_message_not_user_input(monkeypatch):
    _patch_everything(monkeypatch)

    resp = client.post("/trip-plan", json={"message": "Plan a trip to Kandy", "user_input": "should be ignored"})

    assert resp.status_code == 200


def test_message_field_is_required():
    resp = client.post("/trip-plan", json={"user_input": "Plan a trip to Kandy"})
    assert resp.status_code == 422  # "message" is required, this old field name isn't recognized


def test_response_includes_weather_disaster_and_budget_notes(monkeypatch):
    _patch_everything(monkeypatch)

    resp = client.post("/trip-plan", json={
        "message": "Plan a 2-day trip to Kandy",
    })
    body = resp.json()

    assert resp.status_code == 200
    assert body["weather"] == {"current": {}, "forecast": []}
    assert body["disaster"] == {"safe": True, "active_events": []}
    assert body["budget_notes"] == "Fits within budget."
    assert body["currency"] == "LKR"
    assert body["plan_source"] == "llm"
    assert body["data_freshness"] is None   # get_data_freshness mocked to None above
    assert "session_id" in body and body["session_id"]


def test_trace_hidden_unless_debug(monkeypatch):
    _patch_everything(monkeypatch)
    monkeypatch.setattr(settings, "debug", False)

    resp = client.post("/trip-plan", json={"message": "Plan a trip to Kandy"})
    assert resp.json()["trace"] == {}

    monkeypatch.setattr(settings, "debug", True)
    resp = client.post("/trip-plan", json={"message": "Plan a trip to Kandy"})
    body = resp.json()["trace"]
    assert len(body["completed_steps"]) > 0
    assert "react_traces" in body


def test_followup_turn_reuses_session_and_marks_is_followup(monkeypatch):
    seen_is_followup = []

    async def _capture_followup_flag(state):
        seen_is_followup.append(state.is_followup)
        return await _extract_kandy_if_missing(state)

    _patch_everything(monkeypatch, fill_slots_fn=_capture_followup_flag)

    first = client.post("/trip-plan", json={"message": "Plan a 2-day trip to Kandy"})
    session_id = first.json()["session_id"]

    second = client.post("/trip-plan", json={"message": "Make it cheaper", "session_id": session_id})

    assert second.status_code == 200
    assert second.json()["session_id"] == session_id
    assert second.json()["destination"] == "Kandy"  # carried over, not re-specified
    assert seen_is_followup == [False, True]


def test_unknown_session_id_is_treated_as_a_first_turn(monkeypatch):
    _patch_everything(monkeypatch)

    resp = client.post("/trip-plan", json={
        "message": "Plan a trip to Kandy", "session_id": "never-seen-before",
    })

    assert resp.status_code == 200
    assert resp.json()["destination"] == "Kandy"
