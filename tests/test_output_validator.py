# tests/test_output_validator.py
# Pure unit tests - no I/O, no LLM, no mocking needed. Fixed fixtures, since
# this module is what makes an LLM's claimed output honest (project concern #7).

from app.core.output_validator import ValidationContext, validate
from app.models.schemas import PlannerOutput, ItineraryDay, ItineraryItem

KANDY = {"lat": 7.2906, "lon": 80.6337}

VALID_UUID_1 = "11111111-1111-1111-1111-111111111111"
VALID_UUID_2 = "22222222-2222-2222-2222-222222222222"
VALID_UUID_3 = "33333333-3333-3333-3333-333333333333"


def _item(listing_id=VALID_UUID_1, time="09:00", end_time="10:30", est_cost=1000.0, **overrides) -> ItineraryItem:
    defaults = dict(
        time=time, end_time=end_time, type="attraction", listing_id=listing_id,
        name="Test Place", lat=7.29, lon=80.63, est_cost=est_cost, currency="LKR",
    )
    defaults.update(overrides)
    return ItineraryItem(**defaults)


def _day(day=1, date="2026-10-01", items=None, day_cost=1000.0) -> ItineraryDay:
    return ItineraryDay(day=day, date=date, items=items or [_item()], day_cost=day_cost)


def _plan(days=None, estimated_cost=1000.0, budget_notes=None) -> PlannerOutput:
    return PlannerOutput(itinerary=days or [_day()], estimated_cost=estimated_cost, budget_notes=budget_notes)


def _ctx(**overrides) -> ValidationContext:
    defaults = dict(
        duration_days=1, valid_dates={"2026-10-01"}, budget=None, destination=KANDY,
        candidate_listing_ids={VALID_UUID_1, VALID_UUID_2, VALID_UUID_3},
    )
    defaults.update(overrides)
    return ValidationContext(**defaults)


# ---- the happy path ---------------------------------------------------------

def test_a_correct_plan_passes_every_rule():
    result = validate(_plan(), _ctx())
    assert result.ok is True
    assert result.failures == []


# ---- L1: referential --------------------------------------------------------

def test_l1_rejects_a_listing_id_never_seen_this_request():
    plan = _plan(days=[_day(items=[_item(listing_id="99999999-9999-9999-9999-999999999999")])])
    result = validate(plan, _ctx())
    assert result.ok is False
    assert any("L1.listing_id" in f for f in result.failures)


def test_l1_travel_item_with_no_listing_id_is_fine():
    plan = _plan(days=[_day(items=[_item(listing_id=None, type="travel")])])
    result = validate(plan, _ctx())
    assert not any("L1.listing_id" in f for f in result.failures)


def test_l1_non_travel_item_with_no_listing_id_fails():
    plan = _plan(days=[_day(items=[_item(listing_id=None, type="attraction")])])
    result = validate(plan, _ctx())
    assert any("L1.listing_id" in f for f in result.failures)


# ---- L2: day_count / dates / sequencing -------------------------------------

def test_day_count_mismatch_fails():
    plan = _plan(days=[_day(day=1), _day(day=2, date="2026-10-02")])
    result = validate(plan, _ctx(duration_days=1))
    assert any("day_count" in f for f in result.failures)


def test_dates_outside_window_fails():
    plan = _plan(days=[_day(date="2099-01-01")])
    result = validate(plan, _ctx())
    assert any("dates_in_window" in f for f in result.failures)


def test_days_not_sequential_fails():
    plan = _plan(days=[_day(day=1), _day(day=3, date="2026-10-02")])
    result = validate(plan, _ctx(duration_days=2, valid_dates={"2026-10-01", "2026-10-02"}))
    assert any("days_sequential" in f for f in result.failures)


# ---- L2: duplicates / times -------------------------------------------------

def test_duplicate_listing_in_same_day_fails():
    plan = _plan(days=[_day(items=[_item(time="09:00"), _item(time="11:00")])])   # same listing_id twice
    result = validate(plan, _ctx())
    assert any("no_duplicates" in f for f in result.failures)


def test_same_listing_on_different_days_is_fine():
    plan = _plan(
        days=[_day(day=1, items=[_item()]), _day(day=2, date="2026-10-02", items=[_item()])],
        estimated_cost=2000.0,
    )
    result = validate(plan, _ctx(duration_days=2, valid_dates={"2026-10-01", "2026-10-02"}))
    assert not any("no_duplicates" in f for f in result.failures)


def test_times_out_of_order_fails():
    plan = _plan(days=[_day(items=[
        _item(listing_id=VALID_UUID_1, time="14:00", end_time="15:00"),
        _item(listing_id=VALID_UUID_2, time="09:00", end_time="10:00"),
    ], day_cost=2000.0)], estimated_cost=2000.0)
    result = validate(plan, _ctx())
    assert any("times_ordered" in f for f in result.failures)


def test_end_time_before_start_time_fails():
    plan = _plan(days=[_day(items=[_item(time="10:00", end_time="09:00")])])
    result = validate(plan, _ctx())
    assert any("times_ordered" in f for f in result.failures)


# ---- L2: cost checks (the §12 case-1 fix) -----------------------------------

def test_cost_consistent_catches_mismatched_total():
    plan = _plan(days=[_day(day_cost=1000.0)], estimated_cost=5000.0)   # doesn't match day_cost sum
    result = validate(plan, _ctx())
    assert any("cost_consistent" in f for f in result.failures)


def test_cost_recomputes_catches_a_lowballed_claim():
    # The direct regression test for BUILD_PLAN §12 case 1: the plan claims
    # a cost far below what the real (tool-derived) cost lookup says.
    plan = _plan(estimated_cost=1000.0)
    ctx = _ctx(cost_lookup={VALID_UUID_1: 50000.0})   # real cost is much higher
    result = validate(plan, ctx)
    assert any("cost_recomputes" in f for f in result.failures)


def test_cost_recomputes_passes_when_costs_actually_match():
    plan = _plan(estimated_cost=1000.0)
    ctx = _ctx(cost_lookup={VALID_UUID_1: 1000.0})
    result = validate(plan, ctx)
    assert not any("cost_recomputes" in f for f in result.failures)


def test_cost_recomputes_skipped_when_no_cost_lookup_given():
    # Not this rule's job to fail when the caller didn't supply real costs
    # to check against - that's a caller error, not a plan error.
    plan = _plan(estimated_cost=999999.0)
    result = validate(plan, _ctx(cost_lookup={}))
    assert not any("cost_recomputes" in f for f in result.failures)


def test_budget_honest_flags_silent_overrun():
    plan = _plan(estimated_cost=50000.0, budget_notes=None)
    result = validate(plan, _ctx(budget=10000.0))
    assert any("budget_honest" in f for f in result.failures)


def test_budget_honest_allows_overrun_when_explained():
    plan = _plan(estimated_cost=50000.0, budget_notes="This exceeds the budget because...")
    result = validate(plan, _ctx(budget=10000.0))
    assert not any("budget_honest" in f for f in result.failures)


def test_budget_honest_passes_when_within_budget():
    plan = _plan(estimated_cost=5000.0)
    result = validate(plan, _ctx(budget=10000.0))
    assert not any("budget_honest" in f for f in result.failures)


# ---- L2: geography -----------------------------------------------------------

def test_geo_near_dest_catches_a_point_far_from_the_destination():
    plan = _plan(days=[_day(items=[_item(lat=9.6615, lon=80.0255)])])   # Jaffna, far from Kandy
    result = validate(plan, _ctx(destination=KANDY))
    assert any("geo_near_dest" in f for f in result.failures)


def test_geo_near_dest_passes_for_a_nearby_point():
    plan = _plan(days=[_day(items=[_item(lat=7.30, lon=80.64)])])   # right next to Kandy
    result = validate(plan, _ctx(destination=KANDY))
    assert not any("geo_near_dest" in f for f in result.failures)


# ---- L2: weather --------------------------------------------------------------

def test_weather_respect_catches_outdoor_item_on_a_rainy_day():
    plan = _plan(days=[_day(date="2026-10-01", items=[_item(listing_id=VALID_UUID_1)])])
    ctx = _ctx(per_day_rain_probability={"2026-10-01": 0.8}, outdoor_listing_ids={VALID_UUID_1})
    result = validate(plan, ctx)
    assert any("weather_respect" in f for f in result.failures)


def test_weather_respect_allows_outdoor_item_on_a_clear_day():
    plan = _plan(days=[_day(date="2026-10-01", items=[_item(listing_id=VALID_UUID_1)])])
    ctx = _ctx(per_day_rain_probability={"2026-10-01": 0.1}, outdoor_listing_ids={VALID_UUID_1})
    result = validate(plan, ctx)
    assert not any("weather_respect" in f for f in result.failures)


def test_weather_respect_allows_indoor_item_on_a_rainy_day():
    plan = _plan(days=[_day(date="2026-10-01", items=[_item(listing_id=VALID_UUID_2)])])
    ctx = _ctx(per_day_rain_probability={"2026-10-01": 0.9}, outdoor_listing_ids={VALID_UUID_1})
    result = validate(plan, ctx)
    assert not any("weather_respect" in f for f in result.failures)


# ---- L2: disaster / must_avoid / currency -------------------------------------

def test_disaster_avoid_catches_an_item_in_a_red_zone():
    plan = _plan(days=[_day(items=[_item(lat=7.29, lon=80.63)])])
    ctx = _ctx(disaster_red_zones=[{"lat": 7.29, "lon": 80.63}])
    result = validate(plan, ctx)
    assert any("disaster_avoid" in f for f in result.failures)


def test_disaster_avoid_passes_when_no_red_zones():
    plan = _plan()
    result = validate(plan, _ctx(disaster_red_zones=[]))
    assert not any("disaster_avoid" in f for f in result.failures)


def test_must_avoid_catches_a_forbidden_listing():
    plan = _plan(days=[_day(items=[_item(listing_id=VALID_UUID_1)])])
    ctx = _ctx(must_avoid_listing_ids={VALID_UUID_1})
    result = validate(plan, ctx)
    assert any("must_avoid" in f for f in result.failures)


def test_currency_check_passes_for_lkr():
    result = validate(_plan(), _ctx())
    assert not any("currency" in f for f in result.failures)


# ---- multiple failures reported together --------------------------------------

def test_multiple_failures_all_reported_not_just_the_first():
    plan = _plan(
        days=[_day(day=1, date="2099-01-01", items=[_item(listing_id="99999999-9999-9999-9999-999999999999")])],
        estimated_cost=999999.0,
    )
    result = validate(plan, _ctx(budget=100.0))
    assert result.ok is False
    assert len(result.failures) >= 3   # L1 listing_id, dates_in_window, budget_honest at minimum
