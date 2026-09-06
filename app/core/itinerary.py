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

from app.core.scoring import TravelMatrix, haversine_km, haversine_minutes

# Dwell time per category - how long a traveller actually spends at a stop,
# not counting travel to get there.
DWELL_MINUTES = {"hotel": 30, "restaurant": 60, "attraction": 90, "event": 120}

DAY_START = "09:00"
BREAKFAST_TIME = "08:00"
LUNCH_TIME = "12:30"
DINNER_TIME = "19:00"


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
    max_single_hop_minutes: float = 45.0


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


def _nearest_neighbor_order(anchor: dict, items: list[dict]) -> list[dict]:
    """Greedy nearest-neighbour ordering starting from the anchor -
    minimizes backtracking within the day without needing a real routing
    engine for sequencing (only for the actual travel-time numbers, which
    come from the pre-computed TravelMatrix/haversine fallback)."""
    remaining = list(items)
    ordered = []
    current = anchor
    while remaining:
        nearest = min(remaining, key=lambda i: haversine_km(current, i))
        ordered.append(nearest)
        remaining.remove(nearest)
        current = nearest
    return ordered


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
        return haversine_km(a, b)

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
        dwell = DWELL_MINUTES.get(item_type, 60)
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

    # 3. Route: nearest-neighbour from the current anchor through
    #    attractions/restaurant, minimizing backtracking.

    # 4. Meal slots - lunch/dinner inserted from the ranked restaurant list,
    #    nearest to wherever the route is passing through.
    #
    #    used_restaurant_ids tracks EVERY restaurant actually placed so far
    #    today (updated as each one is picked, not just fixed at attraction-
    #    list-creation time) - found live (2026-09-03, real demo run): dinner's
    #    exclusion set only ever checked against `stops` (attractions),
    #    which lunch's pick was never added to, so the same restaurant could
    #    be - and was - selected for both lunch and dinner on the same day.
    used_restaurant_ids: set[str] = set()

    def nearest_restaurant(near: dict) -> Optional[dict]:
        # Prefer a restaurant not already used today; if none is left
        # (a genuinely small real candidate pool), reuse is still better
        # than leaving a meal slot empty - same "degrade, don't omit"
        # philosophy as everywhere else in this module.
        fresh = [r for r in selections.restaurants if r["id"] not in used_restaurant_ids]
        candidates = fresh or selections.restaurants
        return min(candidates, key=lambda r: haversine_km(near, r)) if candidates else None

    ordered_attractions = _nearest_neighbor_order(current_point, accepted_attractions)
    route: list[tuple[dict, str]] = []
    for i, a in enumerate(ordered_attractions):
        route.append((a, "attraction"))
        # insert lunch roughly at the midpoint of the day's attractions
        if constraints.include_lunch and i == len(ordered_attractions) // 2 and selections.restaurants:
            lunch = nearest_restaurant(a)
            if lunch:
                route.append((lunch, "restaurant"))
                used_restaurant_ids.add(lunch["id"])

    if constraints.include_dinner and selections.restaurants:
        dinner_anchor = route[-1][0] if route else current_point
        dinner = nearest_restaurant(dinner_anchor)
        if dinner:
            route.append((dinner, "restaurant"))
            used_restaurant_ids.add(dinner["id"])

    for item_dict, item_type in route:
        if item_type == "attraction":
            hop_minutes = travel_minutes(current_point, item_dict)
            projected_travel = total_travel_min + hop_minutes
            if hop_minutes > constraints.max_single_hop_minutes or projected_travel > constraints.max_travel_minutes:
                dropped.append({"id": item_dict["id"], "reason": "would_exceed_daily_travel_budget"})
                continue
        current_point = emit(item_dict, item_type, current_point)

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
