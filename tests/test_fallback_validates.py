# tests/test_fallback_validates.py
#
# The invariant this file exists to prove: build_plan_core's output always
# passes app/core/output_validator.py's own L1/L2 rules - the same rules
# the LLM planner's output is checked against (app/core/orchestrator.py's
# _verify_node). _verify_node currently SKIPS re-validating a fallback plan
# (see its own docstring), on the assumption that the deterministic path is
# "valid by construction" - this file is what makes that assumption true
# instead of just assumed.
#
# Found live (fallback investigation, 2026-09-25): it wasn't true. Every
# scenario below reproduces a real failure this test caught before the
# corresponding fix in app/core/itinerary.py:
#   - 1-day trips:          no_duplicates      (check-in and check-out both
#                            emitted the same hotel on the one day)
#   - packed pace:           day_ends_by_curfew (lunch's dwell/travel was
#                            never reserved in the day's feasibility sim)
#   - a distant restaurant:  no_absurd_hop,
#                            day_ends_by_curfew (the meal-picking code always
#                            forced the globally nearest restaurant, however
#                            far, instead of only a reachable one)
#
# This does NOT relax output_validator - see tests/test_output_validator.py
# for the (untouched) proof that it still rejects a bad plan. This file
# only constrains build_plan_core: given realistic inputs, its own output
# must clear the bar the LLM path is held to.
from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.core.fallback import PlanningContext, build_plan_core
from app.core.itinerary import DAY_END, DEFAULT_MAX_SINGLE_HOP_MINUTES
from app.core.output_validator import ValidationContext, validate
from app.models.schemas import PlannerOutput

KANDY = {"lat": 7.2906, "lon": 80.6337}
COLOMBO = {"lat": 6.9271, "lon": 79.8612}

HOTEL = {"id": "h1", "name": "Hotel", "lat": 7.2910, "lon": 80.6340,
         "price_level": 2, "rating": 4.5, "rating_count": 100, "tags": ["stay"], "currency": "LKR"}

OUTDOOR_TAGS = frozenset({"hike", "nature"})
COST_TABLE = {
    (None, "hotel", 1): {"unit": "per_night", "typical_cost": 4500.0, "currency": "LKR"},
    (None, "hotel", 2): {"unit": "per_night", "typical_cost": 12000.0, "currency": "LKR"},
    (None, "restaurant", 1): {"unit": "per_meal", "typical_cost": 600.0, "currency": "LKR"},
    (None, "attraction", 1): {"unit": "per_entry", "typical_cost": 0.0, "currency": "LKR"},
}


def _attractions(n: int, tag: str = "culture") -> list[dict]:
    # Spread over a real district-sized area (~0-16km from the anchor in
    # each direction), not clustered on a point - a validator pass that
    # only held for co-located fixture data wouldn't prove anything.
    return [
        {"id": f"a{i}", "name": f"Attraction {i}", "tags": [tag], "price_level": 1,
         "rating": 4.0 + (i % 5) * 0.1, "rating_count": 50, "currency": "LKR",
         "lat": KANDY["lat"] + (i % 5) * 0.04, "lon": KANDY["lon"] + (i // 5) * 0.04}
        for i in range(n)
    ]


def _restaurants(n: int, km_away: float = 1.0) -> list[dict]:
    deg = km_away / 111.0   # ~111km per degree latitude, close enough for a fixture
    return [
        {"id": f"r{i}", "name": f"Restaurant {i}", "tags": ["food"], "price_level": 2,
         "rating": 4.2, "rating_count": 30, "currency": "LKR",
         "lat": KANDY["lat"] + deg + i * 0.001, "lon": KANDY["lon"] + deg}
        for i in range(n)
    ]


def _plan_and_validate(
    duration_days: int, pace_items_per_day: int, start_location: dict,
    restaurants: list[dict], attractions: list[dict], per_day_rain: dict[str, float] | None = None,
) -> tuple[PlannerOutput, "validate.__annotations__"]:
    start_date = date(2026, 10, 1)
    ctx = PlanningContext(
        destination_name="Kandy", district_id=None, duration_days=duration_days,
        start_date=start_date, budget=200_000.0, travelers=2, travel_style="balanced",
        interests=["culture"], pace_items_per_day=pace_items_per_day,
        start_location=start_location, per_day_rain_probability=per_day_rain or {},
    )
    result = build_plan_core(
        ctx, [HOTEL], restaurants, attractions, [],
        OUTDOOR_TAGS, COST_TABLE,
    )
    plan = PlannerOutput(
        itinerary=result.itinerary, estimated_cost=result.estimated_cost,
        currency=result.currency, budget_notes=result.budget_notes,
    )
    candidate_ids = {
        item["listing_id"] for day in result.itinerary for item in day["items"] if item["listing_id"]
    }
    vctx = ValidationContext(
        duration_days=duration_days,
        valid_dates={(start_date + timedelta(d)).isoformat() for d in range(duration_days)},
        budget=ctx.budget, destination=KANDY, candidate_listing_ids=candidate_ids,
        day_end=DAY_END, max_single_hop_minutes=DEFAULT_MAX_SINGLE_HOP_MINUTES,
        expected_items_per_day=pace_items_per_day,
    )
    return plan, validate(plan, vctx)


# id, duration_days, pace, start_location, restaurant pool, attraction pool, rain
SCENARIOS = [
    ("1_day_relaxed", 1, 2, KANDY, _restaurants(3), _attractions(6), None),
    ("1_day_packed", 1, 5, KANDY, _restaurants(3), _attractions(10), None),
    ("2_day_balanced", 2, 3, KANDY, _restaurants(4), _attractions(12), None),
    ("3_day_balanced", 3, 3, KANDY, _restaurants(4), _attractions(15), None),
    ("3_day_packed", 3, 5, KANDY, _restaurants(4), _attractions(20), None),
    ("3_day_relaxed", 3, 2, KANDY, _restaurants(4), _attractions(10), None),
    ("5_day_packed", 5, 5, KANDY, _restaurants(6), _attractions(30), None),
    ("7_day_balanced_sparse_attractions", 7, 3, KANDY, _restaurants(6), _attractions(8), None),
    ("start_location_far_from_hotel", 3, 3, COLOMBO, _restaurants(4), _attractions(15), None),
    ("restaurants_5km_out", 3, 3, KANDY, _restaurants(4, km_away=5.0), _attractions(15), None),
    ("restaurants_15km_out", 3, 3, KANDY, _restaurants(4, km_away=15.0), _attractions(15), None),
    ("restaurants_35km_out", 3, 3, KANDY, _restaurants(4, km_away=35.0), _attractions(15), None),
    ("single_restaurant_reused_all_trip", 3, 3, KANDY, _restaurants(1), _attractions(15), None),
    ("no_restaurants_at_all", 3, 3, KANDY, [], _attractions(15), None),
    ("no_attractions_at_all", 3, 3, KANDY, _restaurants(4), [], None),
    ("rainy_every_day_packed", 3, 5, KANDY, _restaurants(4), _attractions(20), {
        "2026-10-01": 0.9, "2026-10-02": 0.9, "2026-10-03": 0.9,
    }),
    ("attractions_need_long_dwell", 3, 3, KANDY, _restaurants(4), _attractions(15, tag="hike"), None),
]


@pytest.mark.parametrize("case_id,duration_days,pace,start_loc,restaurants,attractions,rain", SCENARIOS,
                        ids=[c[0] for c in SCENARIOS])
def test_fallback_plan_passes_output_validator(case_id, duration_days, pace, start_loc, restaurants, attractions, rain):
    plan, result = _plan_and_validate(duration_days, pace, start_loc, restaurants, attractions, rain)
    assert result.ok, f"{case_id}: fallback plan failed its own validator: {result.failures}\n{plan.itinerary}"


def test_fallback_plan_passes_output_validator_across_every_pace_and_duration():
    # A denser sweep than the named scenarios above, on the standard (not
    # deliberately sparse/distant) candidate pool - the combination space
    # most real requests actually land in.
    failures_by_case = {}
    for duration_days in (1, 2, 3, 4, 5, 6, 7, 10, 14):
        for pace in (2, 3, 5):
            plan, result = _plan_and_validate(
                duration_days, pace, KANDY, _restaurants(5), _attractions(max(duration_days * pace + 5, 12)),
            )
            if not result.ok:
                failures_by_case[f"{duration_days}d_pace{pace}"] = result.failures
    assert not failures_by_case, failures_by_case
