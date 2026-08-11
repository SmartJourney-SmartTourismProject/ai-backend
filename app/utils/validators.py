# app/utils/validators.py
from datetime import date
from app.core.state import TripState


def validate_trip_state(state: TripState) -> TripState:
    """
    Rule-based sanity checks on whatever the user gave — not a completeness check.
    Missing fields are fine (slot-filling / defaults handle those later); this only
    catches values that are present but invalid. Appends to state.errors and returns
    the same state so it composes cleanly as a LangGraph node.
    """
    errors: list[str] = []

    if state.duration_days is not None and state.duration_days <= 0:
        errors.append("duration_days must be a positive integer")

    if state.budget is not None and state.budget <= 0:
        errors.append("budget must be greater than 0")

    if state.travelers is not None and state.travelers < 1:
        errors.append("travelers must be at least 1")

    if state.trip_dates:
        for window in state.trip_dates:
            start = window.get("start_date")
            end = window.get("end_date")
            if start and end and start > end:
                errors.append(f"trip_dates window has start_date after end_date: {start} > {end}")

    state.errors.extend(errors)
    return state