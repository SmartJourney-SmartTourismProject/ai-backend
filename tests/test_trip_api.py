# tests/test_trip_api.py
# Exercises the actual HTTP layer (app/api/trip.py) - previously only
# checked via ad-hoc manual scripts, never a committed test. Mocks the
# same orchestrator-level tools as test_orchestrator.py (this isn't the
# place to re-test graph routing, just the API's own contract: request/
# response shape, session handling, and the settings.debug gate).

from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.trip as trip_module
import app.core.orchestrator as orchestrator_module
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


class _FakeRecommendationAgent:
    async def execute(self, state):
        state.attractions = state.candidate_attractions[:3]
        state.itinerary = [{"day": 1, "items": []}]
        state.estimated_cost = state.budget or 100.0
        state.budget_notes = "Fits within budget."
        return _FakeAgentResult(success=True)


class _FakePlanningAgent:
    async def execute(self, state):
        return _FakeAgentResult(success=True)


def _patch_everything(monkeypatch, *, fill_slots_fn=_extract_kandy_if_missing, geocode=None, resolved_location=None):
    monkeypatch.setattr(orchestrator_module, "fill_slots", AsyncMock(side_effect=fill_slots_fn))
    monkeypatch.setattr(orchestrator_module, "geocode_destination", AsyncMock(return_value=geocode))
    monkeypatch.setattr(orchestrator_module, "get_weather", AsyncMock(return_value={"current": {}, "forecast": []}))
    monkeypatch.setattr(orchestrator_module, "get_disaster_info", AsyncMock(return_value={"safe": True, "active_events": []}))
    monkeypatch.setattr(orchestrator_module, "get_free_days", AsyncMock(return_value=[]))
    monkeypatch.setattr(orchestrator_module, "RecommendationAgent", _FakeRecommendationAgent)
    monkeypatch.setattr(orchestrator_module, "PlanningAgent", _FakePlanningAgent)
    # trip.py imports resolve_start_location directly into its own namespace.
    monkeypatch.setattr(trip_module, "resolve_start_location", AsyncMock(return_value=resolved_location))


def test_request_uses_message_not_user_input(monkeypatch):
    _patch_everything(monkeypatch, geocode={"lat": 7.29, "lon": 80.63})

    resp = client.post("/trip-plan", json={"message": "Plan a trip to Kandy", "user_input": "should be ignored"})

    assert resp.status_code == 200


def test_message_field_is_required():
    resp = client.post("/trip-plan", json={"user_input": "Plan a trip to Kandy"})
    assert resp.status_code == 422  # "message" is required, this old field name isn't recognized


def test_response_includes_weather_disaster_and_budget_notes(monkeypatch):
    _patch_everything(monkeypatch, geocode={"lat": 7.29, "lon": 80.63})

    resp = client.post("/trip-plan", json={
        "message": "Plan a 2-day trip to Kandy",
    })
    body = resp.json()

    assert resp.status_code == 200
    assert body["weather"] == {"current": {}, "forecast": []}
    assert body["disaster"] == {"safe": True, "active_events": []}
    assert body["budget_notes"] == "Fits within budget."
    assert "session_id" in body and body["session_id"]


def test_completed_steps_hidden_unless_debug(monkeypatch):
    _patch_everything(monkeypatch, geocode={"lat": 7.29, "lon": 80.63})
    monkeypatch.setattr(settings, "debug", False)

    resp = client.post("/trip-plan", json={"message": "Plan a trip to Kandy"})
    assert resp.json()["completed_steps"] == []

    monkeypatch.setattr(settings, "debug", True)
    resp = client.post("/trip-plan", json={"message": "Plan a trip to Kandy"})
    assert len(resp.json()["completed_steps"]) > 0


def test_followup_turn_reuses_session_and_marks_is_followup(monkeypatch):
    seen_is_followup = []

    async def _capture_followup_flag(state):
        seen_is_followup.append(state.is_followup)
        return await _extract_kandy_if_missing(state)

    _patch_everything(monkeypatch, fill_slots_fn=_capture_followup_flag, geocode={"lat": 7.29, "lon": 80.63})

    first = client.post("/trip-plan", json={"message": "Plan a 2-day trip to Kandy"})
    session_id = first.json()["session_id"]

    second = client.post("/trip-plan", json={"message": "Make it cheaper", "session_id": session_id})

    assert second.status_code == 200
    assert second.json()["session_id"] == session_id
    assert second.json()["destination"] == "Kandy"  # carried over, not re-specified
    assert seen_is_followup == [False, True]


def test_unknown_session_id_is_treated_as_a_first_turn(monkeypatch):
    _patch_everything(monkeypatch, geocode={"lat": 7.29, "lon": 80.63})

    resp = client.post("/trip-plan", json={
        "message": "Plan a trip to Kandy", "session_id": "never-seen-before",
    })

    assert resp.status_code == 200
    assert resp.json()["destination"] == "Kandy"
