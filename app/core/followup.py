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

# "Make day 2 more relaxed" asks for a lighter day, not a different pace for
# the whole trip. Before these were recognised it came back unchanged: when
# the model left pace null the turn was a plain shape_only rebuild at the
# same item count, which picks the same stops deterministically; when the
# model did extract pace="relaxed" on a trip that was already relaxed, the
# full re-plan landed on the same plan too. The delta is applied against the
# targeted day's OWN current count (followup_replan.py), so it always moves.
_LIGHTER_PHRASES = [
    "relax", "less busy", "less packed", "less rushed", "take it easy", "lighter",
    "slower", "too busy", "too packed", "too rushed", "fewer",
]
_BUSIER_PHRASES = [
    "busier", "more packed", "pack more", "more places", "more stops", "more things",
    "add more", "add another", "add one more",
]

# Questions ABOUT the plan that already exists, rather than requests to change
# it. Before these were recognised, "show budget breakdown" fell through to a
# shape_only re-plan: the user asked a question and got a different itinerary
# back, with a different total, because the plan had been rebuilt from scratch.
# An informational turn must never re-plan - it answers from state.itinerary,
# which session carry-over already provides, with no LLM or tool call at all.
_INFORMATIONAL_PHRASES = [
    "budget breakdown", "cost breakdown", "break down the cost", "breakdown of the cost",
    "show budget", "show the budget", "show cost", "show the cost",
    "how much", "what does it cost", "what will it cost", "total cost",
    "what is the cost", "what's the cost",
]


# Weather questions about the trip already planned ("will it rain on those
# days?") are answered from the forecast stored with the trip
# (trip_context.per_day_weather) - live data, which RAG's static knowledge
# base cannot know. Live-found 2026-09-30: with RAG on, such a question was
# routed to it and got a generic monsoon paragraph.
# Whole-word matched: a bare substring test would read "train" as "rain".
_WEATHER_PATTERN = re.compile(
    r"\b(rain|rainy|raining|weather|forecast|umbrella|raincoat|sunny|temperature|storm|how hot|how cold)\b",
    re.IGNORECASE,
)


@dataclass
class FollowupPlan:
    scope: Literal["full", "shape_only", "informational"]
    info_kind: Literal["budget", "weather"] = "budget"   # only meaningful when scope == "informational"
    target_days: Optional[list[int]] = None   # None = every day in the itinerary
    cheaper: bool = False
    # Stops to add (+) or remove (-) per day. With named days,
    # followup_replan.py applies it to each targeted day's own current count;
    # with none, slot_filling.py folds it into the trip-wide items_per_day.
    density_delta: int = 0


def _density_delta(text: str, extracted: ExtractedSlots) -> int:
    if extracted.items_per_day_delta:
        return extracted.items_per_day_delta
    if extracted.pace == "relaxed" or any(p in text for p in _LIGHTER_PHRASES):
        return -1
    if extracted.pace == "packed" or any(p in text for p in _BUSIER_PHRASES):
        return 1
    return 0


def classify_followup(user_input: str, extracted: ExtractedSlots) -> FollowupPlan:
    """`extracted` is THIS turn's raw ExtractedSlots (before it gets merged
    onto TripState by app/utils/slot_filling.py's overwrite semantics) -
    classification has to look at what the user's message itself actually
    asked to change, not the already-merged state (which would show a
    "change" even when nothing was said, since carried-over values are
    already sitting there)."""
    text = user_input.lower()
    days = sorted({int(m) for m in _DAY_PATTERN.findall(user_input)})

    changed_a_real_field = any([
        extracted.destination, extracted.duration_days, extracted.budget,
        extracted.travelers, extracted.interests, extracted.must_avoid,
        extracted.origin_location,
    ])
    wants_different_places = any(phrase in text for phrase in _WANTS_DIFFERENT_PLACES_PHRASES)

    if changed_a_real_field or wants_different_places:
        return FollowupPlan(scope="full")

    cheaper = any(phrase in text for phrase in _CHEAPER_PHRASES)

    # A pace word with named days ("make day 2 more relaxed") lightens or
    # fills just those days. Without named days, pace is a whole-trip change
    # and still re-plans in full, as before.
    if days:
        delta = _density_delta(text, extracted)
        if delta:
            return FollowupPlan(scope="shape_only", target_days=days, cheaper=cheaper, density_delta=delta)
    if extracted.pace:
        return FollowupPlan(scope="full")
    # "Make it more relaxed" with no day named and nothing the model picked
    # up: slot_filling turns this into a trip-wide items_per_day change. An
    # extracted items_per_day(_delta) is already handled there, so it isn't
    # counted twice.
    whole_trip_delta = 0
    if not days and not extracted.items_per_day and not extracted.items_per_day_delta:
        whole_trip_delta = _density_delta(text, extracted)

    # Checked after the two "this is a real change" gates above, so a message
    # that both asks and instructs ("show the cost, and make it cheaper")
    # still re-plans rather than only answering. Only a pure question is
    # treated as read-only.
    if not cheaper and not days and any(phrase in text for phrase in _INFORMATIONAL_PHRASES):
        return FollowupPlan(scope="informational")
    if not cheaper and not days and _WEATHER_PATTERN.search(text):
        return FollowupPlan(scope="informational", info_kind="weather")

    return FollowupPlan(scope="shape_only", target_days=days or None, cheaper=cheaper, density_delta=whole_trip_delta)
