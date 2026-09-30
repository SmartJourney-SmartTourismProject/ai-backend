"""
Output validation - L0/L1/L2, plus assembling the L3 repair prompt
(docs/master_plan/DETERMINISM_AND_VALIDATION.md §5, project concern #7).
Pure functions, no I/O, no LLM - total happy-path cost is microseconds, so
there's no reason to call an LLM to check whether an LLM's own output makes
sense.

Layers:
  L0 - schema validity. NOT reimplemented here - `with_structured_output()`
       already enforces this at the LangChain layer; a malformed response
       either raises pydantic.ValidationError there (treat that as an L0
       failure, go straight to repair) or you already have a valid
       PlannerOutput object by the time validate() below runs.
  L1 - referential. Every listing_id in the plan actually came from a
       real candidate this request saw - never a name the model recalled
       from memory or invented.
  L2 - business rules. The RULES list below, exactly as specified.
  L3 - one repair attempt (app/prompts/repair_prompt.py assembles the
       actual prompt text); a second failure goes to the deterministic
       fallback planner (app/core/fallback.py), never a second repair.

Live-wired by app/core/orchestrator.py's `_verify_node`, which calls
validate() against every planner-agent output (never against a fallback
plan - see `_verify_node`'s own docstring for why that's correct, not a gap).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from app.core.itinerary import DAY_END, DEFAULT_MAX_SINGLE_HOP_MINUTES, RAIN_FALLBACK_NOTE, allowed_hop_minutes
from app.core.scoring import haversine_km, haversine_minutes
from app.models.schemas import PlannerOutput, ItineraryDay, ItineraryItem

# Sri Lanka's real bounding box - matches the field-level constraint already
# on ItineraryItem.lat/lon (app/models/schemas.py), checked again here so a
# geo_in_country failure produces a clear, itemized message rather than a
# bare pydantic ValidationError with no repair guidance.
SRI_LANKA_LAT = (5.85, 9.95)
SRI_LANKA_LON = (79.5, 82.0)
GEO_NEAR_DEST_KM = 150.0
# Re-exported from the planner's definition rather than repeated: the plan
# is built against this number and then checked against it, so two copies
# drifting apart would make a plan fail a check it was built to pass.
from app.core.itinerary import RAIN_THRESHOLD as WEATHER_RAIN_THRESHOLD
DISASTER_RED_ZONE_KM = 50.0
COST_TOLERANCE = 1.0   # LKR - float rounding noise, not a real discrepancy


@dataclass
class ValidationContext:
    """Everything L1/L2 need to check a PlannerOutput against - the request
    this plan was actually built for, not the plan's own claims about itself."""
    duration_days: int
    valid_dates: set[str]                              # every ISO date actually in date_window
    budget: Optional[float]
    destination: dict                                  # {"lat":..., "lon":...}
    candidate_listing_ids: set[str]                     # every id this request's tool observations returned
    outdoor_listing_ids: set[str] = field(default_factory=set)   # ids tagged with an is_outdoor tag
    disaster_red_zones: list[dict] = field(default_factory=list) # [{"lat":..., "lon":...}]
    must_avoid_listing_ids: set[str] = field(default_factory=set)  # ids that violate a must_avoid tag
    per_day_rain_probability: dict[str, float] = field(default_factory=dict)
    cost_lookup: dict[str, float] = field(default_factory=dict)  # listing_id -> real recomputed cost
    # Kinds of stop the traveler ruled out ("viewpoints only"). Checked here
    # rather than only enforced per planner path: there are four paths that can
    # build a day, and "give view points only" shipped with three of them
    # honouring the request and the fourth quietly ignoring it.
    excluded_categories: set[str] = field(default_factory=set)
    # Part 4 (guardrails) - route/feasibility checks on the LLM planner's own
    # output, mirroring what app/core/itinerary.py already enforces
    # deterministically. All three default to None/absent and degrade to a
    # pass when absent (the _cost_recomputes precedent above - a caller
    # without this data, e.g. orchestrator.py's cost_lookup={} path or a
    # test fixture, shouldn't have that read as a false failure).
    day_end: Optional[str] = None                             # "HH:MM" curfew, e.g. itinerary.DAY_END
    max_single_hop_minutes: Optional[float] = None             # itinerary.DEFAULT_MAX_SINGLE_HOP_MINUTES
    expected_items_per_day: Optional[int] = None               # planner_shared.resolve_items_per_day(state)
    # Every place of a multi-place trip ("Galle and Matara"); a stop only
    # has to be near one of them. Empty = just `destination`.
    destinations: list[dict] = field(default_factory=list)


@dataclass
class ValidationResult:
    ok: bool
    failures: list[str] = field(default_factory=list)


def _all_items(plan: PlannerOutput) -> list[ItineraryItem]:
    return [item for day in plan.itinerary for item in day.items]


# ─────────────────────────── L1: referential ───────────────────────────────

def validate_referential(plan: PlannerOutput, ctx: ValidationContext) -> list[str]:
    failures = []
    for day in plan.itinerary:
        for item in day.items:
            if item.listing_id is None:
                if item.type != "travel":
                    failures.append(f"L1.listing_id: day {day.day} '{item.name}' has no listing_id but type={item.type!r}")
                continue
            if item.listing_id not in ctx.candidate_listing_ids:
                failures.append(
                    f"L1.listing_id: '{item.listing_id}' on day {day.day} ({item.name!r}) "
                    f"was never returned by a db_search_* observation"
                )
    return failures


# ─────────────────────────── L2: business rules ───────────────────────────

def _days_sequential(plan: PlannerOutput) -> Optional[str]:
    actual = [d.day for d in plan.itinerary]
    expected = list(range(1, len(plan.itinerary) + 1))
    if actual == expected:
        return None
    return f"day numbers are {actual}, expected {expected} (sequential from 1, no gaps or repeats)"


def _no_duplicates(plan: PlannerOutput) -> Optional[str]:
    """A repeated listing_id in a day is almost always a construction bug
    (the same hotel emitted at check-in AND check-out, an attraction picked
    twice) - except a restaurant, which real travellers genuinely do revisit
    within a day (the same place for both lunch and dinner) when it's the
    only real option nearby. app/core/itinerary.py's build_day_plan
    deliberately reuses a restaurant rather than skip a meal when the
    candidate pool is that thin (see its nearest_restaurant docstring) -
    found live (fallback investigation, 2026-09-25) that this correct
    behavior was being rejected by this rule as if it were the same bug as
    a duplicated hotel/attraction. Two separate real visits at two different
    times isn't a duplicate the way a hotel counted twice for one stay is."""
    for day in plan.itinerary:
        seen = set()
        for item in day.items:
            if item.listing_id is None or item.type == "restaurant":
                continue
            if item.listing_id in seen:
                return f"day {day.day} lists '{item.listing_id}' ({item.name!r}) more than once"
            seen.add(item.listing_id)
    return None


def _times_ordered(day: ItineraryDay) -> Optional[str]:
    times = [item.time for item in day.items]
    if times != sorted(times):
        return f"day {day.day}: items are not in time order ({times})"
    for i in day.items:
        if i.end_time <= i.time:
            return f"day {day.day}: '{i.name}' ends ({i.end_time}) at or before it starts ({i.time})"
    return None


def _cost_consistent(plan: PlannerOutput) -> Optional[str]:
    summed = sum(d.day_cost for d in plan.itinerary)
    if abs(plan.estimated_cost - summed) < COST_TOLERANCE:
        return None
    return f"estimated_cost ({plan.estimated_cost}) does not match the sum of day_cost values ({summed})"


def _cost_recomputes(plan: PlannerOutput, ctx: ValidationContext) -> Optional[str]:
    """The direct fix for the §12 case-1 failure (BUILD_PLAN's own
    verification run: the LLM picked a $$$$ hotel on a $500 budget and
    narrated past it in budget_notes instead of respecting it). The plan's
    claimed cost is recomputed from ctx.cost_lookup - real, tool-derived
    numbers - not from what the plan itself says. An LLM cannot narrate its
    way past an arithmetic check."""
    if not ctx.cost_lookup:
        return None   # nothing to recompute against - not this check's job to flag that
    recomputed = sum(ctx.cost_lookup.get(item.listing_id, 0.0) for item in _all_items(plan) if item.listing_id)
    if abs(plan.estimated_cost - recomputed) < COST_TOLERANCE:
        return None
    return f"estimated_cost ({plan.estimated_cost}) does not match the real per-item costs ({recomputed})"


def _budget_honest(plan: PlannerOutput, ctx: ValidationContext) -> Optional[str]:
    if ctx.budget is None or plan.estimated_cost <= ctx.budget or plan.budget_notes:
        return None
    over_by = plan.estimated_cost - ctx.budget
    return (
        f"estimated_cost ({plan.estimated_cost}) is {over_by:.2f} over the budget ({ctx.budget}) "
        f"and budget_notes is empty - explain the gap"
    )


def _geo_in_country(plan: PlannerOutput) -> Optional[str]:
    for i in _all_items(plan):
        if not (SRI_LANKA_LAT[0] <= i.lat <= SRI_LANKA_LAT[1] and SRI_LANKA_LON[0] <= i.lon <= SRI_LANKA_LON[1]):
            return f"'{i.name}' ({i.listing_id}) at ({i.lat}, {i.lon}) is outside Sri Lanka"
    return None


def _geo_near_dest(plan: PlannerOutput, ctx: ValidationContext) -> Optional[str]:
    centres = ctx.destinations or [ctx.destination]
    for i in _all_items(plan):
        dist = min(haversine_km(c, {"lat": i.lat, "lon": i.lon}) for c in centres)
        if dist > GEO_NEAR_DEST_KM:
            return f"'{i.name}' ({i.listing_id}) is {dist:.0f}km from the destination, over the {GEO_NEAR_DEST_KM:.0f}km limit"
    return None


def _weather_respect(plan: PlannerOutput, ctx: ValidationContext) -> Optional[str]:
    for day in plan.itinerary:
        if ctx.per_day_rain_probability.get(day.date, 0.0) < WEATHER_RAIN_THRESHOLD:
            continue
        attractions = [i for i in day.items if i.type == "attraction"]
        for item in day.items:
            if item.listing_id in ctx.outdoor_listing_ids:
                # The builder's rain fallback (app/core/itinerary.py): one
                # outdoor stop kept, clearly noted, on a day that would
                # otherwise have no sightseeing at all. Accepted only in that
                # exact shape - the note alone can't carry a full outdoor day.
                if RAIN_FALLBACK_NOTE in (item.notes or "") and len(attractions) == 1:
                    continue
                return (
                    f"day {day.day} ({day.date}) has rain_probability "
                    f">= {WEATHER_RAIN_THRESHOLD} but still schedules outdoor item "
                    f"'{item.name}' ({item.listing_id})"
                )
    return None


def _disaster_avoid(plan: PlannerOutput, ctx: ValidationContext) -> Optional[str]:
    for item in _all_items(plan):
        for zone in ctx.disaster_red_zones:
            dist = haversine_km({"lat": item.lat, "lon": item.lon}, zone)
            if dist <= DISASTER_RED_ZONE_KM:
                return f"'{item.name}' ({item.listing_id}) is {dist:.0f}km from a disaster zone, inside the {DISASTER_RED_ZONE_KM:.0f}km exclusion radius"
    return None


def _must_avoid_respected(plan: PlannerOutput, ctx: ValidationContext) -> Optional[str]:
    for item in _all_items(plan):
        if item.listing_id in ctx.must_avoid_listing_ids:
            return f"'{item.name}' ({item.listing_id}) matches a must_avoid tag"
    return None


def _categories_respected(plan: PlannerOutput, ctx: ValidationContext) -> Optional[str]:
    """No stop of a kind the traveler ruled out.

    This is the check that makes the requirement hold for every planner path at
    once, including ones not written yet - which is the point. Enforcing it
    only where each path builds its constraints means each new path has to
    remember, and the LLM tool path did not.
    """
    if not ctx.excluded_categories:
        return None
    offenders = [i.name for i in _all_items(plan) if i.type in ctx.excluded_categories]
    if offenders:
        kinds = ", ".join(sorted(ctx.excluded_categories))
        return f"plan contains {kinds} the traveler excluded: {', '.join(offenders[:3])}"
    return None


def _currency_is_lkr(plan: PlannerOutput) -> Optional[str]:
    if plan.currency != "LKR":
        return f"plan currency is {plan.currency!r}, expected 'LKR'"
    for i in _all_items(plan):
        if i.currency != "LKR":
            return f"'{i.name}' ({i.listing_id}) has currency {i.currency!r}, expected 'LKR'"
    return None


def _days_have_items(plan: PlannerOutput) -> Optional[str]:
    """Live-found regression (itinerary-quality/token-reduction pass): A1
    dropped ItineraryDay.items' schema-level `min_length=1` (a business
    rule, not something that should live in a schema that has to survive
    provider-side structured decoding - see schemas.py's own comment on
    why). Nothing else in L1/L2 ever required a day to have at least one
    item - `_times_ordered`/`_cost_consistent` are all vacuously true on an
    empty list - so a repair call was observed live producing
    `items: []` with the PREVIOUS attempt's day_cost carried over
    unchanged, and it passed every existing check. A day with a nonzero
    cost and zero items is not a plan; this is the L2-layer equivalent of
    the schema constraint that was removed."""
    empty = [d.day for d in plan.itinerary if len(d.items) == 0]
    if not empty:
        return None
    return f"day(s) {empty} have no items"


def _day_ends_by_curfew(plan: PlannerOutput, ctx: ValidationContext) -> Optional[str]:
    """Part 4 (guardrails): the deterministic path (app/core/itinerary.py)
    enforces DAY_END by construction now; nothing previously checked
    whether the LLM planner's OWN output respects the same curfew - it
    could freely claim a day ending at 23:40 and pass every other rule.
    Compares the MAX end_time in the day, not just the last list entry -
    this check runs independently of times_ordered (validate() doesn't
    stop at the first failure), so the list isn't guaranteed sorted yet
    when this evaluates."""
    if ctx.day_end is None:
        return None
    for day in plan.itinerary:
        if not day.items:
            continue
        latest = max(i.end_time for i in day.items)
        if latest > ctx.day_end:
            return f"day {day.day} ends at {latest}, past the {ctx.day_end} curfew - drop or move the last item(s)"
    return None


def _no_absurd_hop(plan: PlannerOutput, ctx: ValidationContext) -> Optional[str]:
    """Part 4 (guardrails): the deterministic path's max_single_hop_minutes
    cap (the direct fix for the Rangala Natural Pool case - a single
    unreasonable hop placed early in the day, before any cumulative cap
    would catch it) only ever applied to build_day_plan's own construction;
    the LLM planner's finished output was never checked against it at all."""
    if ctx.max_single_hop_minutes is None:
        return None
    for day in plan.itinerary:
        items = day.items
        for prev, cur in zip(items, items[1:]):
            hop = haversine_minutes({"lat": prev.lat, "lon": prev.lon}, {"lat": cur.lat, "lon": cur.lon})
            # Same per-pair rule the builder uses (itinerary.allowed_hop_minutes):
            # a stop the sparse-area rule admitted may be a longer drive.
            if hop > allowed_hop_minutes(ctx.max_single_hop_minutes, prev.notes, cur.notes):
                return (
                    f"day {day.day}: the hop from '{prev.name}' to '{cur.name}' is ~{hop:.0f} min, "
                    f"over the {ctx.max_single_hop_minutes:.0f} min single-hop cap"
                )
    return None


def _items_per_day_respected(plan: PlannerOutput, ctx: ValidationContext) -> Optional[str]:
    """Part 4 (guardrails): a follow-up asking for fewer destinations per
    day (Part 3) only actually means something if the planner's output is
    CHECKED against it - `<=`, not `==`, since weather/price/feasibility
    drops legitimately produce a day with fewer attractions than requested,
    and that's correct behavior, not a violation."""
    if ctx.expected_items_per_day is None:
        return None
    for day in plan.itinerary:
        attraction_count = sum(1 for i in day.items if i.type == "attraction")
        if attraction_count > ctx.expected_items_per_day:
            return f"day {day.day} has {attraction_count} attractions, over the requested {ctx.expected_items_per_day}/day"
    return None


def _day_count(plan: PlannerOutput, ctx: ValidationContext) -> Optional[str]:
    if len(plan.itinerary) == ctx.duration_days:
        return None
    return f"plan has {len(plan.itinerary)} day(s), the trip is {ctx.duration_days} day(s)"


def _dates_in_window(plan: PlannerOutput, ctx: ValidationContext) -> Optional[str]:
    bad = [(d.day, d.date) for d in plan.itinerary if d.date not in ctx.valid_dates]
    if not bad:
        return None
    return f"day(s) {bad} use a date outside the trip's real window ({sorted(ctx.valid_dates)})"


# Named exactly as docs/master_plan/DETERMINISM_AND_VALIDATION.md §5 lists
# them, so a failure message's rule name is directly traceable to the spec.
# Each check returns None on a pass, or a specific, human-readable detail
# string on a failure - not just a bare "failed" (fallback investigation,
# 2026-09-25: the repair prompt's FAILURES list was, for every L2 rule,
# nothing but "L2.<name>: failed", with no day, item, or actual value - the
# repair model had to guess what to change from the rule's NAME alone).
_L2_RULES: list[tuple[str, callable]] = [
    ("day_count", _day_count),
    ("dates_in_window", _dates_in_window),
    ("days_sequential", lambda p, c: _days_sequential(p)),
    ("days_have_items", lambda p, c: _days_have_items(p)),
    ("no_duplicates", lambda p, c: _no_duplicates(p)),
    ("times_ordered", lambda p, c: next((f for f in (_times_ordered(d) for d in p.itinerary) if f), None)),
    ("cost_consistent", lambda p, c: _cost_consistent(p)),
    ("cost_recomputes", lambda p, c: _cost_recomputes(p, c)),
    ("budget_honest", lambda p, c: _budget_honest(p, c)),
    ("geo_in_country", lambda p, c: _geo_in_country(p)),
    ("geo_near_dest", lambda p, c: _geo_near_dest(p, c)),
    ("weather_respect", lambda p, c: _weather_respect(p, c)),
    ("disaster_avoid", lambda p, c: _disaster_avoid(p, c)),
    ("must_avoid", lambda p, c: _must_avoid_respected(p, c)),
    ("categories_respected", lambda p, c: _categories_respected(p, c)),
    ("currency", lambda p, c: _currency_is_lkr(p)),
    ("day_ends_by_curfew", lambda p, c: _day_ends_by_curfew(p, c)),
    ("no_absurd_hop", lambda p, c: _no_absurd_hop(p, c)),
    ("items_per_day_respected", lambda p, c: _items_per_day_respected(p, c)),
]


def validate(plan: PlannerOutput, ctx: ValidationContext) -> ValidationResult:
    """Runs L1 then all of L2. Does not stop at the first failure - a
    repair prompt built from every failure at once is more useful (and
    cheaper, since it avoids repeated round trips) than fixing one thing,
    re-validating, finding the next thing, and so on."""
    failures = validate_referential(plan, ctx)
    for name, check in _L2_RULES:
        try:
            detail = check(plan, ctx)
            if detail:
                failures.append(f"L2.{name}: {detail}")
        except Exception as e:
            failures.append(f"L2.{name}: check itself raised {type(e).__name__}: {e}")
    return ValidationResult(ok=not failures, failures=failures)


# ─────────────────────── targeted deterministic repair ─────────────────────

# The L2 rules whose failure message names a specific day AND whose
# violation build_day_plan already prevents by construction (app/core/
# itinerary.py) - a full re-run of that same deterministic tool for just
# that one day is guaranteed to fix it, with no LLM call needed at all.
# Deliberately excludes anything cross-day (day_count, days_sequential,
# cost_consistent/cost_recomputes sum over the WHOLE trip, budget_honest is
# about the narrative field, not a day's construction) and anything
# item-scoped rather than day-scoped (geo_in_country/geo_near_dest/
# disaster_avoid/must_avoid name an item, not reliably a day a rebuild can
# fix - the same candidate pool might just re-select the same bad item).
_DAY_SCOPED_L2_RULES = frozenset({
    "day_ends_by_curfew", "no_absurd_hop", "no_duplicates",
    "times_ordered", "weather_respect", "items_per_day_respected",
})
_DAY_NUM_RE = re.compile(r"^day (\d+)\b")
_DATES_IN_WINDOW_DAY_RE = re.compile(r"\((\d+),")


def day_scoped_repair_target(failures: list[str]) -> Optional[set[int]]:
    """Returns the exact day number(s) a deterministic build_day_plan
    rebuild can fix, for app/core/orchestrator.py's `_repair_node` fast path -
    or None when it isn't safe to guess, which means "do a real LLM repair
    instead". Deliberately conservative: EVERY failure in the list must be
    both a recognized day-scoped rule (_DAY_SCOPED_L2_RULES, or
    dates_in_window's own list-of-(day, date) format) and one whose message
    actually names the day(s) it's about - one unrecognized failure bails
    the whole batch to None, since silently rebuilding only SOME of what's
    broken (or guessing the wrong day) would ship a plan that still fails
    the checks it never even looked at."""
    days: set[int] = set()
    for f in failures:
        if f.startswith("L2.dates_in_window:"):
            found = _DATES_IN_WINDOW_DAY_RE.findall(f)
            if not found:
                return None
            days.update(int(d) for d in found)
            continue
        matched = False
        for rule in _DAY_SCOPED_L2_RULES:
            prefix = f"L2.{rule}:"
            if f.startswith(prefix):
                m = _DAY_NUM_RE.match(f[len(prefix):].strip())
                if not m:
                    return None
                days.add(int(m.group(1)))
                matched = True
                break
        if not matched:
            return None
    return days or None
