# tests/test_budget.py
# Pure unit tests - no I/O. cost_reference is a plain dict passed in
# (CostReferenceTable), matching the fixture-friendly design shared with
# app/core/scoring.py's TravelMatrix.

import pytest

from app.core.budget import (
    budget_split, budget_per_day, estimate_item_cost, feasibility, check_budget,
    DEFAULT_SPLIT, SPLIT_BY_STYLE,
)

COST_TABLE = {
    ("district-1", "hotel", 2): {"unit": "per_night", "typical_cost": 12000.0, "currency": "LKR"},
    (None, "hotel", 2): {"unit": "per_night", "typical_cost": 10000.0, "currency": "LKR"},
    (None, "restaurant", 1): {"unit": "per_meal", "typical_cost": 600.0, "currency": "LKR"},
}


# ---- budget_split / budget_per_day -----------------------------------------

def test_budget_split_default_when_no_style():
    assert budget_split(None) == DEFAULT_SPLIT


def test_budget_split_known_style():
    assert budget_split("luxury") == SPLIT_BY_STYLE["luxury"]


def test_budget_split_unknown_style_falls_back_to_default():
    assert budget_split("not_a_real_style") == DEFAULT_SPLIT


def test_budget_split_sums_to_one():
    for style in (None, "budget", "luxury"):
        assert sum(budget_split(style).values()) == pytest.approx(1.0)


def test_budget_per_day_none_budget_returns_all_none():
    result = budget_per_day(None, 3, None)
    assert all(v is None for v in result.values())


def test_budget_per_day_splits_correctly():
    result = budget_per_day(60000.0, 3, None)   # DEFAULT_SPLIT: stay=0.40
    assert result["hotel"] == pytest.approx(60000.0 * 0.40 / 3)


def test_budget_per_day_per_person_divides_by_travelers():
    solo = budget_per_day(60000.0, 3, None, travelers=1, per_person=True)
    group = budget_per_day(60000.0, 3, None, travelers=4, per_person=True)
    assert group["hotel"] == pytest.approx(solo["hotel"] / 4)


def test_budget_per_day_zero_duration_returns_none():
    result = budget_per_day(60000.0, 0, None)
    assert all(v is None for v in result.values())


# ---- estimate_item_cost ----------------------------------------------------

def test_estimate_item_cost_prefers_exact_hotel_price():
    item = {"price_per_night": 15000.0, "currency": "LKR", "price_level": 2}
    est = estimate_item_cost(item, "hotel", "district-1", COST_TABLE)
    assert est.value == 15000.0
    assert est.basis == "exact"


def test_estimate_item_cost_prefers_exact_event_price():
    item = {"price_min": 500.0, "currency": "LKR"}
    est = estimate_item_cost(item, "event", "district-1", COST_TABLE)
    assert est.value == 500.0
    assert est.basis == "exact"


def test_estimate_item_cost_district_reference():
    item = {"price_level": 2}
    est = estimate_item_cost(item, "hotel", "district-1", COST_TABLE)
    assert est.value == 12000.0
    assert est.basis == "reference"


def test_estimate_item_cost_national_fallback_when_no_district_match():
    item = {"price_level": 2}
    est = estimate_item_cost(item, "hotel", "district-unknown", COST_TABLE)
    assert est.value == 10000.0
    assert est.basis == "national"


def test_estimate_item_cost_unknown_when_nothing_matches():
    item = {"price_level": 4}
    est = estimate_item_cost(item, "attraction", "district-1", COST_TABLE)
    assert est.value is None
    assert est.basis == "unknown"


def test_estimate_item_cost_never_assumes_zero():
    # An item with genuinely no cost data must never silently become 0.0 -
    # that's the entire point of the "unknown" basis existing.
    est = estimate_item_cost({}, "hotel", None, {})
    assert est.value is None


def test_estimate_item_cost_requires_explicit_category_not_read_from_item():
    # Regression test for a real bug: a prior version read item.get("category"),
    # but no item dict this codebase produces (real db_tool rows or test
    # fixtures) carries that key - every cost lookup silently returned
    # "unknown" regardless of price_level, which made budget feasibility
    # checks blind (cheapest_total came out 0, so every budget looked
    # affordable). category is now a required, separate argument.
    item = {"category": "hotel", "price_level": 1}   # a stray "category" key on the item itself
    est = estimate_item_cost(item, "restaurant", "district-1", COST_TABLE)   # explicit category says restaurant
    # COST_TABLE has no district-1 restaurant entry, only a national one -
    # if the buggy version's item.get("category") ("hotel") were used
    # instead, this would incorrectly match the district-1 *hotel* row
    # (12000.0) rather than falling through to the national restaurant row.
    assert est.value == 600.0
    assert est.basis == "national"


# ---- feasibility ------------------------------------------------------------

def test_feasibility_none_budget_is_always_feasible():
    hotels = [{"id": "h1", "price_per_night": 100000.0, "currency": "LKR"}]
    result = feasibility(hotels, [], [], 3, None, None, {})
    assert result.feasible is True


def test_feasibility_uses_cheapest_hotel_and_restaurant():
    hotels = [
        {"id": "h1", "price_per_night": 20000.0, "currency": "LKR"},
        {"id": "h2", "price_per_night": 5000.0, "currency": "LKR"},   # cheapest
    ]
    restaurants = [{"id": "r1", "price_min": 1000.0, "currency": "LKR"}]
    # 2 nights: 5000*2 + 1000*2*2 (2 meals/day) = 10000 + 4000 = 14000
    result = feasibility(hotels, restaurants, [], 2, 20000.0, None, {})
    assert result.cheapest_total == pytest.approx(14000.0)
    assert result.feasible is True


def test_feasibility_infeasible_when_even_cheapest_exceeds_budget():
    hotels = [{"id": "h1", "price_per_night": 50000.0, "currency": "LKR"}]
    result = feasibility(hotels, [], [], 3, 10000.0, None, {})
    assert result.feasible is False
    assert result.shortfall > 0


def test_feasibility_tracks_unknown_cost_items():
    hotels = [{"id": "h1"}]   # no price data anywhere
    result = feasibility(hotels, [], [], 1, 10000.0, None, {})
    assert "h1" in result.unknown_cost_items


# ---- check_budget -----------------------------------------------------------

def test_check_budget_sums_across_days():
    days = [{"hotel": 5000.0, "restaurant": 2000.0}, {"hotel": 5000.0, "restaurant": 2000.0}]
    result = check_budget(days, 20000.0)
    assert result.total == 14000.0
    assert result.feasible is True


def test_check_budget_over_budget_reports_over_by():
    days = [{"hotel": 20000.0}]
    result = check_budget(days, 10000.0)
    assert result.feasible is False
    assert result.over_by == 10000.0


def test_check_budget_none_budget_always_feasible():
    days = [{"hotel": 1000000.0}]
    result = check_budget(days, None)
    assert result.feasible is True


def test_check_budget_per_category_breakdown():
    days = [{"hotel": 5000.0, "restaurant": 1000.0}, {"hotel": 5000.0, "restaurant": 1500.0}]
    result = check_budget(days, None)
    assert result.per_category == {"hotel": 10000.0, "restaurant": 2500.0}
