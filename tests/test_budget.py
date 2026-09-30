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


def test_estimate_item_cost_approved_entry_fee_beats_assumed_free_band():
    # Sigiriya carries no price_level, so without its approved CCF fee the
    # assumed default band (free, 0011) would price it at 0.
    table = {**COST_TABLE,
             (None, "attraction", 1): {"unit": "per_entry", "typical_cost": 0.0, "currency": "LKR",
                                       "is_assumed_default": True}}
    item = {"entry_fee": 11690.0, "price_level": None, "currency": "USD"}
    est = estimate_item_cost(item, "attraction", "district-1", table)
    assert est.value == 11690.0
    assert est.basis == "exact"
    assert est.currency == "LKR"


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
    # A 2-day trip is 1 night: 5000*1 + 1000*2*2 (2 meals/day) = 5000 + 4000 = 9000
    result = feasibility(hotels, restaurants, [], 2, 20000.0, None, {})
    assert result.cheapest_total == pytest.approx(9000.0)
    assert result.feasible is True


def test_feasibility_hotel_charged_by_nights_not_per_day():
    # Regression test for a live bug (2026-09-06): a 3-day trip reported
    # "even the cheapest options come to 118,517 LKR" while the actual
    # delivered plan cost only 79,011 LKR - a "floor" higher than reality,
    # because this used to multiply hotel cost by duration_days while the
    # real planner (itinerary.py) only ever charges the WHOLE stay once, at
    # check-in (nightly_rate * hotel_nights), with check-out a free bookend.
    hotels = [{"id": "h1", "price_per_night": 20000.0, "currency": "LKR"}]
    restaurants = [{"id": "r1", "price_min": 1000.0, "currency": "LKR"}]
    # 3 days = 2 nights: 20000*2 + 1000*2*3 meals = 40000 + 6000
    result = feasibility(hotels, restaurants, [], 3, 100000.0, None, {})
    assert result.cheapest_total == pytest.approx(46000.0)


def test_feasibility_hotel_cost_scales_with_longer_stays():
    # A flat "charge twice regardless of length" (the first, overcorrected
    # fix for the 2026-09-06 bug above) coincidentally matched the 3-day case
    # right above (2 nights == "2 emits"), but silently UNDERSTATED the floor
    # for anything longer - live-found 2026-09-26, a 1->5 day follow-up left
    # this floor at 2 nights' worth of hotel cost while the actual delivered
    # plan billed 4. Nights must scale with duration_days.
    hotels = [{"id": "h1", "price_per_night": 20000.0, "currency": "LKR"}]
    restaurants = [{"id": "r1", "price_min": 1000.0, "currency": "LKR"}]
    # 5 days = 4 nights: 20000*4 + 1000*2*5 meals = 80000 + 10000
    result = feasibility(hotels, restaurants, [], 5, 200000.0, None, {})
    assert result.cheapest_total == pytest.approx(90000.0)


def test_feasibility_no_hotels_charges_nothing_for_stay():
    result = feasibility([], [{"id": "r1", "price_min": 1000.0, "currency": "LKR"}], [], 3, 100000.0, None, {})
    assert result.cheapest_total == pytest.approx(6000.0)


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


class TestAssumedCostsPerCategory:
    """A cost the catalogue does not know must not be invented.

    Live complaint, 2026-09-30: itineraries were charging an entry fee for
    places that are free to visit. The cause was a single assumed price band
    (the mid of 1-4) applied to every category, which cost_reference prices at
    1,500 LKR for an attraction - and 336 of 339 verified attractions carry no
    price_level at all. Beaches, viewpoints, the Galle Fort ramparts and most
    temples are free in Sri Lanka, so the tracker was reporting charges that do
    not exist.
    """

    # Mirrors the real cost_reference table, is_assumed_default included
    # (migration 0011): which band fills a gap is data now, not a rule in
    # code, so the fixture has to say it the same way the database does.
    TABLE = {
        (None, "attraction", 1): {"typical_cost": 0.0, "currency": "LKR", "is_assumed_default": True},
        (None, "attraction", 2): {"typical_cost": 1500.0, "currency": "LKR"},
        (None, "attraction", 3): {"typical_cost": 5000.0, "currency": "LKR"},
        (None, "restaurant", 2): {"typical_cost": 1800.0, "currency": "LKR", "is_assumed_default": True},
        (None, "hotel", 2): {"typical_cost": 12000.0, "currency": "LKR", "is_assumed_default": True},
    }

    def test_an_attraction_with_no_price_band_is_free(self):
        est = estimate_item_cost({"id": "a"}, "attraction", None, self.TABLE)
        assert est.value == 0.0
        assert est.basis == "assumed"

    def test_a_meal_with_no_price_band_is_not_assumed_free(self):
        # The mirror case: a restaurant is never free, so assuming zero would
        # understate every trip that eats.
        est = estimate_item_cost({"id": "r"}, "restaurant", None, self.TABLE)
        assert est.value == 1800.0

    def test_a_room_with_no_price_band_is_not_assumed_free(self):
        est = estimate_item_cost({"id": "h"}, "hotel", None, self.TABLE)
        assert est.value == 12000.0

    def test_a_stated_price_band_still_wins_over_the_assumption(self):
        # A ticketed attraction that DOES declare a band must keep its real
        # price - the assumption only fills a genuine gap.
        est = estimate_item_cost({"id": "a", "price_level": 3}, "attraction", None, self.TABLE)
        assert est.value == 5000.0
        # The point is that it is a looked-up price, not an assumed one; which
        # of the two lookup branches answered is not what this test guards.
        assert est.basis != "assumed"

    def test_an_exact_price_still_wins(self):
        est = estimate_item_cost(
            {"id": "h", "price_per_night": 26697.2}, "hotel", None, self.TABLE,
        )
        assert est.value == 26697.2
        assert est.basis == "exact"
