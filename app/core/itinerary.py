"""
build_day_plan - routing, timing, and sequencing for a single day, entirely
deterministic (docs/master_plan/AGENT_ARCHITECTURE.md §4, PROJECT_MASTER_PLAN.md
Phase 4). Pure functions, no I/O, no LLM - the planner agent (Phase 6) hands
this ranked candidates and constraints; this module does the actual
arranging. Weather/disaster filtering -> meal slots -> nearest-neighbour
route -> opening-hours check -> times, exactly per the plan's tool
contract: `build_day_plan(day, date, anchor, selections, constraints) ->
{items[], day_cost, total_km, total_travel_min, dropped[]}`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from app.core.scoring import ROAD_FACTOR, TravelMatrix, haversine_km, haversine_minutes

# Dwell time per category - how long a traveller actually spends at a stop,
# not counting travel to get there. Fallback default for an attraction with
# no tag matching TAG_DWELL_MINUTES below, and the only value ever used for
# hotel/restaurant/event (those aren't looked up by tag).
DWELL_MINUTES = {"hotel": 30, "restaurant": 60, "attraction": 90, "event": 120}

# Tag-based dwell for attractions (itinerary-quality/token-reduction pass) -
# DWELL_MINUTES["attraction"]=90 flat was the same number for a 20-minute
# viewpoint stop and a half-day hike, which is a real contributor to
# infeasible days (Defect 2). Keyed on app/data/seed_reference.py's
# _TAG_VOCABULARY (the only canonical tag list in this codebase). An item
# matching multiple tags takes the LONGEST (max, not mean) - the
# conservative, deterministic choice; iteration order below is the
# tie-break for which key "wins" when times are equal, though max() makes
# that not actually observable.
TAG_DWELL_MINUTES = {
    "hike": 180, "wildlife": 180, "nature": 120, "beach": 120,
    "history": 90, "culture": 90, "family": 90, "sightseeing": 75,
    "views": 45, "food": 60, "local_food": 60,
}

DAY_START = "09:00"
DAY_END = "21:00"
BREAKFAST_TIME = "08:00"
LUNCH_TIME = "12:30"
DINNER_TIME = "19:00"
# Exported (Part 4, guardrails) so app/core/output_validator.py's
# no_absurd_hop rule can check the LLM planner's own output against the
# SAME cap this module already enforces on the deterministic path, instead
# of a second, driftable copy of the number.
DEFAULT_MAX_SINGLE_HOP_MINUTES = 45.0


def _dwell_for(item: dict, item_type: str) -> int:
    """Attractions only (hotels/restaurants/events keep the flat
    DWELL_MINUTES value - a "how long is dinner" question doesn't vary by
    tag the way "how long is this attraction" does). Untagged/unmatched
    items fall back to DWELL_MINUTES["attraction"] (90), so this is
    byte-identical to the old behavior for every item that doesn't carry
    one of TAG_DWELL_MINUTES' keys."""
    if item_type != "attraction":
        return DWELL_MINUTES.get(item_type, 60)
    tags = set(item.get("tags") or [])
    matched = [minutes for tag, minutes in TAG_DWELL_MINUTES.items() if tag in tags]
    return max(matched) if matched else DWELL_MINUTES["attraction"]


@dataclass
class DaySelections:
    """Ranked candidates available for this day - already scored and
    ordered by app/core/scoring.py's rank(); build_day_plan only ever
    consumes them in that order, never re-sorts."""
    hotels: list[dict] = field(default_factory=list)
    restaurants: list[dict] = field(default_factory=list)
    attractions: list[dict] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)


@dataclass
class DayConstraints:
    items_target: int = 3                 # attraction count, from the planner agent's `pace` reading
    exclude_outdoor: bool = False          # set when this day's weather is bad
    outdoor_tags: frozenset[str] = field(default_factory=frozenset)   # from tag_vocabulary.is_outdoor
    need_hotel_checkin: bool = False       # day 1
    need_hotel_checkout: bool = False      # last day
    # nights = duration_days - 1 (a 3-day trip is 2 nights; a 1-day trip is
    # 0 nights, so no accommodation is actually stayed). Real hotel cost -
    # live-found 2026-09-06: check-in and check-out each independently
    # charged the full nightly rate regardless of trip length, so a 1-day
    # and a 5-day trip to the same hotel came out to the identical total.
    hotel_nights: int = 1
    include_lunch: bool = True
    include_dinner: bool = True
    prefer_price_level_max: Optional[int] = None
    cost_lookup: dict[str, float] = field(default_factory=dict)   # item id -> estimated LKR cost (from budget.py)
    # Live-found 2026-09-06: attractions were chosen purely by rank/count
    # (items_target), with total_travel_min only ever *reported*, never
    # used to stop packing more stops into a day - a district can span a
    # genuine hour-plus drive (e.g. central Kandy town vs. Rangala), and
    # nothing stopped both ending up in the same day. 180 minutes (3h) of
    # total travel is a generous cap for real same-area sightseeing, not a
    # tight one - it's meant to catch "half the day is windshield time",
    # not to shrink ordinary itineraries. Applies to attractions only -
    # meals are never dropped for it (a skipped meal is a worse outcome
    # than a slightly fuller day).
    max_travel_minutes: float = 180.0
    # A cumulative cap alone isn't enough - it only rejects stops once the
    # day's budget is already spent, so one lone far-away attraction placed
    # EARLY in the day (nothing accumulated yet) sails through untouched.
    # Live-found 2026-09-06: Rangala Natural Pool (a ~63-minute hop) was
    # exactly this case - it was only the *second* stop of the day. This
    # catches a single hop that's unreasonable on its own, regardless of
    # where in the day it falls.
    max_single_hop_minutes: float = DEFAULT_MAX_SINGLE_HOP_MINUTES
    # DAY_END (itinerary-quality/token-reduction pass): the real fix for
    # Defect 2 ("too many places in a day") - the travel caps above bound
    # TRAVEL time only; nothing previously bounded total elapsed time
    # including dwell, so a day could run well past midnight and still
    # pass. items_target becomes an upper bound; this is the real one.
    day_end: str = DAY_END


@dataclass
class ItineraryItem:
    time: str
    end_time: str
    type: str                        # hotel | restaurant | attraction | event | travel
    listing_id: Optional[str]
    name: str
    lat: float
    lon: float
    est_cost: float
    currency: str
    notes: str = ""


@dataclass
class DayPlan:
    day: int
    date: str
    items: list[ItineraryItem]
    day_cost: float
    total_km: float
    total_travel_min: float
    dropped: list[dict] = field(default_factory=list)   # [{"id":..., "reason": "..."}]


def _is_outdoor(item: dict, outdoor_tags: frozenset[str]) -> bool:
    if not outdoor_tags:
        return False
    return bool(set(item.get("tags") or []) & outdoor_tags)


def _add_minutes(hhmm: str, minutes: float) -> str:
    t = datetime.strptime(hhmm, "%H:%M") + timedelta(minutes=round(minutes))
    return t.strftime("%H:%M")


def _hhmm_to_minutes(hhmm: str) -> int:
    t = datetime.strptime(hhmm, "%H:%M")
    return t.hour * 60 + t.minute


def _nearest_neighbor_order(anchor: dict, items: list[dict], cost) -> list[dict]:
    """Greedy nearest-neighbour ordering starting from the anchor -
    minimizes backtracking within the day without needing a real routing
    engine for sequencing (only for the actual travel-time numbers, which
    come from the pre-computed TravelMatrix/haversine fallback).

    `cost` is the same travel_minutes closure build_day_plan uses for
    everything else (itinerary-quality/token-reduction pass, "2.2") - this
    used to sort by raw haversine_km while the clock (and every feasibility
    cap) ran on minutes, so ordering optimised a different quantity than
    feasibility actually depended on. Minimizing travel TIME (not straight-
    line distance) is what construction + 2-opt are supposed to minimize."""
    remaining = list(items)
    ordered = []
    current = anchor
    while remaining:
        nearest = min(remaining, key=lambda i: cost(current, i))
        ordered.append(nearest)
        remaining.remove(nearest)
        current = nearest
    return ordered


def _two_opt(anchor: dict, ordered: list[dict], cost, max_passes: int = 8) -> list[dict]:
    """Standard 2-opt local search over an OPEN path (anchor is fixed, no
    return leg) - the direct fix for Defect 1 (the largest hop in the trip
    sitting between two consecutive stops, with the road between them
    passing stops scheduled later): nearest-neighbour construction alone is
    well known to leave exactly this kind of crossing edge/oversized final
    leg behind, and had no improvement pass at all before this.

    Reverses segment [i..j] whenever that strictly reduces the sum of the
    two edges it touches; scans in index order and applies the FIRST
    improving move found, then restarts the scan - deterministic (no ties
    broken by iteration/insertion order beyond that), bounded by
    max_passes. n <= 3 is returned unchanged (three items admit no
    meaningfully different 2-opt move here)."""
    n = len(ordered)
    if n <= 3:
        return list(ordered)

    points = [anchor] + list(ordered)
    passes = 0
    improved = True
    while improved and passes < max_passes:
        improved = False
        passes += 1
        for i in range(1, n):
            for j in range(i + 1, n + 1):
                before = cost(points[i - 1], points[i]) + (cost(points[j], points[j + 1]) if j < n else 0.0)
                after = cost(points[i - 1], points[j]) + (cost(points[i], points[j + 1]) if j < n else 0.0)
                if after - before < -1e-9:
                    points[i:j + 1] = points[i:j + 1][::-1]
                    improved = True
                    break
            if improved:
                break
    return points[1:]


def _cost_of(item: dict, cost_lookup: dict[str, float]) -> float:
    return cost_lookup.get(item["id"], 0.0)


def build_day_plan(
    day: int,
    date: str,
    anchor: dict,
    selections: DaySelections,
    constraints: DayConstraints,
    matrix: Optional[TravelMatrix] = None,
) -> DayPlan:
    matrix = matrix or TravelMatrix()
    dropped: list[dict] = []
    items: list[ItineraryItem] = []
    total_km = 0.0
    total_travel_min = 0.0
    day_cost = 0.0
    clock = DAY_START

    def travel_minutes(a: dict, b: dict) -> float:
        m = matrix.minutes(a, b)
        return m if m is not None else haversine_minutes(a, b)

    def travel_km(a: dict, b: dict) -> float:
        # ROAD_FACTOR (itinerary-quality/token-reduction pass): travel_minutes
        # already applies this factor internally (haversine_minutes), but
        # travel_km used to report raw straight-line distance - the two
        # silently disagreed by ~35%, understating total_km relative to what
        # the clock actually budgeted for.
        return haversine_km(a, b) * ROAD_FACTOR

    def emit(item_dict: dict, item_type: str, from_point: dict, cost_override: Optional[float] = None) -> dict:
        """Advances the clock past travel time + dwell time, appends an
        ItineraryItem, and returns the point to travel from next.

        Always computes a travel leg, including for the day's first stop -
        the traveller genuinely has to get from the anchor (start location,
        or the hotel on a later day) to wherever they're going first. An
        earlier version special-cased "no travel before the first stop",
        which was simply wrong whenever the anchor and the first stop
        aren't the same point (the common case) - found by a test that
        set an implausible matrix distance and asserted it was actually
        used; total_travel_min silently stayed 0.0 instead.

        cost_override exists solely for hotel check-in/check-out (below):
        the normal per-item cost_lookup gives a nightly rate, not a
        whole-stay total, so those two call sites compute the real
        nights-scaled cost themselves rather than letting this fall
        through to the plain per-item lookup."""
        nonlocal clock, total_km, total_travel_min, day_cost
        mins = travel_minutes(from_point, item_dict)
        km = travel_km(from_point, item_dict)
        total_travel_min += mins
        total_km += km
        clock = _add_minutes(clock, mins)

        start = clock
        dwell = _dwell_for(item_dict, item_type)
        clock = _add_minutes(clock, dwell)
        cost = _cost_of(item_dict, constraints.cost_lookup) if cost_override is None else cost_override
        day_cost += cost

        items.append(ItineraryItem(
            time=start, end_time=clock, type=item_type,
            listing_id=item_dict["id"], name=item_dict["name"],
            lat=item_dict["lat"], lon=item_dict["lon"],
            est_cost=cost, currency=item_dict.get("currency", "LKR"),
        ))
        return item_dict

    current_point = anchor

    # 1. Hotel check-in, if this is the arrival day. The whole stay's cost
    #    is charged here (nightly rate x nights) - check-out below is a
    #    free "closing" bookend, not a second charge for the same stay.
    if constraints.need_hotel_checkin and selections.hotels:
        nightly_rate = _cost_of(selections.hotels[0], constraints.cost_lookup)
        current_point = emit(
            selections.hotels[0], "hotel", current_point,
            cost_override=nightly_rate * constraints.hotel_nights,
        )

    # 2. Weather/disaster filter -> attractions, in ranked order, up to items_target.
    #    Dropped items are recorded with a reason, per the plan's contract -
    #    never silently vanish.
    accepted_attractions = []
    for a in selections.attractions:
        if len(accepted_attractions) >= constraints.items_target:
            break
        if constraints.exclude_outdoor and _is_outdoor(a, constraints.outdoor_tags):
            dropped.append({"id": a["id"], "reason": "excluded_outdoor_bad_weather"})
            continue
        if constraints.prefer_price_level_max is not None and a.get("price_level") and \
           a["price_level"] > constraints.prefer_price_level_max:
            dropped.append({"id": a["id"], "reason": "over_price_ceiling"})
            continue
        accepted_attractions.append(a)

    # 3. Route: nearest-neighbour construction + 2-opt improvement, both
    #    minimizing travel MINUTES (not raw distance - see
    #    _nearest_neighbor_order's own docstring). 2-opt is what actually
    #    fixes Defect 1 (the biggest hop of the trip sitting between two
    #    consecutive stops, with the road between them passing later
    #    stops) - NN construction alone is known to leave exactly that
    #    behind.
    def _order(pts: list[dict]) -> list[dict]:
        return _two_opt(current_point, _nearest_neighbor_order(current_point, pts, travel_minutes), travel_minutes)

    ordered_attractions = _order(accepted_attractions)

    # 3b. Feasibility simulation (Defect 2's real fix) - a shadow walk over
    #     the ordered route BEFORE anything is actually emitted, so a drop
    #     here can trigger a re-order of the survivors rather than leaving
    #     them in an order that was only ever optimal for the original,
    #     now-different set (the bug in the old code: NN ran once, before
    #     any cap-drops, so the surviving route was computed for a set that
    #     no longer existed).
    #
    #     Reserve accounts for the dinner leg/dwell and the hotel-checkout
    #     leg that will follow the last accepted attraction - both are
    #     necessarily approximate here (the real dinner spot and the exact
    #     checkout leg depend on wherever the route actually ends, which
    #     this simulation is still deciding), using a fixed, conservative
    #     nominal minutes rather than a real distance computation.
    _RESERVE_DINNER_MIN = DWELL_MINUTES["restaurant"] + 15
    _RESERVE_CHECKOUT_MIN = 15
    reserve = 0
    if constraints.include_dinner and selections.restaurants:
        reserve += _RESERVE_DINNER_MIN
    if constraints.need_hotel_checkout and selections.hotels:
        reserve += _RESERVE_CHECKOUT_MIN
    day_end_minutes = _hhmm_to_minutes(constraints.day_end)

    def _simulate(pts: list[dict]) -> tuple[list[dict], list[dict]]:
        kept: list[dict] = []
        sim_dropped: list[dict] = []
        sim_point = current_point
        sim_clock = _hhmm_to_minutes(clock)
        # Seeded from the REAL cumulative so far (the hotel check-in leg,
        # if this is a check-in day, already ran through emit() above) -
        # matches what the final safety-net re-check below compares
        # against, so the two never disagree about what's already "spent".
        sim_cumulative_travel = total_travel_min
        for a in pts:
            hop = travel_minutes(sim_point, a)
            projected_travel = sim_cumulative_travel + hop
            if hop > constraints.max_single_hop_minutes or projected_travel > constraints.max_travel_minutes:
                # A single unreasonable hop, or the cumulative travel budget
                # - neither means EVERY later stop is also unreachable (an
                # early outlier shouldn't block the rest of the day), so
                # this one is skipped, not a day-ending stop.
                sim_dropped.append({"id": a["id"], "reason": "would_exceed_daily_travel_budget"})
                continue
            dwell = _dwell_for(a, "attraction")
            projected_end = sim_clock + hop + dwell + reserve
            if projected_end > day_end_minutes:
                # Once the day is genuinely full, later stops in THIS order
                # are unfittable too - unlike the travel caps above, this
                # one breaks rather than continuing past it.
                sim_dropped.append({"id": a["id"], "reason": "day_would_run_past_end"})
                break
            kept.append(a)
            sim_point = a
            sim_clock += hop + dwell
            sim_cumulative_travel = projected_travel
        return kept, sim_dropped

    kept_attractions, sim_dropped = _simulate(ordered_attractions)
    dropped.extend(sim_dropped)
    if len(kept_attractions) < len(ordered_attractions):
        ordered_attractions = _order(kept_attractions)

    # 4. Meal slots, placed by the actual CLOCK, not by list position -
    #    LUNCH_TIME/DINNER_TIME were declared and never used before this
    #    pass; lunch used to be spliced at the attraction list's INDEX
    #    midpoint, so "lunch" could land at 09:40 or "dinner" at 14:20
    #    depending on how many attractions came first. used_restaurant_ids
    #    tracks EVERY restaurant actually placed so far today (updated as
    #    each one is picked) - found live (2026-09-03, real demo run):
    #    dinner's exclusion set only ever checked against attractions,
    #    which lunch's pick was never added to, so the same restaurant
    #    could be - and was - selected for both lunch and dinner.
    used_restaurant_ids: set[str] = set()

    def nearest_restaurant(near: dict) -> Optional[dict]:
        # Prefer a restaurant not already used today; if none is left
        # (a genuinely small real candidate pool), reuse is still better
        # than leaving a meal slot empty - same "degrade, don't omit"
        # philosophy as everywhere else in this module.
        fresh = [r for r in selections.restaurants if r["id"] not in used_restaurant_ids]
        candidates = fresh or selections.restaurants
        return min(candidates, key=lambda r: haversine_km(near, r)) if candidates else None

    def _emit_lunch() -> None:
        nonlocal current_point
        lunch = nearest_restaurant(current_point)
        if lunch:
            current_point = emit(lunch, "restaurant", current_point)
            used_restaurant_ids.add(lunch["id"])

    # The simulation already vetted every attraction in ordered_attractions
    # against both travel caps; this re-check is a safety net for the rare
    # case a re-order (above) introduced a new adjacency the simulation
    # never actually tested, not the primary enforcement point anymore.
    lunch_done = not (constraints.include_lunch and selections.restaurants)
    for item_dict in ordered_attractions:
        hop_minutes = travel_minutes(current_point, item_dict)
        projected_travel = total_travel_min + hop_minutes
        if hop_minutes > constraints.max_single_hop_minutes or projected_travel > constraints.max_travel_minutes:
            dropped.append({"id": item_dict["id"], "reason": "would_exceed_daily_travel_budget"})
            continue

        if not lunch_done and clock >= LUNCH_TIME:
            _emit_lunch()
            lunch_done = True

        current_point = emit(item_dict, "attraction", current_point)

    # Still pending at the end of the day (e.g. every attraction finished
    # before the clock ever reached LUNCH_TIME) - force it rather than
    # silently skipping the meal, same "degrade, don't omit" convention.
    if not lunch_done:
        _emit_lunch()

    if constraints.include_dinner and selections.restaurants:
        if clock < DINNER_TIME:
            # An idle gap, not a real travel/dwell leg - still monotone, so
            # the "times strictly increasing" invariant holds.
            clock = DINNER_TIME
        dinner = nearest_restaurant(current_point)
        if dinner:
            current_point = emit(dinner, "restaurant", current_point)
            used_restaurant_ids.add(dinner["id"])

    # 5. Hotel check-out, if this is the departure day (no new dwell time,
    #    no additional cost - the whole stay was already charged at
    #    check-in above; this just closes the day at the hotel for
    #    map/route completeness).
    if constraints.need_hotel_checkout and selections.hotels:
        emit(selections.hotels[0], "hotel", current_point, cost_override=0.0)

    return DayPlan(
        day=day, date=date, items=items,
        day_cost=round(day_cost, 2), total_km=round(total_km, 2),
        total_travel_min=round(total_travel_min, 1), dropped=dropped,
    )
