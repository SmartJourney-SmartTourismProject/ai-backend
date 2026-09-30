from app.api.trip import _estimated_cost


def test_keeps_the_plans_own_total():
    result = {"estimated_cost": 500.0, "itinerary": [{"day": 1, "items": [{}], "day_cost": 100.0}]}
    assert _estimated_cost(result) == 500.0


def test_sums_day_costs_when_total_missing():
    # A weather/budget question turn carries the itinerary but not its total.
    result = {
        "estimated_cost": None,
        "itinerary": [
            {"day": 1, "items": [{}], "day_cost": 18445.0},
            {"day": 2, "items": [{}], "day_cost": 0.0},
        ],
    }
    assert _estimated_cost(result) == 18445.0


def test_no_plan_means_no_cost():
    assert _estimated_cost({"estimated_cost": None, "itinerary": []}) is None
    assert _estimated_cost({"itinerary": [{"day": 1, "items": [], "day_cost": 0.0}]}) is None
