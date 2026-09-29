# tests/test_state.py
from app.core.state import TripState


def test_trip_state_minimal_construction():
    state = TripState(user_input="Plan a 3-day trip to Ella")
    assert state.user_input == "Plan a 3-day trip to Ella"
    assert state.destination is None
    assert state.errors == []
    assert state.completed_steps == []
    assert state.clarification_needed is None
