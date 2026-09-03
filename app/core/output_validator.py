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

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from app.core.scoring import haversine_km
from app.models.schemas import PlannerOutput, ItineraryDay, ItineraryItem

# Sri Lanka's real bounding box - matches the field-level constraint already
# on ItineraryItem.lat/lon (app/models/schemas.py), checked again here so a
# geo_in_country failure produces a clear, itemized message rather than a
# bare pydantic ValidationError with no repair guidance.
SRI_LANKA_LAT = (5.85, 9.95)
SRI_LANKA_LON = (79.5, 82.0)
GEO_NEAR_DEST_KM = 150.0
WEATHER_RAIN_THRESHOLD = 0.6
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

def _days_sequential(plan: PlannerOutput) -> bool:
    return [d.day for d in plan.itinerary] == list(range(1, len(plan.itinerary) + 1))


def _no_duplicates(plan: PlannerOutput) -> bool:
    for day in plan.itinerary:
        seen = set()
        for item in day.items:
            if item.listing_id is None:
                continue
            if item.listing_id in seen:
                return False
            seen.add(item.listing_id)
    return True


def _times_ordered(day: ItineraryDay) -> bool:
    times = [item.time for item in day.items]
    return times == sorted(times) and all(i.end_time > i.time for i in day.items)


def _cost_consistent(plan: PlannerOutput) -> bool:
    return abs(plan.estimated_cost - sum(d.day_cost for d in plan.itinerary)) < COST_TOLERANCE


def _cost_recomputes(plan: PlannerOutput, ctx: ValidationContext) -> bool:
    """The direct fix for the §12 case-1 failure (BUILD_PLAN's own
    verification run: the LLM picked a $$$$ hotel on a $500 budget and
    narrated past it in budget_notes instead of respecting it). The plan's
    claimed cost is recomputed from ctx.cost_lookup - real, tool-derived
    numbers - not from what the plan itself says. An LLM cannot narrate its
    way past an arithmetic check."""
    if not ctx.cost_lookup:
        return True   # nothing to recompute against - not this check's job to flag that
    recomputed = sum(ctx.cost_lookup.get(item.listing_id, 0.0) for item in _all_items(plan) if item.listing_id)
    return abs(plan.estimated_cost - recomputed) < COST_TOLERANCE


def _budget_honest(plan: PlannerOutput, ctx: ValidationContext) -> bool:
    return ctx.budget is None or plan.estimated_cost <= ctx.budget or bool(plan.budget_notes)


def _geo_in_country(plan: PlannerOutput) -> bool:
    return all(
        SRI_LANKA_LAT[0] <= i.lat <= SRI_LANKA_LAT[1] and SRI_LANKA_LON[0] <= i.lon <= SRI_LANKA_LON[1]
        for i in _all_items(plan)
    )


def _geo_near_dest(plan: PlannerOutput, ctx: ValidationContext) -> bool:
    return all(haversine_km(ctx.destination, {"lat": i.lat, "lon": i.lon}) <= GEO_NEAR_DEST_KM
              for i in _all_items(plan))


def _weather_respect(plan: PlannerOutput, ctx: ValidationContext) -> bool:
    for day in plan.itinerary:
        if ctx.per_day_rain_probability.get(day.date, 0.0) < WEATHER_RAIN_THRESHOLD:
            continue
        for item in day.items:
            if item.listing_id in ctx.outdoor_listing_ids:
                return False
    return True


def _disaster_avoid(plan: PlannerOutput, ctx: ValidationContext) -> bool:
    for item in _all_items(plan):
        for zone in ctx.disaster_red_zones:
            if haversine_km({"lat": item.lat, "lon": item.lon}, zone) <= DISASTER_RED_ZONE_KM:
                return False
    return True


def _must_avoid_respected(plan: PlannerOutput, ctx: ValidationContext) -> bool:
    return not any(item.listing_id in ctx.must_avoid_listing_ids for item in _all_items(plan))


def _currency_is_lkr(plan: PlannerOutput) -> bool:
    return plan.currency == "LKR" and all(i.currency == "LKR" for i in _all_items(plan))


# Named exactly as docs/master_plan/DETERMINISM_AND_VALIDATION.md §5 lists
# them, so a failure message's rule name is directly traceable to the spec.
_L2_RULES: list[tuple[str, callable]] = [
    ("day_count", lambda p, c: len(p.itinerary) == c.duration_days),
    ("dates_in_window", lambda p, c: all(d.date in c.valid_dates for d in p.itinerary)),
    ("days_sequential", lambda p, c: _days_sequential(p)),
    ("no_duplicates", lambda p, c: _no_duplicates(p)),
    ("times_ordered", lambda p, c: all(_times_ordered(d) for d in p.itinerary)),
    ("cost_consistent", lambda p, c: _cost_consistent(p)),
    ("cost_recomputes", lambda p, c: _cost_recomputes(p, c)),
    ("budget_honest", lambda p, c: _budget_honest(p, c)),
    ("geo_in_country", lambda p, c: _geo_in_country(p)),
    ("geo_near_dest", lambda p, c: _geo_near_dest(p, c)),
    ("weather_respect", lambda p, c: _weather_respect(p, c)),
    ("disaster_avoid", lambda p, c: _disaster_avoid(p, c)),
    ("must_avoid", lambda p, c: _must_avoid_respected(p, c)),
    ("currency", lambda p, c: _currency_is_lkr(p)),
]


def validate(plan: PlannerOutput, ctx: ValidationContext) -> ValidationResult:
    """Runs L1 then all of L2. Does not stop at the first failure - a
    repair prompt built from every failure at once is more useful (and
    cheaper, since it avoids repeated round trips) than fixing one thing,
    re-validating, finding the next thing, and so on."""
    failures = validate_referential(plan, ctx)
    for name, check in _L2_RULES:
        try:
            if not check(plan, ctx):
                failures.append(f"L2.{name}: failed")
        except Exception as e:
            failures.append(f"L2.{name}: check itself raised {type(e).__name__}: {e}")
    return ValidationResult(ok=not failures, failures=failures)
