# app/utils/session_store.py
"""
Interim persistence for multi-turn trip-planning conversations.

TODO(Member B): move this to the real chat_session/itinerary tables (see
SAD §9 Data View - Chat_Session, Chat_Message); NestJS is arguably the
better owner, since it already needs chat history for the sidebar.
Until then, a local JSON file keyed by session_id - same interim pattern
as app/tools/calendar_tool.py's token store, and with the same caveats:
not safe across multiple server instances, no encryption at rest.
"""
import json
from pathlib import Path
from typing import Optional

_SESSION_STORE_PATH = Path(__file__).resolve().parent.parent.parent / "trip_sessions.json"

# Fields carried over from the previous turn onto the next TripState. Not
# everything on TripState belongs here - user_input, errors, clarification_needed,
# completed_steps, and final_response are all per-turn, not session state.
_CARRY_OVER_FIELDS = [
    "destination", "duration_days", "budget", "travelers", "user_id",
    "start_location", "trip_dates", "interests", "travel_style",
    "candidate_attractions", "candidate_hotels", "candidate_restaurants", "candidate_events",
    "weather", "disaster", "attractions", "hotels", "restaurants", "events",
    "itinerary", "recommendations", "estimated_cost",
]


def _load_store() -> dict:
    if not _SESSION_STORE_PATH.exists():
        return {}
    try:
        return json.loads(_SESSION_STORE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_store(store: dict) -> None:
    _SESSION_STORE_PATH.write_text(json.dumps(store, indent=2, default=str))


def load_session(session_id: str) -> Optional[dict]:
    """Returns the carried-over fields for a session_id, or None if unknown."""
    return _load_store().get(session_id)


def save_session(session_id: str, state) -> None:
    """Persists the carry-over subset of `state` (a TripState) for next turn."""
    store = _load_store()
    store[session_id] = {field: getattr(state, field) for field in _CARRY_OVER_FIELDS}
    _save_store(store)
