# tests/test_output_validator.py
# Pure unit tests - no I/O, no LLM, no mocking needed. Fixed fixtures, since
# this module is what makes an LLM's claimed output honest (project concern #7).

from app.core.output_validator import ValidationContext, day_scoped_repair_target, validate
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


def test_empty_day_items_fails():
    # Live-found regression: A1 dropped ItineraryDay.items' schema-level
    # min_length=1 (moved the constraint to L2, where business rules
    # belong per A1's own reasoning) - a real repair call was observed
    # producing items=[] with a nonzero day_cost carried over from the
    # previous attempt, and every other L1/L2 rule passed it vacuously.
    # ItineraryDay constructed directly (not via _day()) - that helper's
    # `items or [_item()]` default would silently replace an empty list.
    plan = _plan(days=[ItineraryDay(day=1, date="2026-10-01", items=[], day_cost=1000.0)])
    result = validate(plan, _ctx())
    assert result.ok is False
    assert any("days_have_items" in f for f in result.failures)


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


# ---- Part 4 (guardrails): route/feasibility checks on the LLM planner's own output --

def test_day_ends_by_curfew_catches_a_day_running_past_it():
    plan = _plan(days=[_day(items=[_item(time="20:00", end_time="22:00")])])
    result = validate(plan, _ctx(day_end="21:00"))
    assert any("day_ends_by_curfew" in f for f in result.failures)


def test_day_ends_by_curfew_passes_within_the_curfew():
    plan = _plan(days=[_day(items=[_item(time="19:00", end_time="20:30")])])
    result = validate(plan, _ctx(day_end="21:00"))
    assert not any("day_ends_by_curfew" in f for f in result.failures)


def test_day_ends_by_curfew_passes_when_absent_from_context():
    plan = _plan(days=[_day(items=[_item(time="20:00", end_time="23:59")])])
    result = validate(plan, _ctx())   # day_end not set
    assert not any("day_ends_by_curfew" in f for f in result.failures)


def test_no_absurd_hop_catches_a_far_consecutive_pair():
    # Kandy-ish to a point ~500km away - a hop no single-day cap should allow.
    plan = _plan(days=[_day(items=[
        _item(listing_id=VALID_UUID_1, time="09:00", end_time="10:00", lat=7.29, lon=80.63),
        _item(listing_id=VALID_UUID_2, time="10:15", end_time="11:00", lat=9.66, lon=80.02),
    ])])
    result = validate(plan, _ctx(max_single_hop_minutes=45.0))
    assert any("no_absurd_hop" in f for f in result.failures)


def test_no_absurd_hop_passes_for_nearby_consecutive_stops():
    plan = _plan(days=[_day(items=[
        _item(listing_id=VALID_UUID_1, time="09:00", end_time="10:00", lat=7.29, lon=80.63),
        _item(listing_id=VALID_UUID_2, time="10:15", end_time="11:00", lat=7.291, lon=80.631),
    ])])
    result = validate(plan, _ctx(max_single_hop_minutes=45.0))
    assert not any("no_absurd_hop" in f for f in result.failures)


def test_no_absurd_hop_passes_when_absent_from_context():
    plan = _plan(days=[_day(items=[
        _item(listing_id=VALID_UUID_1, time="09:00", end_time="10:00", lat=7.29, lon=80.63),
        _item(listing_id=VALID_UUID_2, time="10:15", end_time="11:00", lat=9.66, lon=80.02),
    ])])
    result = validate(plan, _ctx())   # max_single_hop_minutes not set
    assert not any("no_absurd_hop" in f for f in result.failures)


def test_items_per_day_respected_catches_too_many_attractions():
    plan = _plan(days=[_day(items=[
        _item(listing_id=VALID_UUID_1, time="09:00", end_time="10:00"),
        _item(listing_id=VALID_UUID_2, time="10:15", end_time="11:00"),
        _item(listing_id=VALID_UUID_3, time="11:15", end_time="12:00"),
    ])])
    result = validate(plan, _ctx(expected_items_per_day=2))
    assert any("items_per_day_respected" in f for f in result.failures)


def test_items_per_day_respected_allows_fewer_than_requested():
    # A day legitimately thinned by weather/price/feasibility drops is not
    # a violation - only exceeding the count is.
    plan = _plan(days=[_day(items=[_item()])])
    result = validate(plan, _ctx(expected_items_per_day=3))
    assert not any("items_per_day_respected" in f for f in result.failures)


def test_items_per_day_respected_passes_when_absent_from_context():
    plan = _plan(days=[_day(items=[
        _item(listing_id=VALID_UUID_1, time="09:00", end_time="10:00"),
        _item(listing_id=VALID_UUID_2, time="10:15", end_time="11:00"),
        _item(listing_id=VALID_UUID_3, time="11:15", end_time="12:00"),
    ])])
    result = validate(plan, _ctx())   # expected_items_per_day not set
    assert not any("items_per_day_respected" in f for f in result.failures)


# ---- multiple failures reported together --------------------------------------

def test_multiple_failures_all_reported_not_just_the_first():
    plan = _plan(
        days=[_day(day=1, date="2099-01-01", items=[_item(listing_id="99999999-9999-9999-9999-999999999999")])],
        estimated_cost=999999.0,
    )
    result = validate(plan, _ctx(budget=100.0))
    assert result.ok is False
    assert len(result.failures) >= 3   # L1 listing_id, dates_in_window, budget_honest at minimum


# ---- day_scoped_repair_target (app/core/orchestrator.py's Tier 4 fast path) --

def test_day_scoped_target_extracts_a_single_day_scoped_failure():
    failures = ["L2.no_duplicates: day 2 lists 'x' ('Hotel') more than once"]
    assert day_scoped_repair_target(failures) == {2}


def test_day_scoped_target_extracts_multiple_days_across_rules():
    failures = [
        "L2.no_absurd_hop: day 1: the hop from 'A' to 'B' is ~90 min, over the 45 min limit",
        "L2.day_ends_by_curfew: day 3 ends at 23:40, past the 21:00 curfew - drop or move the last item(s)",
    ]
    assert day_scoped_repair_target(failures) == {1, 3}


def test_day_scoped_target_parses_dates_in_windows_own_tuple_format():
    # dates_in_window's message shape is different from every other rule
    # (a list of (day, date) tuples, not a leading "day N") - real case
    # from this conversation's earlier fallback investigation.
    failures = ["L2.dates_in_window: day(s) [(2, '2026-09-27')] use a date outside the trip's real window (['2026-09-26'])"]
    assert day_scoped_repair_target(failures) == {2}


def test_day_scoped_target_bails_to_none_on_a_cross_day_failure():
    # day_count is about the WHOLE itinerary's day count, not any single
    # day's construction - no amount of rebuilding one day fixes it, so this
    # must bail rather than guess.
    failures = ["L2.day_count: plan has 2 day(s), the trip is 3 day(s)"]
    assert day_scoped_repair_target(failures) is None


def test_day_scoped_target_bails_when_any_single_failure_is_unrecognized():
    # Even one unrecognized/unlocalizable failure bails the WHOLE batch -
    # partially rebuilding only what's recognized would ship a plan that
    # still fails the check this function never looked at.
    failures = [
        "L2.no_duplicates: day 2 lists 'x' ('Hotel') more than once",
        "L2.budget_honest: estimated_cost (200000.0) is 50000.00 over the budget (150000.0) and budget_notes is empty - explain the gap",
    ]
    assert day_scoped_repair_target(failures) is None


def test_day_scoped_target_bails_on_an_l1_referential_failure():
    failures = ["L1.listing_id: 'x' on day 1 ('Hotel') was never returned by a db_search_* observation"]
    assert day_scoped_repair_target(failures) is None
