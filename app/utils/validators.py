# app/utils/validators.py
from datetime import date

from app.core.state import TripState

# Upper bounds - decision D6/Phase 5 (docs/master_plan/DETERMINISM_AND_VALIDATION.md
# §5 "Validating the inputs too"). These reject an obvious typo (an extra
# zero on the budget, "300 days" instead of "3 days") before it reaches the
# LLM or a real database query, not real trip constraints - a 30-day Sri
# Lanka trip or a 20-person group tour are both plausible; ten million
# people or three years are not.
MAX_BUDGET_LKR = 100_000_000
MAX_DURATION_DAYS = 30
MAX_TRAVELERS = 20
MAX_USER_INPUT_CHARS = 2_000


def validate_trip_state(state: TripState) -> TripState:
    """
    Rule-based sanity checks on whatever the user gave — not a completeness check.
    Missing fields are fine (slot-filling / defaults handle those later); this only
    catches values that are present but invalid. Appends to state.errors and returns
    the same state so it composes cleanly as a LangGraph node.
    """
    errors: list[str] = []
    #duration is negative
    if state.duration_days is not None and state.duration_days <= 0:
        errors.append("duration_days must be a positive integer")
    if state.duration_days is not None and state.duration_days > MAX_DURATION_DAYS:
        errors.append(f"duration_days must be at most {MAX_DURATION_DAYS}")
    #budget is negative
    if state.budget is not None and state.budget <= 0:
        errors.append("budget must be greater than 0")
    if state.budget is not None and state.budget > MAX_BUDGET_LKR:
        errors.append(f"budget must be at most {MAX_BUDGET_LKR:,} (looks like a typo otherwise)")
    # Traveler count is less than 1
    if state.travelers is not None and state.travelers < 1:
        errors.append("travelers must be at least 1")
    if state.travelers is not None and state.travelers > MAX_TRAVELERS:
        errors.append(f"travelers must be at most {MAX_TRAVELERS}")
    if state.user_input is not None and len(state.user_input) > MAX_USER_INPUT_CHARS:
        errors.append(f"user_input must be at most {MAX_USER_INPUT_CHARS} characters")
    # Start date  is after end date
    if state.trip_dates:
        today = date.today().isoformat()
        for window in state.trip_dates:
            start = window.get("start_date")
            end = window.get("end_date")
            if start and end and start > end:
                errors.append(f"trip_dates window has start_date after end_date: {start} > {end}")
            if end and end < today:
                errors.append(f"trip_dates window is entirely in the past: end_date {end} < today {today}")

    state.errors.extend(errors)
    return state