"""
Deterministic follow-up classifier (docs/master_plan/AGENT_ARCHITECTURE.md
§5) - decides whether a follow-up turn only changes plan SHAPE ("make day 2
cheaper", "swap the temple for something indoors") or something that
requires a full re-plan (destination/dates/interests/budget/must_avoid, or
an explicit request for different places). "A small deterministic
classifier over the extracted follow-up slots, not an LLM call" is §5's own
words - this is exactly that, nothing more.

Filed as a real gap in ai-backend/TODO.md after PROJECT_MASTER_PLAN.md's
Phase 8 golden-scenario run found it missing (scenario 5 failed because
every follow-up re-ran the full orchestrate->recommend->plan pipeline,
regenerating the whole itinerary instead of adjusting only what changed).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, Optional

from app.models.schemas import ExtractedSlots

_DAY_PATTERN = re.compile(r"\bday\s*(\d+)\b", re.IGNORECASE)

# Deliberately narrow phrase lists, same accepted trade-off as
# app/utils/policy_guard.py's blocklist: catches the obvious phrasing, not
# a determined paraphrase. False negatives here only ever push toward a
# FULL re-plan when a targeted one would have sufficed - safe, just less
# efficient. They never push the other way (missing a "this changed"
# signal and wrongly doing a targeted-only rebuild), since a targeted
# rebuild is chosen only when NONE of the real extracted slots changed.
_WANTS_DIFFERENT_PLACES_PHRASES = [
    "something else", "different place", "different option", "not that",
    "don't like", "dont like", "swap", "instead of", "replace",
]
_CHEAPER_PHRASES = ["cheaper", "less expensive", "lower budget", "reduce cost", "reduce the cost"]


@dataclass
class FollowupPlan:
    scope: Literal["full", "shape_only"]
    target_days: Optional[list[int]] = None   # None = every day in the itinerary
    cheaper: bool = False


def classify_followup(user_input: str, extracted: ExtractedSlots) -> FollowupPlan:
    """`extracted` is THIS turn's raw ExtractedSlots (before it gets merged
    onto TripState by app/utils/slot_filling.py's overwrite semantics) -
    classification has to look at what the user's message itself actually
    asked to change, not the already-merged state (which would show a
    "change" even when nothing was said, since carried-over values are
    already sitting there)."""
    text = user_input.lower()

    changed_a_real_field = any([
        extracted.destination, extracted.duration_days, extracted.budget,
        extracted.travelers, extracted.interests, extracted.must_avoid,
        extracted.pace, extracted.origin_location,
    ])
    wants_different_places = any(phrase in text for phrase in _WANTS_DIFFERENT_PLACES_PHRASES)

    if changed_a_real_field or wants_different_places:
        return FollowupPlan(scope="full")

    days = sorted({int(m) for m in _DAY_PATTERN.findall(user_input)})
    cheaper = any(phrase in text for phrase in _CHEAPER_PHRASES)
    return FollowupPlan(scope="shape_only", target_days=days or None, cheaper=cheaper)
