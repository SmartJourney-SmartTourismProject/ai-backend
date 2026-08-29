# tests/test_session_store.py
from app.core.state import TripState
from app.utils.session_store import load_session, save_session


def test_unknown_session_returns_none():
    assert load_session("nope") is None


def test_save_and_load_round_trips_carry_over_fields():
    state = TripState(
        user_input="Plan a trip to Kandy",
        destination="Kandy",
        duration_days=2,
        budget=300.0,
        interests=["culture"],
        itinerary=[{"day": 1, "items": []}],
        estimated_cost=250.0,
    )

    save_session("session-1", state)
    loaded = load_session("session-1")

    assert loaded["destination"] == "Kandy"
    assert loaded["duration_days"] == 2
    assert loaded["budget"] == 300.0
    assert loaded["interests"] == ["culture"]
    assert loaded["itinerary"] == [{"day": 1, "items": []}]
    assert loaded["estimated_cost"] == 250.0


def test_per_turn_fields_are_not_carried_over():
    # user_input, errors, clarification_needed, completed_steps, and
    # final_response describe a single turn, not the ongoing session - they
    # should never be part of what's saved/loaded.
    state = TripState(
        user_input="anything",
        destination="Galle",
        errors=["some error"],
        clarification_needed="Which destination?",
        completed_steps=["validate", "policy"],
        final_response="some response",
    )

    save_session("session-2", state)
    loaded = load_session("session-2")

    assert "user_input" not in loaded
    assert "errors" not in loaded
    assert "clarification_needed" not in loaded
    assert "completed_steps" not in loaded
    assert "final_response" not in loaded


def test_second_session_does_not_clobber_first():
    save_session("session-a", TripState(user_input="x", destination="Kandy"))
    save_session("session-b", TripState(user_input="x", destination="Galle"))

    assert load_session("session-a")["destination"] == "Kandy"
    assert load_session("session-b")["destination"] == "Galle"


def test_loading_a_previous_session_and_applying_it_to_a_new_state():
    original = TripState(
        user_input="Plan a trip to Kandy, budget 300",
        destination="Kandy",
        budget=300.0,
        duration_days=2,
    )
    save_session("session-3", original)

    carried_over = load_session("session-3")
    follow_up_state = TripState(
        user_input="Make the budget 600 instead",
        session_id="session-3",
        is_followup=True,
        **carried_over,
    )

    assert follow_up_state.destination == "Kandy"
    assert follow_up_state.budget == 300.0  # not yet updated - that's fill_slots' job
    assert follow_up_state.is_followup is True
