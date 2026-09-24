# tests/test_itinerary.py
# Pure unit tests - no I/O. Fixed fixtures throughout, since build_day_plan
# is meant to be bit-reproducible (docs/master_plan/PROJECT_MASTER_PLAN.md
# Phase 4 gate).

import pytest

from app.core.itinerary import DAY_END, DayConstraints, DaySelections, _two_opt, build_day_plan
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


def test_build_day_plan_drops_attractions_that_would_exceed_the_travel_budget():
    # Regression (live-found 2026-09-06): a real Kandy itinerary chained
    # central-town attractions together with ones a genuine hour-plus drive
    # away (e.g. Rangala), because attractions were picked purely by rank
    # and count - total_travel_min was computed but never used to stop
    # packing more stops into the day. The a1<->a2 hop is set to 200 minutes
    # in BOTH directions (itinerary-quality/token-reduction pass: ordering
    # now minimizes travel MINUTES via nearest-neighbour+2-opt, not raw
    # distance - a one-directional matrix entry could be dodged entirely by
    # visiting the cheaper-to-reach attraction first and never taking the
    # expensive edge at all, which isn't what this test means to exercise).
    # ANCHOR->a2 is left on the haversine fallback (cheap, real coords are
    # close), so a2 is visited first; the 200-minute edge to a1 from THERE
    # is what should still be dropped.
    matrix = TravelMatrix()
    matrix.set(ANCHOR, ATTRACTION_1, 20.0)
    matrix.set(ATTRACTION_1, ATTRACTION_2, 200.0)
    matrix.set(ATTRACTION_2, ATTRACTION_1, 200.0)
    constraints = DayConstraints(items_target=2, include_lunch=False, include_dinner=False)
    plan = build_day_plan(1, "2026-10-01", ANCHOR, _selections(), constraints, matrix)

    attraction_ids = [i.listing_id for i in plan.items if i.type == "attraction"]
    assert attraction_ids == ["a2"]
    assert plan.dropped == [{"id": "a1", "reason": "would_exceed_daily_travel_budget"}]


def test_build_day_plan_drops_a_single_far_hop_early_in_the_day():
    # Regression (live-found 2026-09-06, the actual production case): a
    # cumulative-only cap doesn't catch a lone far attraction placed EARLY
    # in the day, since nothing has accumulated yet by the time it's
    # considered - Rangala Natural Pool (a real ~63-minute hop) was exactly
    # the *second* stop, well under any cumulative budget at that point.
    # This is what max_single_hop_minutes (45.0 default) exists for.
    #
    # a1's expensive edge is set in BOTH directions (itinerary-quality/
    # token-reduction pass, same reasoning as the sibling test above) -
    # otherwise 2-opt-aware ordering could reach a1 cheaply from a2 instead
    # of from ANCHOR, which would defeat the "genuinely unreachable" case
    # this test means to exercise. ANCHOR->a2 stays on the haversine
    # fallback (cheap), so a2 is visited first either way.
    matrix = TravelMatrix()
    matrix.set(ANCHOR, ATTRACTION_1, 63.0)   # the lone far hop, right at the start
    matrix.set(ATTRACTION_1, ATTRACTION_2, 5.0)
    matrix.set(ATTRACTION_2, ATTRACTION_1, 63.0)
    constraints = DayConstraints(items_target=2, include_lunch=False, include_dinner=False)
    plan = build_day_plan(1, "2026-10-01", ANCHOR, _selections(), constraints, matrix)

    attraction_ids = [i.listing_id for i in plan.items if i.type == "attraction"]
    assert attraction_ids == ["a2"]
    assert plan.dropped == [{"id": "a1", "reason": "would_exceed_daily_travel_budget"}]


def test_build_day_plan_travel_budget_cap_never_drops_meals():
    # A meal slot is worse to lose than a slightly fuller day - the cap
    # only ever applies to attractions.
    matrix = TravelMatrix()
    matrix.set(ANCHOR, ATTRACTION_1, 20.0)
    matrix.set(ATTRACTION_1, RESTAURANT, 200.0)
    constraints = DayConstraints(items_target=1, include_lunch=False, include_dinner=True)
    plan = build_day_plan(1, "2026-10-01", ANCHOR, _selections(), constraints, matrix)

    restaurant_items = [i for i in plan.items if i.type == "restaurant"]
    assert len(restaurant_items) == 1


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
    # 45.0 is deliberately implausible for these fixture coordinates (which
    # are close enough that haversine would give a much smaller number), to
    # prove the matrix value is actually used - but still comfortably under
    # DayConstraints' max_travel_minutes cap (180.0), so this stays a test
    # of "is the matrix read" and doesn't also trip the travel-budget cap
    # added 2026-09-06 (see test_build_day_plan_drops_attractions_that_would_
    # exceed_the_travel_budget for that behavior specifically).
    matrix.set(ANCHOR, ATTRACTION_1, 45.0)
    constraints = DayConstraints(items_target=1, include_lunch=False, include_dinner=False)
    plan = build_day_plan(1, "2026-10-01", ANCHOR, _selections(attractions=[ATTRACTION_1]), constraints, matrix)

    assert plan.total_travel_min == 45.0


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


# ---- 2-opt uncrosses a known bad route (itinerary-quality/token-reduction pass) --

def _euclidean(a: dict, b: dict) -> float:
    return ((a["lat"] - b["lat"]) ** 2 + (a["lon"] - b["lon"]) ** 2) ** 0.5


def _route_cost(anchor: dict, route: list[dict], cost) -> float:
    total = 0.0
    current = anchor
    for point in route:
        total += cost(current, point)
        current = point
    return total


def test_two_opt_uncrosses_a_known_bad_route():
    # A simple rectangle of 4 points (using lat/lon as plain (x, y)
    # coordinates) - the perimeter order anchor->p1->p2->p3->p4 costs 4.0
    # exactly; a deliberately crossing order (visiting corners out of
    # sequence) costs noticeably more. 2-opt, given the bad order, must
    # reduce total cost toward the optimum - this is the direct fix for
    # Defect 1 (the largest hop in a real itinerary sitting between two
    # consecutive stops, with the road between them passing later stops).
    anchor = {"lat": 0.0, "lon": 0.0}
    p1 = {"lat": 0.0, "lon": 1.0}
    p2 = {"lat": 1.0, "lon": 1.0}
    p3 = {"lat": 1.0, "lon": 0.0}
    p4 = {"lat": 2.0, "lon": 0.0}

    bad_order = [p3, p1, p4, p2]
    bad_cost = _route_cost(anchor, bad_order, _euclidean)

    improved = _two_opt(anchor, bad_order, _euclidean)
    improved_cost = _route_cost(anchor, improved, _euclidean)

    assert improved_cost < bad_cost
    assert improved_cost == pytest.approx(4.0, abs=1e-6)   # the perimeter order is optimal here
    assert {id(p) for p in improved} == {id(p) for p in bad_order}   # same 4 points, just reordered


def test_two_opt_leaves_three_or_fewer_points_unchanged():
    anchor = {"lat": 0.0, "lon": 0.0}
    points = [{"lat": 3.0, "lon": 0.0}, {"lat": 1.0, "lon": 0.0}, {"lat": 2.0, "lon": 0.0}]
    assert _two_opt(anchor, points, _euclidean) == points


# ---- DAY_END: a day never runs past its curfew (itinerary-quality/token-reduction pass) --

def test_day_never_runs_past_day_end():
    # Five attractions with a long tag-based dwell each (180 min - "hike",
    # see TAG_DWELL_MINUTES) plus dinner: at DAY_START=09:00 this cannot
    # possibly fit all five before DAY_END=21:00, so some must be dropped
    # with reason "day_would_run_past_end" rather than the day just running
    # past midnight (the actual production bug this fixes).
    hiking_spots = [
        {"id": f"h{i}", "name": f"Hike {i}", "lat": 7.29 + i * 0.001, "lon": 80.63 + i * 0.001,
         "tags": ["hike"], "currency": "LKR"}
        for i in range(5)
    ]
    constraints = DayConstraints(items_target=5, include_lunch=False, include_dinner=True)
    selections = _selections(attractions=hiking_spots)
    plan = build_day_plan(1, "2026-10-01", ANCHOR, selections, constraints)

    for item in plan.items:
        assert item.end_time <= DAY_END
    day_end_drops = [d for d in plan.dropped if d["reason"] == "day_would_run_past_end"]
    assert day_end_drops   # at least one hike had to be dropped to fit the day
