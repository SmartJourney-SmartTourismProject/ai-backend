# tests/test_planner_shared.py
# assemble_planner_days() - the fallback investigation's core fix
# (2026-09-25): the planner/repair finalize step was found live to
# transcribe its own build_day_plan tool output incorrectly (duplicate
# items, whole-trip cost repeated per day, curfew violations). This
# function rebuilds the final itinerary straight from the tool observations
# instead of trusting that transcription - these tests cover the real
# failure shapes that were observed, plus its degrade-gracefully paths.

from app.core.planner_shared import assemble_planner_days, enforce_budget_notes, fill_missing_days
from app.core.react import TraceStep, ToolCallTrace
from app.core.state import TripState
from app.models.schemas import ItineraryDay, ItineraryItem

_ATTR_ID = "22222222-2222-2222-2222-222222222222"
_HOTEL_ID = "11111111-1111-1111-1111-111111111111"


def _build_day_plan_call(day: int, date: str, day_cost: float) -> ToolCallTrace:
    return ToolCallTrace(
        tool="build_day_plan",
        args={"day": day, "date": date},
        observation={
            "items": [
                {"time": "09:00", "end_time": "10:00", "type": "attraction", "listing_id": _ATTR_ID,
                 "name": "Temple", "lat": 7.30, "lon": 80.64, "est_cost": 500.0,
                 "currency": "LKR", "notes": ""},
            ],
            "day_cost": day_cost, "total_km": 1.0, "total_travel_min": 5, "dropped": [],
        },
    )


def _wrong_llm_day(day: int, date: str) -> ItineraryDay:
    """Mimics the real failure: same hotel twice, whole-trip cost repeated."""
    item = ItineraryItem(
        time="09:00", end_time="10:00", type="hotel", listing_id=_HOTEL_ID,
        name="Hotel", lat=7.29, lon=80.63, est_cost=39505.69, currency="LKR",
    )
    return ItineraryDay(day=day, date=date, items=[item, item], day_cost=39505.69)


def test_prefers_tool_observation_over_llm_transcription():
    trace = [TraceStep(step=1, tool_calls=[_build_day_plan_call(1, "2026-10-01", 500.0)])]
    llm_days = [_wrong_llm_day(1, "2026-10-01")]

    assembled, warnings, unbacked = assemble_planner_days(trace, llm_days)

    assert warnings == []
    assert unbacked == []
    assert len(assembled) == 1
    assert len(assembled[0].items) == 1   # not the duplicated hotel
    assert assembled[0].items[0].listing_id == _ATTR_ID
    assert assembled[0].day_cost == 500.0   # not the whole-trip total


def test_last_call_per_day_wins():
    trace = [TraceStep(step=1, tool_calls=[
        _build_day_plan_call(1, "2026-10-01", 400.0),
        _build_day_plan_call(1, "2026-10-01", 600.0),   # a re-plan of day 1 in the same turn
    ])]
    assembled, warnings, unbacked = assemble_planner_days(trace, [_wrong_llm_day(1, "2026-10-01")])

    assert warnings == []
    assert unbacked == []
    assert assembled[0].day_cost == 600.0


def test_falls_back_to_llm_day_when_no_observation():
    """The recommend/planner step-budget failure mode: the model ran out of
    turns before reaching build_day_plan for every day."""
    trace = [TraceStep(step=1, tool_calls=[_build_day_plan_call(1, "2026-10-01", 500.0)])]
    llm_days = [_wrong_llm_day(1, "2026-10-01"), _wrong_llm_day(2, "2026-10-02")]

    assembled, warnings, unbacked = assemble_planner_days(trace, llm_days)

    assert len(assembled) == 2
    assert assembled[0].day_cost == 500.0   # day 1: real tool data
    assert assembled[1].day_cost == 39505.69   # day 2: no observation, falls back to the model's day
    assert len(warnings) == 1
    assert "day 2" in warnings[0]
    assert unbacked == [2]   # day 2 flagged for fill_missing_days to rebuild deterministically


def test_errored_tool_call_is_ignored():
    trace = [TraceStep(step=1, tool_calls=[
        ToolCallTrace(tool="build_day_plan", args={"day": 1, "date": "2026-10-01"},
                      observation={"error": "boom"}, error="boom"),
    ])]
    llm_days = [_wrong_llm_day(1, "2026-10-01")]

    assembled, warnings, unbacked = assemble_planner_days(trace, llm_days)

    assert len(assembled) == 1
    assert assembled[0].day_cost == 39505.69   # errored call ignored, falls back to the model's day
    assert len(warnings) == 1
    assert unbacked == [1]


def test_no_observation_and_no_llm_day_is_dropped_with_a_warning():
    assembled, warnings, unbacked = assemble_planner_days([], [])
    assert assembled == []
    assert warnings == []   # nothing to assemble, nothing to warn about either
    assert unbacked == []


def test_prefers_the_tool_observations_own_server_corrected_date():
    # Part 5 (server-side enforcement): app/tools/registry.py's
    # build_day_plan tool now returns its own server-derived "date" in the
    # observation - this must win over both the model's structured-output
    # date AND the raw call args, since either of those can be wrong in
    # exactly the way the observation's date cannot.
    trace = [TraceStep(step=1, tool_calls=[ToolCallTrace(
        tool="build_day_plan", args={"day": 1, "date": "2099-01-01"},   # wrong
        observation={
            "items": [{"time": "09:00", "end_time": "10:00", "type": "attraction", "listing_id": _ATTR_ID,
                       "name": "Temple", "lat": 7.30, "lon": 80.64, "est_cost": 500.0,
                       "currency": "LKR", "notes": ""}],
            "day_cost": 500.0, "total_km": 1.0, "total_travel_min": 5, "dropped": [],
            "day": 1, "date": "2026-10-01",   # the real, server-corrected date
        },
    )])]
    llm_days = [_wrong_llm_day(1, "2099-12-31")]   # also wrong

    assembled, warnings, unbacked = assemble_planner_days(trace, llm_days)

    assert warnings == []
    assert unbacked == []
    assert assembled[0].date == "2026-10-01"


# ---- enforce_budget_notes (Part 5, server-side enforcement) ----------------

def test_enforce_budget_notes_fills_a_note_when_over_budget_and_the_model_left_it_empty():
    note = enforce_budget_notes(None, estimated_cost=120_000.0, budget=100_000.0)
    assert note is not None
    assert "120,000" in note and "100,000" in note and "20,000" in note


def test_enforce_budget_notes_leaves_the_models_own_note_alone():
    note = enforce_budget_notes("Already over, swapped to a cheaper hotel.", estimated_cost=120_000.0, budget=100_000.0)
    assert note == "Already over, swapped to a cheaper hotel."


def test_enforce_budget_notes_does_nothing_when_within_budget():
    assert enforce_budget_notes(None, estimated_cost=80_000.0, budget=100_000.0) is None


def test_enforce_budget_notes_does_nothing_with_no_budget_set():
    assert enforce_budget_notes(None, estimated_cost=120_000.0, budget=None) is None


# ---- fill_missing_days (Part 5, server-side enforcement) --------------------

_ATTRACTION = {"id": "33333333-3333-3333-3333-333333333333", "name": "Waterfall",
               "lat": 7.30, "lon": 80.64, "currency": "LKR", "tags": []}


def _state_with_one_assembled_day(duration_days: int) -> TripState:
    state = TripState(user_input="x", destination="Kandy", duration_days=duration_days,
                      start_location={"lat": 7.29, "lon": 80.63})
    state.attractions = [_ATTRACTION]
    state.trip_context = {"date_window": {"start_date": "2026-10-01"}}
    return state


def test_fill_missing_days_does_nothing_when_every_day_is_already_present():
    state = _state_with_one_assembled_day(1)
    day1 = ItineraryDay(day=1, date="2026-10-01", items=[], day_cost=0.0)

    filled, warnings = fill_missing_days(state, [day1], cost_table={})

    assert filled == [day1]
    assert warnings == []


def test_fill_missing_days_builds_the_days_the_model_never_reached():
    state = _state_with_one_assembled_day(3)
    day1 = ItineraryDay(
        day=1, date="2026-10-01",
        items=[ItineraryItem(time="09:00", end_time="10:00", type="attraction", listing_id=_ATTR_ID,
                             name="Temple", lat=7.30, lon=80.64, est_cost=500.0, currency="LKR")],
        day_cost=500.0,
    )

    filled, warnings = fill_missing_days(state, [day1], cost_table={})

    assert [d.day for d in filled] == [1, 2, 3]
    assert filled[0] is day1   # the real day is untouched, not rebuilt
    assert filled[1].date == "2026-10-02"
    assert filled[2].date == "2026-10-03"
    assert len(warnings) == 2
    assert "day 2" in warnings[0] and "day 3" in warnings[1]


def test_fill_missing_days_uses_the_real_candidate_pool_for_the_filled_day():
    state = _state_with_one_assembled_day(2)
    day1 = ItineraryDay(day=1, date="2026-10-01", items=[], day_cost=0.0)

    filled, _warnings = fill_missing_days(state, [day1], cost_table={})

    day2_attractions = [i.listing_id for i in filled[1].items if i.type == "attraction"]
    assert day2_attractions == [_ATTRACTION["id"]]


# ---- fill_missing_days's force_rebuild_days (2026-09-26, Tiers 2 & 4) ------

def test_fill_missing_days_force_rebuilds_a_present_but_unbacked_day():
    # A day that IS in assembled_days (so "missing" alone wouldn't catch it)
    # but was flagged unbacked by assemble_planner_days, or day-scoped by
    # output_validator.day_scoped_repair_target - force_rebuild_days treats
    # it exactly like a genuinely missing day: excluded from "already
    # present", rebuilt with the same real candidate pool.
    state = _state_with_one_assembled_day(2)
    day1 = ItineraryDay(day=1, date="2026-10-01", items=[], day_cost=0.0)
    unbacked_day2 = ItineraryDay(
        day=2, date="2026-10-02",
        items=[ItineraryItem(time="09:00", end_time="10:00", type="hotel", listing_id=_HOTEL_ID,
                             name="Hotel", lat=7.29, lon=80.63, est_cost=99999.0, currency="LKR")],
        day_cost=99999.0,
    )

    filled, warnings = fill_missing_days(
        state, [day1, unbacked_day2], cost_table={}, force_rebuild_days=[2],
    )

    assert [d.day for d in filled] == [1, 2]
    assert filled[0] is day1   # untouched - not in force_rebuild_days
    assert filled[1] is not unbacked_day2   # rebuilt, not the stale model day
    assert filled[1].day_cost != 99999.0
    day2_attractions = [i.listing_id for i in filled[1].items if i.type == "attraction"]
    assert day2_attractions == [_ATTRACTION["id"]]
    assert len(warnings) == 1
    assert "day 2" in warnings[0] and "rebuilt deterministically" in warnings[0]


def test_fill_missing_days_force_rebuild_combines_with_a_genuinely_missing_day():
    state = _state_with_one_assembled_day(3)
    day1 = ItineraryDay(day=1, date="2026-10-01", items=[], day_cost=0.0)
    unbacked_day2 = ItineraryDay(day=2, date="2026-10-02", items=[], day_cost=12345.0)
    # day 3 is genuinely absent from assembled_days entirely

    filled, warnings = fill_missing_days(
        state, [day1, unbacked_day2], cost_table={}, force_rebuild_days=[2],
    )

    assert [d.day for d in filled] == [1, 2, 3]
    assert filled[1].day_cost != 12345.0   # day 2 was force-rebuilt
    assert filled[2].date == "2026-10-03"   # day 3 filled as genuinely missing
    assert len(warnings) == 2


def test_a_later_day_repeating_an_earlier_days_attraction_is_marked_for_rebuild():
    # Regression (live-found 2026-09-30): build_day_plan calls issued in the
    # same turn run concurrently, so two days can both schedule the same
    # attraction (Bembewa on days 1 and 2 of a Hambantota trip).
    trace = [TraceStep(step=1, tool_calls=[
        _build_day_plan_call(1, "2026-10-01", 500.0),
        _build_day_plan_call(2, "2026-10-02", 500.0),   # same _ATTR_ID again
    ])]
    assembled, warnings, unbacked = assemble_planner_days(trace, [])
    assert unbacked == [2]
    assert any("repeated an attraction" in w for w in warnings)


def test_days_with_different_attractions_are_left_alone():
    other = _build_day_plan_call(2, "2026-10-02", 500.0)
    other.observation["items"][0]["listing_id"] = "33333333-3333-3333-3333-333333333333"
    trace = [TraceStep(step=1, tool_calls=[_build_day_plan_call(1, "2026-10-01", 500.0), other])]
    _, _, unbacked = assemble_planner_days(trace, [])
    assert unbacked == []
