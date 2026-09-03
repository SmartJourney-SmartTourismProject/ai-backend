# app/utils/session_store.py
"""
Multi-turn trip-planning session persistence, backed by the real
`ai_session` table (Phase 7, docs/master_plan/AGENT_ARCHITECTURE.md §5) -
replaces the interim `trip_sessions.json` file this module used through
Phase 6. That file grew without bound (every field TripState ever touched
was persisted forever) and made a follow-up turn reuse stale weather/
disaster/candidate data instead of re-deriving it. Both are fixed here:
`_CARRY_OVER_FIELDS` is the narrow set §5 actually specifies, and
`ai_session.expires_at` (default now()+7 days, enforced both by the WHERE
clause below and by `app/scheduler.py`'s nightly `session_gc` job) means a
session genuinely goes away instead of accumulating forever.

Both functions are async now (the JSON-file versions were sync) - callers
(`app/api/trip.py`) must `await` them.
"""
from __future__ import annotations

import json
import logging
from typing import Optional
from uuid import UUID

from app.core.state import TripState
from app.utils.db_pool import get_pool

logger = logging.getLogger(__name__)

# Carried over between turns (AGENT_ARCHITECTURE.md §5), adapted to Phase 6's
# actual TripState shape: trip_context now bundles destination_name/
# district_id/lat/lon/date_window together (the orchestrator agent's own
# structured output), so it's carried as one blob rather than decomposed
# back into loose fields. `destination`/`trip_dates` are also kept since
# they're what slot_filling.py reads directly on a follow-up turn, ahead of
# the orchestrator agent re-running.
#
# Deliberately NOT carried (re-derived each turn, per §5): candidate_*
# arrays, weather, disaster, errors, final_response, react_traces,
# completed_steps, recommendation_output, planner_output, validation_failures,
# plan_source. Today's TripState still HAS all of these fields - carrying
# them was Phase 6-and-earlier's actual bug; this list is what stops that,
# not a TripState schema change.
_CARRY_OVER_FIELDS = [
    "destination", "trip_context", "start_location", "trip_dates",
    "duration_days", "budget", "travelers",
    "interests", "travel_style", "must_avoid", "pace",
    "itinerary",
]

_SELECT_SQL = "SELECT state FROM ai_session WHERE session_id = $1 AND expires_at > now()"
_UPSERT_SQL = """
    INSERT INTO ai_session (session_id, user_id, state, react_trace, turn_count)
    VALUES ($1, $2, $3, $4, 1)
    ON CONFLICT (session_id) DO UPDATE SET
        state = EXCLUDED.state,
        react_trace = EXCLUDED.react_trace,
        user_id = COALESCE(EXCLUDED.user_id, ai_session.user_id),
        turn_count = ai_session.turn_count + 1,
        updated_at = now(),
        expires_at = now() + interval '7 days'
"""


def _as_uuid_or_none(value: Optional[str]) -> Optional[str]:
    """ai_session.user_id is a real `uuid` column - a non-UUID string
    (a test fixture's "user-1", or any bad client input) must not blow up
    the whole session save. Validated here rather than left for asyncpg to
    reject, so a bad user_id degrades to "session saved without one"
    instead of the session not saving at all."""
    if not value:
        return None
    try:
        return str(UUID(value))
    except (ValueError, AttributeError, TypeError):
        logger.debug(f"session_store: user_id {value!r} is not a UUID, storing session without it.")
        return None


def _selections_ids_only(state: TripState) -> dict[str, list[dict]]:
    """The "selections (ids only, not full rows)" half of §5 - state.hotels/
    etc already carry the full merged candidate dict (lat/lon/tags/price...)
    recommendation_agent.py built for this turn; none of that should survive
    into next turn's session (it can be stale, and the next turn re-derives
    it from a fresh db_search_* call if it needs to recommend again)."""
    def _strip(items: list[dict]) -> list[dict]:
        return [{"id": i.get("id"), "category": i.get("category")} for i in items]

    return {
        "hotels": _strip(state.hotels), "restaurants": _strip(state.restaurants),
        "attractions": _strip(state.attractions), "events": _strip(state.events),
    }


async def load_session(session_id: str) -> Optional[dict]:
    """Returns the carried-over fields for a session_id, or None if unknown,
    expired, or the database is unreachable. A database outage degrading a
    follow-up into "treated as a first turn" (session_id still comes back
    in the response, just without memory of the prior turn) is the correct
    failure mode here - never a 500 for what's fundamentally a convenience
    feature, not the request itself."""
    pool = await get_pool()
    if pool is None:
        logger.debug("session_store: no database configured, treating as a first turn.")
        return None
    try:
        row = await pool.fetchrow(_SELECT_SQL, session_id)
    except Exception as e:
        logger.warning(f"session_store: load failed for session {session_id}: {e}")
        return None
    if row is None:
        return None

    carried = json.loads(row["state"])
    # "selections" isn't a real TripState field (it's this module's own
    # carry-over shape for hotels/restaurants/attractions/events ids) -
    # TripState(**carried_over) in app/api/trip.py would reject an unknown
    # kwarg, so it's popped back out here rather than merged in as-is.
    carried.pop("selections", None)
    return carried


async def save_session(session_id: str, state: TripState) -> None:
    """Persists the carry-over subset of `state` (upsert - a follow-up turn
    overwrites the whole row, not merges field-by-field, since TripState(
    **carried_over) in app/api/trip.py already handles what should and
    shouldn't be freshly overridden per-turn) plus this turn's react traces
    for `react_trace` (debug/report material only - never read back onto
    the next turn's TripState). Never raises - a failed session save
    degrades to "this follow-up won't remember this turn", not a failed
    response to a user who already has their plan."""
    pool = await get_pool()
    if pool is None:
        logger.debug(f"session_store: no database configured, session {session_id} not persisted.")
        return

    carried = {f: getattr(state, f) for f in _CARRY_OVER_FIELDS}
    carried["selections"] = _selections_ids_only(state)

    try:
        await pool.execute(
            _UPSERT_SQL, session_id,
            _as_uuid_or_none(state.user_id),
            json.dumps(carried, default=str),
            json.dumps(state.react_traces, default=str) if state.react_traces else None,
        )
    except Exception as e:
        logger.warning(f"session_store: save failed for session {session_id}: {e}")
