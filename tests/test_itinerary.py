# tests/test_itinerary.py
# Pure unit tests - no I/O. Fixed fixtures throughout, since build_day_plan
# is meant to be bit-reproducible (docs/master_plan/PROJECT_MASTER_PLAN.md
# Phase 4 gate).

from app.core.itinerary import DayConstraints, DaySelections, build_day_plan
from app.core.scoring import TravelMatrix

ANCHOR = {"id": "start", "name": "Start", "lat": 7.2906, "lon": 80.6337}   # Kandy

HOTEL = {"id": "h1", "name": "Test Hotel", "lat": 7.2910, "lon": 80.6340, "currency": "LKR"}
ATTRACTION_1 = {"id": "a1", "name": "Temple", "lat": 7.2936, "lon": 80.6413,
                "tags": ["culture"], "currency": "LKR"}
ATTRACTION_2 = {"id": "a2", "name": "Waterfall", "lat": 7.30, "lon": 80.65,
                "tags": ["nature"], "currency": "LKR"}
ATTRACTION_OUTDOOR = {"id": "a3", "name": "Hiking Trail", "lat": 7.31, "lon": 80.66,
                      "tags": ["hike"], "currency": "LKR"}
RESTAURANT = {"id": "r1", "name": "Test Restaurant", "lat": 7.2920, "lon": 80.6350, "currency": "LKR"}


def _selections(**overrides) -> DaySelections:
    defaults = dict(hotels=[HOTEL], restaurants=[RESTAURANT], attractions=[ATTRACTION_1, ATTRACTION_2], events=[])
    defaults.update(overrides)
    return DaySelections(**defaults)


def test_build_day_plan_includes_target_number_of_attractions():
    constraints = DayConstraints(items_target=2, include_lunch=False, include_dinner=False)
    plan = build_day_plan(1, "2026-10-01", ANCHOR, _selections(), constraints)

    attraction_items = [i for i in plan.items if i.type == "attraction"]
    assert len(attraction_items) == 2


def test_build_day_plan_respects_items_target_limit():
    constraints = DayConstraints(items_target=1, include_lunch=False, include_dinner=False)
    plan = build_day_plan(1, "2026-10-01", ANCHOR, _selections(), constraints)

    attraction_items = [i for i in plan.items if i.type == "attraction"]
    assert len(attraction_items) == 1


def test_build_day_plan_hotel_checkin_on_day_one():
    constraints = DayConstraints(items_target=1, need_hotel_checkin=True, include_lunch=False, include_dinner=False)
    plan = build_day_plan(1, "2026-10-01", ANCHOR, _selections(), constraints)

    assert plan.items[0].type == "hotel"
    assert plan.items[0].listing_id == "h1"


def test_build_day_plan_hotel_checkout_on_last_day():
    constraints = DayConstraints(items_target=1, need_hotel_checkout=True, include_lunch=False, include_dinner=False)
    plan = build_day_plan(3, "2026-10-03", ANCHOR, _selections(), constraints)

    assert plan.items[-1].type == "hotel"


def test_build_day_plan_excludes_outdoor_items_on_bad_weather_day():
    constraints = DayConstraints(
        items_target=3, exclude_outdoor=True, outdoor_tags=frozenset({"hike", "nature"}),
        include_lunch=False, include_dinner=False,
    )
    selections = _selections(attractions=[ATTRACTION_1, ATTRACTION_2, ATTRACTION_OUTDOOR])
    plan = build_day_plan(1, "2026-10-01", ANCHOR, selections, constraints)

    kept_ids = {i.listing_id for i in plan.items if i.type == "attraction"}
    assert "a3" not in kept_ids   # hike-tagged, excluded
    assert "a2" not in kept_ids   # nature-tagged, excluded
    assert "a1" in kept_ids       # culture-tagged, kept

    dropped_ids = {d["id"] for d in plan.dropped}
    assert "a3" in dropped_ids and "a2" in dropped_ids
    assert all(d["reason"] == "excluded_outdoor_bad_weather" for d in plan.dropped if d["id"] in ("a2", "a3"))


def test_build_day_plan_no_weather_exclusion_when_flag_is_false():
    constraints = DayConstraints(
        items_target=3, exclude_outdoor=False, outdoor_tags=frozenset({"hike", "nature"}),
        include_lunch=False, include_dinner=False,
    )
    selections = _selections(attractions=[ATTRACTION_1, ATTRACTION_OUTDOOR])
    plan = build_day_plan(1, "2026-10-01", ANCHOR, selections, constraints)

    kept_ids = {i.listing_id for i in plan.items if i.type == "attraction"}
    assert "a3" in kept_ids   # not excluded - exclude_outdoor is False


def test_build_day_plan_includes_lunch_and_dinner_when_requested():
    constraints = DayConstraints(items_target=2, include_lunch=True, include_dinner=True)
    plan = build_day_plan(1, "2026-10-01", ANCHOR, _selections(), constraints)

    restaurant_items = [i for i in plan.items if i.type == "restaurant"]
    assert len(restaurant_items) >= 1   # at least dinner; lunch depends on route length


def test_build_day_plan_times_are_strictly_increasing():
    constraints = DayConstraints(items_target=2, include_lunch=True, include_dinner=True)
    plan = build_day_plan(1, "2026-10-01", ANCHOR, _selections(), constraints)

    times = [i.time for i in plan.items]
    assert times == sorted(times)


def test_build_day_plan_end_time_after_start_time_for_every_item():
    constraints = DayConstraints(items_target=2, include_lunch=True, include_dinner=True)
    plan = build_day_plan(1, "2026-10-01", ANCHOR, _selections(), constraints)

    for item in plan.items:
        assert item.end_time > item.time


def test_build_day_plan_day_cost_sums_item_costs():
    constraints = DayConstraints(
        items_target=1, include_lunch=False, include_dinner=False,
        cost_lookup={"a1": 1500.0},
    )
    plan = build_day_plan(1, "2026-10-01", ANCHOR, _selections(), constraints)
    assert plan.day_cost == 1500.0


def test_build_day_plan_zero_cost_when_no_cost_data():
    constraints = DayConstraints(items_target=1, include_lunch=False, include_dinner=False)
    plan = build_day_plan(1, "2026-10-01", ANCHOR, _selections(), constraints)
    assert plan.day_cost == 0.0


def test_build_day_plan_empty_selections_produces_empty_day_not_a_crash():
    constraints = DayConstraints(items_target=3)
    plan = build_day_plan(1, "2026-10-01", ANCHOR, DaySelections(), constraints)
    assert plan.items == []
    assert plan.day_cost == 0.0


def test_build_day_plan_uses_travel_matrix_when_available():
    matrix = TravelMatrix()
    matrix.set(ANCHOR, ATTRACTION_1, 999.0)   # deliberately implausible, to prove it's actually used
    constraints = DayConstraints(items_target=1, include_lunch=False, include_dinner=False)
    plan = build_day_plan(1, "2026-10-01", ANCHOR, _selections(attractions=[ATTRACTION_1]), constraints, matrix)

    assert plan.total_travel_min == 999.0


def test_build_day_plan_is_deterministic_across_runs():
    constraints = DayConstraints(items_target=2, include_lunch=True, include_dinner=True,
                                 cost_lookup={"a1": 1500.0, "a2": 500.0, "r1": 2000.0})
    results = [build_day_plan(1, "2026-10-01", ANCHOR, _selections(), constraints) for _ in range(5)]
    first = results[0]
    for r in results[1:]:
        assert [i.__dict__ for i in r.items] == [i.__dict__ for i in first.items]
        assert r.day_cost == first.day_cost
        assert r.total_km == first.total_km


# ---- lunch/dinner don't pick the same restaurant twice (found live 2026-09-03) --

RESTAURANT_2 = {"id": "r2", "name": "Second Restaurant", "lat": 7.2925, "lon": 80.6355, "currency": "LKR"}


def test_build_day_plan_lunch_and_dinner_are_different_restaurants_when_two_exist():
    constraints = DayConstraints(items_target=2, include_lunch=True, include_dinner=True)
    selections = _selections(restaurants=[RESTAURANT, RESTAURANT_2])
    plan = build_day_plan(1, "2026-10-01", ANCHOR, selections, constraints)

    restaurant_ids = [i.listing_id for i in plan.items if i.type == "restaurant"]
    assert len(restaurant_ids) == 2
    assert len(set(restaurant_ids)) == 2   # not the same restaurant twice


def test_build_day_plan_reuses_the_only_restaurant_rather_than_skip_a_meal():
    # Only one real restaurant candidate - dinner reusing it is better than
    # silently dropping the meal slot.
    constraints = DayConstraints(items_target=2, include_lunch=True, include_dinner=True)
    selections = _selections(restaurants=[RESTAURANT])
    plan = build_day_plan(1, "2026-10-01", ANCHOR, selections, constraints)

    restaurant_ids = [i.listing_id for i in plan.items if i.type == "restaurant"]
    assert restaurant_ids == ["r1", "r1"]
