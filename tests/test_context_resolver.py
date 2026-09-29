# tests/test_context_resolver.py
# app/core/context_resolver.py - the deterministic replacement for the old
# "orchestrator" ReAct agent ("C2", itinerary-quality/token-reduction pass).
# No real LLM, no real network, no real database: every underlying
# app/tools/* function it calls is mocked at the module's own import name,
# same convention as test_agents.py uses for run_react.

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import app.core.context_resolver as context_resolver_module
from app.core.context_resolver import resolve_trip_context
from app.core.state import TripState
from app.utils.clock import today_local

_PLACE = {"name": "Kandy", "lat": 7.29, "lon": 80.63, "district_id": "d1", "confidence": "high"}


def _patch_happy_path(monkeypatch, disaster=None, weather=None, free_days=None):
    monkeypatch.setattr(context_resolver_module, "resolve_place", AsyncMock(return_value=_PLACE))
    monkeypatch.setattr(context_resolver_module, "resolve_district", AsyncMock(return_value=None))
    monkeypatch.setattr(context_resolver_module, "get_weather", AsyncMock(return_value=weather or {"forecast": []}))
    monkeypatch.setattr(
        context_resolver_module, "get_disaster_info",
        AsyncMock(return_value=disaster or {"safe": True, "active_events": []}),
    )
    monkeypatch.setattr(context_resolver_module, "get_free_days", AsyncMock(return_value=free_days or []))


async def test_populates_trip_context_on_success(monkeypatch):
    _patch_happy_path(monkeypatch)
    state = TripState(user_input="x", destination="Kandy", duration_days=2)

    await resolve_trip_context(state)

    assert state.trip_context["district_id"] == "d1"
    assert state.trip_context["destination_name"] == "Kandy"
    assert state.trip_context["date_window"]["source"] == "default"
    assert len(state.trip_context["date_window"]["dates"]) == 2
    assert state.trip_dates[0]["start_date"] == state.trip_context["date_window"]["start_date"]
    assert state.react_traces["orchestrator"]["stopped_by"] == "deterministic"


async def test_uses_carried_trip_dates_as_the_user_source_when_present(monkeypatch):
    _patch_happy_path(monkeypatch)
    state = TripState(
        user_input="x", destination="Kandy", duration_days=2,
        trip_dates=[{"start_date": "2026-11-01", "end_date": "2026-11-02"}],
    )

    await resolve_trip_context(state)

    assert state.trip_context["date_window"] == {
        "start_date": "2026-11-01", "end_date": "2026-11-02", "source": "user",
        "dates": ["2026-11-01", "2026-11-02"],
    }


async def test_stale_carried_window_is_recomputed_against_new_duration(monkeypatch):
    # Live-found 2026-09-26: a follow-up that grows duration_days (e.g.
    # "make it 5 days") doesn't touch trip_dates, so the carried window was
    # still the OLD 1-day range. dates_in_window then rejected every day
    # past day 1 (they were derived from start_date but weren't in the
    # window), sending a perfectly good plan through repair -> fallback.
    # start_date is kept; only end_date/dates should be recomputed.
    _patch_happy_path(monkeypatch)
    state = TripState(
        user_input="x", destination="Kandy", duration_days=5,
        trip_dates=[{"start_date": "2026-09-26", "end_date": "2026-09-26", "source": "user"}],
    )

    await resolve_trip_context(state)

    window = state.trip_context["date_window"]
    assert window["start_date"] == "2026-09-26"
    assert window["end_date"] == "2026-09-30"
    assert window["source"] == "user"
    assert window["dates"] == [
        "2026-09-26", "2026-09-27", "2026-09-28", "2026-09-29", "2026-09-30",
    ]


async def test_stale_carried_calendar_window_is_rechecked_not_reused(monkeypatch):
    # A carried "calendar" window was only ever verified free for the
    # duration AT THE TIME it was resolved (the >= duration guard in
    # get_free_days below) - reusing it as-is after duration grows could
    # silently book the traveller over their own busy days. It must be
    # re-derived from a fresh free_days lookup against the NEW duration.
    today = today_local()
    long_start = (today + timedelta(days=5)).isoformat()
    long_end = (today + timedelta(days=8)).isoformat()   # 4 days free
    _patch_happy_path(monkeypatch, free_days=[
        {"start_date": long_start, "end_date": long_end},
    ])
    state = TripState(
        user_input="x", destination="Kandy", duration_days=4, user_id="u1",
        # Carried from an earlier, shorter-duration turn - a single day
        # taken from the middle of the real free range, which would be
        # invalid as a 4-day window's start on its own.
        trip_dates=[{"start_date": long_start, "end_date": long_start, "source": "calendar"}],
    )

    await resolve_trip_context(state)

    window = state.trip_context["date_window"]
    assert window["source"] == "calendar"
    assert window["start_date"] == long_start
    assert len(window["dates"]) == 4


async def test_uses_the_soonest_long_enough_calendar_window(monkeypatch):
    today = today_local()
    short_start = (today + timedelta(days=1)).isoformat()
    short_end = (today + timedelta(days=1)).isoformat()   # only 1 day free - too short for a 2-day trip
    long_start = (today + timedelta(days=5)).isoformat()
    long_end = (today + timedelta(days=8)).isoformat()    # 4 days free - long enough
    _patch_happy_path(monkeypatch, free_days=[
        {"start_date": short_start, "end_date": short_end},
        {"start_date": long_start, "end_date": long_end},
    ])
    state = TripState(user_input="x", destination="Kandy", duration_days=2, user_id="u1")

    await resolve_trip_context(state)

    assert state.trip_context["date_window"]["source"] == "calendar"
    assert state.trip_context["date_window"]["start_date"] == long_start
    # trimmed to exactly duration_days, not the whole free range
    assert len(state.trip_context["date_window"]["dates"]) == 2


async def test_defaults_to_today_when_no_carried_dates_and_no_calendar_match(monkeypatch):
    _patch_happy_path(monkeypatch, free_days=[])
    state = TripState(user_input="x", destination="Kandy", duration_days=3, user_id="u1")

    await resolve_trip_context(state)

    today = today_local().isoformat()
    assert state.trip_context["date_window"]["source"] == "default"
    assert state.trip_context["date_window"]["start_date"] == today
    assert state.trip_context["context_confidence"] == "medium"   # capped by the default-window rule


async def test_synthesizes_safety_note_from_a_red_disaster_event(monkeypatch):
    # Same guarantee the old LLM agent's deterministic synthesis provided
    # (found live, Phase 8 scenario 8, 2026-09-06) - a red-severity event
    # must always produce a safety_note; there's no LLM to forget it now.
    _patch_happy_path(monkeypatch, disaster={
        "safe": False,
        "active_events": [{"type": "flood", "severity": "red", "title": "Test flood event",
                            "source": "test", "distance_km": 5.0}],
    })
    state = TripState(user_input="x", destination="Kandy", duration_days=1)

    await resolve_trip_context(state)

    safety_errors = [e for e in state.errors if "safety_note" in e]
    assert safety_errors
    assert "Test flood event" in safety_errors[0]
    assert state.trip_context["disaster"]["max_severity"] == "red"


async def test_no_safety_note_when_no_red_disaster(monkeypatch):
    _patch_happy_path(monkeypatch, disaster={
        "safe": False,
        "active_events": [{"type": "flood", "severity": "orange", "title": "Minor flooding",
                            "source": "test", "distance_km": 5.0}],
    })
    state = TripState(user_input="x", destination="Kandy", duration_days=1)

    await resolve_trip_context(state)

    assert not any("safety_note" in e for e in state.errors)
    assert state.trip_context["disaster"]["max_severity"] == "orange"


async def test_falls_back_to_resolve_district_when_place_has_no_district(monkeypatch):
    place_without_district = {**_PLACE, "district_id": None}
    monkeypatch.setattr(context_resolver_module, "resolve_place", AsyncMock(return_value=place_without_district))
    monkeypatch.setattr(context_resolver_module, "resolve_district",
                         AsyncMock(return_value={"district_id": "d2", "name": "Kandy", "province": "Central"}))
    monkeypatch.setattr(context_resolver_module, "get_weather", AsyncMock(return_value={"forecast": []}))
    monkeypatch.setattr(context_resolver_module, "get_disaster_info",
                         AsyncMock(return_value={"safe": True, "active_events": []}))
    state = TripState(user_input="x", destination="Kandy", duration_days=1)

    await resolve_trip_context(state)

    assert state.trip_context["district_id"] == "d2"


async def test_degrades_when_destination_never_resolves(monkeypatch):
    resolve_place_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(context_resolver_module, "resolve_place", resolve_place_mock)
    state = TripState(user_input="x", destination="Nowhereville", duration_days=1)

    await resolve_trip_context(state)   # must not raise

    assert state.trip_context is None
    assert any("orchestrator_failed" in e for e in state.errors)
    assert resolve_place_mock.await_count == 2   # one retry, matching the old prompt's RULE 2


async def test_degrades_when_district_never_resolves(monkeypatch):
    place_without_district = {**_PLACE, "district_id": None}
    monkeypatch.setattr(context_resolver_module, "resolve_place", AsyncMock(return_value=place_without_district))
    monkeypatch.setattr(context_resolver_module, "resolve_district", AsyncMock(return_value=None))
    state = TripState(user_input="x", destination="Kandy", duration_days=1)

    await resolve_trip_context(state)   # must not raise

    assert state.trip_context is None
    assert any("orchestrator_failed" in e for e in state.errors)


async def test_degrades_on_unexpected_exception_without_raising(monkeypatch):
    async def _boom(*a, **kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(context_resolver_module, "resolve_place", _boom)
    state = TripState(user_input="x", destination="Kandy", duration_days=1)

    await resolve_trip_context(state)   # must not raise

    assert any("orchestrator_failed" in e for e in state.errors)
