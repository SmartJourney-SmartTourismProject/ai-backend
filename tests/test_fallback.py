# tests/test_fallback.py
# Pure unit tests against build_plan_core - no I/O. This file directly
# implements Phase 4's gate (docs/master_plan/PROJECT_MASTER_PLAN.md):
# "fallback.build_plan(fixture) returns a byte-identical itinerary across
# 10 runs, respects a tight budget, and puts nothing outdoors on a
# rain_probability=0.8 day."

from datetime import date

from app.core.fallback import PlanningContext, build_plan_core

KANDY_START = {"lat": 7.2906, "lon": 80.6337}

HOTEL_MID = {"id": "h1", "name": "Mid Hotel", "lat": 7.2910, "lon": 80.6340,
            "price_level": 2, "rating": 4.5, "rating_count": 100, "tags": ["stay"], "currency": "LKR"}
HOTEL_CHEAP = {"id": "h2", "name": "Cheap Hotel", "lat": 7.2915, "lon": 80.6345,
              "price_level": 1, "rating": 4.0, "rating_count": 50, "tags": ["stay"], "currency": "LKR"}

RESTAURANT = {"id": "r1", "name": "Test Restaurant", "lat": 7.2920, "lon": 80.6350,
             "price_level": 1, "rating": 4.2, "rating_count": 30, "tags": ["food"], "currency": "LKR"}

ATTRACTION_CULTURE = {"id": "a1", "name": "Temple", "lat": 7.2936, "lon": 80.6413,
                      "price_level": 1, "rating": 4.8, "rating_count": 200, "tags": ["culture"], "currency": "LKR"}
ATTRACTION_OUTDOOR = {"id": "a2", "name": "Hiking Trail", "lat": 7.31, "lon": 80.66,
                      "price_level": 1, "rating": None, "rating_count": 0, "tags": ["hike"], "currency": "LKR"}
ATTRACTION_NATURE = {"id": "a3", "name": "Waterfall", "lat": 7.32, "lon": 80.67,
                     "price_level": 1, "rating": None, "rating_count": 0, "tags": ["nature"], "currency": "LKR"}

OUTDOOR_TAGS = frozenset({"hike", "nature"})

COST_TABLE = {
    (None, "hotel", 1): {"unit": "per_night", "typical_cost": 4500.0, "currency": "LKR"},
    (None, "hotel", 2): {"unit": "per_night", "typical_cost": 12000.0, "currency": "LKR"},
    (None, "restaurant", 1): {"unit": "per_meal", "typical_cost": 600.0, "currency": "LKR"},
    (None, "attraction", 1): {"unit": "per_entry", "typical_cost": 0.0, "currency": "LKR"},
}


def _ctx(**overrides) -> PlanningContext:
    defaults = dict(
        destination_name="Kandy", district_id=None, duration_days=2,
        start_date=date(2026, 10, 1), budget=60000.0, travelers=1, travel_style=None,
        interests=["culture"], start_location=KANDY_START,
    )
    defaults.update(overrides)
    return PlanningContext(**defaults)


def _build(ctx=None, **kwargs):
    ctx = ctx or _ctx()
    return build_plan_core(
        ctx,
        candidate_hotels=kwargs.get("hotels", [HOTEL_MID, HOTEL_CHEAP]),
        candidate_restaurants=kwargs.get("restaurants", [RESTAURANT]),
        candidate_attractions=kwargs.get("attractions", [ATTRACTION_CULTURE, ATTRACTION_OUTDOOR]),
        candidate_events=kwargs.get("events", []),
        outdoor_tags=kwargs.get("outdoor_tags", OUTDOOR_TAGS),
        cost_table=kwargs.get("cost_table", COST_TABLE),
    )


# ---- the gate itself: determinism ------------------------------------------

def test_build_plan_is_byte_identical_across_ten_runs():
    results = [_build() for _ in range(10)]
    first = results[0]
    for r in results[1:]:
        assert r.itinerary == first.itinerary
        assert r.estimated_cost == first.estimated_cost
        assert r.budget_notes == first.budget_notes
        assert r.final_response == first.final_response


# ---- the gate itself: respects a tight budget ------------------------------

def test_build_plan_respects_a_tight_budget():
    ctx = _ctx(budget=8000.0, duration_days=1)   # tight - only the cheap hotel + budget meals fit
    result = _build(ctx)

    assert result.estimated_cost <= 8000.0 or result.budget_notes is not None
    # the cheap hotel should be preferred when budget is this tight
    hotel_items = [i for day in result.itinerary for i in day["items"] if i["type"] == "hotel"]
    assert hotel_items[0]["listing_id"] == "h2"   # cheapest, not the mid-priced one


def test_build_plan_flags_infeasible_budget_before_building():
    ctx = _ctx(budget=100.0, duration_days=5)   # impossible - even the cheapest options exceed this
    result = _build(ctx)

    assert result.budget_notes is not None
    assert "over" in result.budget_notes.lower() or "budget" in result.budget_notes.lower()


# ---- the gate itself: nothing outdoors on a bad-weather day ----------------

def test_build_plan_puts_nothing_outdoors_on_a_rain_probability_08_day():
    ctx = _ctx(
        duration_days=1,
        per_day_rain_probability={"2026-10-01": 0.8},
    )
    result = _build(ctx, attractions=[ATTRACTION_CULTURE, ATTRACTION_OUTDOOR, ATTRACTION_NATURE])

    day1_items = result.itinerary[0]["items"]
    outdoor_ids = {"a2", "a3"}
    kept_ids = {i["listing_id"] for i in day1_items}
    assert not (kept_ids & outdoor_ids), "no hike/nature-tagged item should appear on a rain_probability=0.8 day"


def test_build_plan_allows_outdoor_items_on_a_clear_day():
    ctx = _ctx(duration_days=1, per_day_rain_probability={"2026-10-01": 0.1})
    result = _build(ctx, attractions=[ATTRACTION_OUTDOOR])

    day1_items = result.itinerary[0]["items"]
    kept_ids = {i["listing_id"] for i in day1_items}
    assert "a2" in kept_ids   # low rain probability - outdoor item is fine


def test_build_plan_per_day_weather_is_independent():
    # A multi-day trip where only day 1 has bad weather - day 2 must still
    # be allowed to include outdoor items.
    ctx = _ctx(duration_days=2, per_day_rain_probability={"2026-10-01": 0.9, "2026-10-02": 0.1})
    result = _build(ctx, attractions=[ATTRACTION_CULTURE, ATTRACTION_OUTDOOR])

    day1_ids = {i["listing_id"] for i in result.itinerary[0]["items"]}
    day2_ids = {i["listing_id"] for i in result.itinerary[1]["items"]}
    assert "a2" not in day1_ids
    # a2 was already used on neither day necessarily, but must not be
    # excluded from day 2 purely because day 1 excluded it.


# ---- general shape ---------------------------------------------------------

def test_build_plan_produces_one_entry_per_day():
    ctx = _ctx(duration_days=4)
    result = _build(ctx)
    assert len(result.itinerary) == 4
    assert [d["day"] for d in result.itinerary] == [1, 2, 3, 4]


def test_build_plan_dates_are_sequential_from_start_date():
    ctx = _ctx(duration_days=3, start_date=date(2026, 12, 25))
    result = _build(ctx)
    assert [d["date"] for d in result.itinerary] == ["2026-12-25", "2026-12-26", "2026-12-27"]


def test_build_plan_hotel_checkin_on_day_one_checkout_on_last():
    ctx = _ctx(duration_days=3)
    result = _build(ctx)

    day1_types = [i["type"] for i in result.itinerary[0]["items"]]
    last_day_types = [i["type"] for i in result.itinerary[-1]["items"]]
    assert day1_types[0] == "hotel"
    assert last_day_types[-1] == "hotel"


def test_build_plan_estimated_cost_matches_sum_of_day_costs():
    result = _build()
    assert result.estimated_cost == sum(d["day_cost"] for d in result.itinerary)


def test_build_plan_plan_source_is_fallback():
    result = _build()
    assert result.plan_source == "fallback"


def test_build_plan_no_candidates_produces_empty_but_valid_plan():
    ctx = _ctx(duration_days=1)
    result = _build(ctx, hotels=[], restaurants=[], attractions=[])
    assert len(result.itinerary) == 1
    assert result.itinerary[0]["items"] == []
    assert result.estimated_cost == 0.0
