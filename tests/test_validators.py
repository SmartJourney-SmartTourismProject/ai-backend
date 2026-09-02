# tests/test_validators.py
# New - validators.py didn't have a dedicated test file before, only
# exercised indirectly through the orchestrator.

import pytest

from app.core.state import TripState
from app.utils.validators import validate_trip_state


def test_valid_state_has_no_errors():
    state = TripState(user_input="x", duration_days=3, budget=100, travelers=2)
    result = validate_trip_state(state)
    assert result.errors == []


@pytest.mark.parametrize("field,value,expected_substring", [
    ("duration_days", -1, "duration_days"),
    ("duration_days", 0, "duration_days"),
    ("duration_days", 31, "duration_days"),        # Phase 5 addition: upper bound
    ("budget", 0, "budget"),
    ("budget", -50, "budget"),
    ("budget", 100_000_001, "budget"),              # Phase 5 addition: typo-catching upper bound
    ("travelers", 0, "travelers"),
    ("travelers", 21, "travelers"),                 # Phase 5 addition: upper bound
])
def test_invalid_field_produces_error(field, value, expected_substring):
    state = TripState(user_input="x", **{field: value})
    result = validate_trip_state(state)
    assert any(expected_substring in e for e in result.errors)


def test_duration_and_travelers_at_the_exact_max_are_valid():
    # Boundary check: MAX_* values themselves must be accepted, not rejected
    # off-by-one.
    state = TripState(user_input="x", duration_days=30, travelers=20, budget=100_000_000)
    result = validate_trip_state(state)
    assert result.errors == []


def test_user_input_over_length_limit_is_flagged():
    state = TripState(user_input="x" * 2001)
    result = validate_trip_state(state)
    assert any("user_input" in e for e in result.errors)


def test_user_input_at_exact_limit_is_valid():
    state = TripState(user_input="x" * 2000)
    result = validate_trip_state(state)
    assert result.errors == []


def test_trip_dates_entirely_in_the_past_is_flagged():
    state = TripState(
        user_input="x",
        trip_dates=[{"start_date": "2020-01-01", "end_date": "2020-01-05"}],
    )
    result = validate_trip_state(state)
    assert any("entirely in the past" in e for e in result.errors)


def test_trip_dates_in_the_future_is_not_flagged():
    state = TripState(
        user_input="x",
        trip_dates=[{"start_date": "2099-01-01", "end_date": "2099-01-05"}],
    )
    result = validate_trip_state(state)
    assert not any("past" in e for e in result.errors)


def test_trip_dates_start_after_end_is_flagged():
    state = TripState(
        user_input="x",
        trip_dates=[{"start_date": "2026-09-05", "end_date": "2026-09-01"}],
    )
    result = validate_trip_state(state)
    assert any("start_date after end_date" in e for e in result.errors)


def test_missing_fields_are_not_errors():
    # validators.py deliberately doesn't check completeness - only catches
    # values that are present but invalid. Missing fields are slot-filling's
    # job, not this one's.
    state = TripState(user_input="x")
    result = validate_trip_state(state)
    assert result.errors == []
