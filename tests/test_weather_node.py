# tests/test_weather_node.py
# Live-weather questions (intent="weather") - get_weather, resolve_place and
# the clock are faked; the graph routing is real.

from datetime import date
from unittest.mock import AsyncMock

import app.core.orchestrator as orchestrator_module
from app.core.orchestrator import _umbrella_advice, orchestrator
from app.core.state import TripState

from tests.test_orchestrator import _patch_agents

TODAY = date(2026, 10, 1)


def _forecast(*days):
    return {"current": {}, "forecast": [
        {"date": d, "temp_min": 25.0, "temp_max": 31.0, "condition": c, "rain_probability": p}
        for d, c, p in days
    ]}


def _setup(monkeypatch, fill, weather=None, place={"lat": 6.93, "lon": 79.85, "confidence": "high"}):
    _patch_agents(monkeypatch, fill_slots_fn=fill)
    monkeypatch.setattr(orchestrator_module, "today_local", lambda: TODAY)
    get_weather = AsyncMock(return_value=weather)
    monkeypatch.setattr(orchestrator_module, "get_weather", get_weather)
    monkeypatch.setattr(orchestrator_module, "resolve_place", AsyncMock(return_value=place))
    return get_weather


def _asks(place=None, when=None):
    async def fill(state):
        state.intent = "weather"
        state.weather_place = place
        state.weather_when = when
        return state
    return fill


def test_umbrella_advice_thresholds():
    assert _umbrella_advice(0.8).startswith("Yes")
    assert _umbrella_advice(0.3).startswith("Maybe")
    assert _umbrella_advice(0.1).startswith("Probably not")


async def test_rain_tomorrow_in_colombo_answers_from_the_live_forecast(monkeypatch):
    get_weather = _setup(monkeypatch, _asks("Colombo", "tomorrow"),
                         weather=_forecast(("2026-10-02", "Rain", 0.9)))
    result = await orchestrator.ainvoke(TripState(user_input="will it rain tomorrow in colombo?"))

    assert result["completed_steps"] == ["validate", "policy", "slot_fill", "weather"]
    assert get_weather.call_args.args[2] == ["2026-10-02"]
    reply = result["final_response"]
    assert "Weather for Colombo" in reply and "Tomorrow (2026-10-02)" in reply
    assert "90% chance of rain" in reply and "Yes, take an umbrella" in reply


async def test_no_place_named_uses_the_travellers_location(monkeypatch):
    _setup(monkeypatch, _asks(None, "tomorrow"), weather=_forecast(("2026-10-02", "Clear", 0.05)))
    state = TripState(user_input="will it rain tomorrow", start_location={"lat": 6.9, "lon": 79.9, "source": "ip"})
    result = await orchestrator.ainvoke(state)
    assert "Weather for your location" in result["final_response"]
    assert "Probably not needed" in result["final_response"]


async def test_no_place_at_all_asks_which_place(monkeypatch):
    _setup(monkeypatch, _asks(None, "tomorrow"))
    result = await orchestrator.ainvoke(TripState(user_input="will it rain tomorrow"))
    assert result["final_response"] == "Which place should I check the weather for?"


async def test_asking_about_another_place_never_changes_the_trip(monkeypatch):
    _setup(monkeypatch, _asks("Colombo", "today"), weather=_forecast(("2026-10-01", "Clouds", 0.3)))
    itinerary = [{"day": 1, "date": "2026-10-05", "items": [{"name": "Temple"}], "day_cost": 0.0}]
    state = TripState(user_input="rain in colombo today?", destination="Kandy", itinerary=itinerary, is_followup=True)
    result = await orchestrator.ainvoke(state)

    assert result["destination"] == "Kandy" and result["itinerary"] == itinerary
    assert "Weather for Colombo" in result["final_response"]


async def test_trip_dates_are_used_for_a_weather_follow_up(monkeypatch):
    get_weather = _setup(monkeypatch, _asks(None, "trip_dates"),
                         weather=_forecast(("2026-10-02", "Rain", 0.97), ("2026-10-03", "Clear", 0.1)))
    state = TripState(user_input="will it rain on those days?", destination="Colombo", is_followup=True,
                      trip_context={"lat": 6.93, "lon": 79.85, "destination_name": "Colombo",
                                    "date_window": {"dates": ["2026-10-02", "2026-10-03"]}})
    result = await orchestrator.ainvoke(state)
    assert get_weather.call_args.args[2] == ["2026-10-02", "2026-10-03"]
    assert "97% chance of rain" in result["final_response"]


async def test_dates_beyond_the_forecast_horizon_say_so(monkeypatch):
    get_weather = _setup(monkeypatch, _asks(None, "trip_dates"))
    state = TripState(user_input="rain on my trip?", destination="Kandy", is_followup=True,
                      trip_context={"lat": 7.29, "lon": 80.63, "date_window": {"dates": ["2026-10-20"]}})
    result = await orchestrator.ainvoke(state)
    get_weather.assert_not_called()
    assert "too far ahead to forecast" in result["final_response"]


async def test_live_failure_falls_back_to_the_trips_stored_forecast(monkeypatch):
    _setup(monkeypatch, _asks(None, "trip_dates"), weather=None)
    state = TripState(user_input="rain?", destination="Colombo", is_followup=True,
                      trip_context={"lat": 6.93, "lon": 79.85, "date_window": {"dates": ["2026-10-02"]},
                                    "per_day_weather": [{"date": "2026-10-02", "temp_min": 24.0, "temp_max": 30.0,
                                                         "condition": "Rain", "rain_probability": 0.9}]})
    result = await orchestrator.ainvoke(state)
    assert "forecast from when your plan was made" in result["final_response"]


async def test_weather_service_down_with_nothing_stored(monkeypatch):
    _setup(monkeypatch, _asks("Galle", "today"), weather=None)
    result = await orchestrator.ainvoke(TripState(user_input="weather in galle today"))
    assert "weather service isn't available" in result["final_response"]


async def test_weather_follow_up_classified_by_the_followup_classifier_routes_live(monkeypatch):
    async def followup_weather(state):
        state.followup_scope = "informational"
        state.followup_info = "weather"
        state.intent = "question"   # what the real LLM tends to tag it
        return state
    _setup(monkeypatch, followup_weather, weather=_forecast(("2026-10-01", "Rain", 0.6)))
    state = TripState(user_input="will it rain?", destination="Colombo", is_followup=True,
                      trip_context={"lat": 6.93, "lon": 79.85}, weather_when=None)
    result = await orchestrator.ainvoke(state)
    assert "weather" in result["completed_steps"]
    assert "60% chance of rain" in result["final_response"]
