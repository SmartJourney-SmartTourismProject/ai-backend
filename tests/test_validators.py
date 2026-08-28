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
    ("budget", 0, "budget"),
    ("budget", -50, "budget"),
    ("travelers", 0, "travelers"),
])
def test_invalid_field_produces_error(field, value, expected_substring):
    state = TripState(user_input="x", **{field: value})
    result = validate_trip_state(state)
    assert any(expected_substring in e for e in result.errors)


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
