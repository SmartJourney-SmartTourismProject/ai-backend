# tests/test_followup_replan.py
# app/core/followup_replan.py's deterministic targeted-day rebuild - no
# LLM, real DB calls mocked (search_listings_by_district, get_pool).

from unittest.mock import AsyncMock, MagicMock

import app.core.followup_replan as followup_replan_module
import app.utils.slot_filling as slot_filling_module
from app.core.followup_replan import rebuild_targeted_days
from app.core.state import TripState
from app.models.schemas import ExtractedSlots
from app.utils.slot_filling import fill_slots


def _hotel(id_="h1", lat=7.29, lon=80.63):
    return {"id": id_, "name": "Hotel", "tags": ["stay"], "lat": lat, "lon": lon,
            "rating": 4.0, "rating_count": 10, "price_level": 3, "price_per_night": None, "currency": "LKR"}


def _attraction(id_, price_level=3, lat=7.30, lon=80.64):
    return {"id": id_, "name": f"Attraction {id_}", "tags": [], "lat": lat, "lon": lon,
            "rating": 4.0, "rating_count": 10, "price_level": price_level, "currency": "LKR"}


def _restaurant(id_, price_level=3, lat=7.29, lon=80.64):
    return {"id": id_, "name": f"Restaurant {id_}", "tags": [], "lat": lat, "lon": lon,
            "rating": 4.0, "rating_count": 10, "price_level": price_level, "currency": "LKR"}


def _base_state(**overrides) -> TripState:
    defaults = dict(
        user_input="make day 2 cheaper",
        is_followup=True,
        followup_scope="shape_only",
        trip_context={"destination_name": "Kandy", "district_id": "d1", "lat": 7.29, "lon": 80.63},
        itinerary=[
            {"day": 1, "date": "2026-10-01", "items": [
                {"time": "09:00", "end_time": "10:00", "type": "hotel", "listing_id": "h1",
                 "name": "Hotel", "lat": 7.29, "lon": 80.63, "est_cost": 5000.0, "currency": "LKR", "notes": ""},
            ], "day_cost": 5000.0},
            {"day": 2, "date": "2026-10-02", "items": [
                {"time": "09:00", "end_time": "10:30", "type": "attraction", "listing_id": "a1",
                 "name": "Old Attraction", "lat": 7.30, "lon": 80.64, "est_cost": 8000.0, "currency": "LKR", "notes": ""},
            ], "day_cost": 8000.0},
            {"day": 3, "date": "2026-10-03", "items": [
                {"time": "09:00", "end_time": "10:00", "type": "attraction", "listing_id": "a2",
                 "name": "Final Attraction", "lat": 7.31, "lon": 80.65, "est_cost": 1000.0, "currency": "LKR", "notes": ""},
            ], "day_cost": 1000.0},
        ],
    )
    defaults.update(overrides)
    return TripState(**defaults)


def _patch_search(monkeypatch, hotels=None, restaurants=None, attractions=None):
    async def _fake_search(district_id, category, **kwargs):
        items = {"hotel": hotels or [_hotel()], "restaurant": restaurants or [_restaurant("r1")],
                  "attraction": attractions or [_attraction("a1"), _attraction("a2")]}[category]
        return {"items": items, "total": len(items), "truncated": False}

    monkeypatch.setattr(followup_replan_module, "search_listings_by_district", _fake_search)
    monkeypatch.setattr(followup_replan_module, "get_pool", AsyncMock(return_value=None))


async def test_only_targeted_day_is_rebuilt_others_untouched(monkeypatch):
    _patch_search(monkeypatch)
    state = _base_state(followup_target_days=[2])
    original_day1 = dict(state.itinerary[0])
    original_day3 = dict(state.itinerary[2])

    await rebuild_targeted_days(state)

    assert state.itinerary[0] == original_day1
    assert state.itinerary[2] == original_day3
    assert state.itinerary[1]["day"] == 2
    assert state.followup_scope != "full"


async def test_plan_source_is_fallback_deterministic_label(monkeypatch):
    _patch_search(monkeypatch)
    state = _base_state(followup_target_days=[2])
    await rebuild_targeted_days(state)
    assert state.plan_source == "fallback"


async def test_no_target_days_rebuilds_every_day(monkeypatch):
    _patch_search(monkeypatch)
    state = _base_state(followup_target_days=None)
    original_day1 = dict(state.itinerary[0])

    await rebuild_targeted_days(state)

    # Day 1 is a target too now (no days named = every day) - it may come
    # out identical in content, but it went through a real rebuild, not a
    # verbatim copy; the meaningful assertion is that it's still valid,
    # not that it's the exact same dict object.
    assert len(state.itinerary) == 3
    assert all(isinstance(d.get("day_cost"), float) for d in state.itinerary)


async def test_cheaper_flag_prefers_lower_price_level_items(monkeypatch):
    cheap = _attraction("cheap", price_level=1)
    expensive = _attraction("pricey", price_level=4)
    _patch_search(monkeypatch, attractions=[expensive, cheap])

    state = _base_state(followup_target_days=[2], followup_cheaper=True)
    await rebuild_targeted_days(state)

    day2_ids = {item["listing_id"] for item in state.itinerary[1]["items"]}
    assert "pricey" not in day2_ids   # hard-filtered out by the price ceiling
    assert "cheap" in day2_ids


async def test_cheaper_flag_also_reconsiders_the_hotel_choice(monkeypatch):
    # Regression (2026-09-03): hotels used to be excluded from the price
    # ceiling on the theory that "the hotel itself isn't what cheaper
    # swaps" - wrong, since hotels are the one category with reliable real
    # price data (Booking.com) in this dataset; excluding them defeated the
    # point for most real requests.
    cheap_hotel = _hotel(id_="cheap_hotel")
    cheap_hotel["price_level"] = 1
    pricey_hotel = _hotel(id_="pricey_hotel")
    pricey_hotel["price_level"] = 4
    _patch_search(monkeypatch, hotels=[pricey_hotel, cheap_hotel])

    state = _base_state(followup_target_days=[1], followup_cheaper=True)
    # Day 1 needs a hotel check-in for this to matter.
    state.itinerary[0]["items"] = []
    await rebuild_targeted_days(state)

    day1_ids = {item["listing_id"] for item in state.itinerary[0]["items"]}
    assert "pricey_hotel" not in day1_ids


async def test_cheaper_falls_back_to_unconstrained_when_ceiling_empties_pool(monkeypatch):
    # Every real candidate is price_level=4 - a hard ceiling of 2 would
    # wipe out the whole pool. Must degrade to unconstrained, not produce
    # an empty day.
    only_expensive = [_attraction("a1", price_level=4), _attraction("a2", price_level=4)]
    _patch_search(monkeypatch, attractions=only_expensive)

    state = _base_state(followup_target_days=[2], followup_cheaper=True)
    await rebuild_targeted_days(state)

    assert len(state.itinerary[1]["items"]) > 0


async def test_no_carried_itinerary_degrades_to_full_replan(monkeypatch):
    _patch_search(monkeypatch)
    state = _base_state(itinerary=[])

    await rebuild_targeted_days(state)

    assert state.followup_scope == "full"


async def test_no_district_id_degrades_to_full_replan(monkeypatch):
    _patch_search(monkeypatch)
    state = _base_state(trip_context={"destination_name": "Kandy"})   # no district_id

    await rebuild_targeted_days(state)

    assert state.followup_scope == "full"


async def test_data_unavailable_degrades_to_full_replan(monkeypatch):
    from app.tools.db_tool import DataUnavailable

    async def _raise(*a, **kw):
        raise DataUnavailable("db down")

    monkeypatch.setattr(followup_replan_module, "search_listings_by_district", _raise)
    monkeypatch.setattr(followup_replan_module, "get_pool", AsyncMock(return_value=None))

    state = _base_state(followup_target_days=[2])
    await rebuild_targeted_days(state)

    assert state.followup_scope == "full"
    assert any("targeted_replan_failed" in e for e in state.errors)


# ---- cross-day variety (same fix as app/core/fallback.py, found live) ------

def _many_attractions(n: int, prefix="z"):
    return [_attraction(f"{prefix}{i}", lat=7.30 + i * 0.001, lon=80.64 + i * 0.001) for i in range(n)]


async def test_rebuilt_days_dont_repeat_attractions_across_each_other(monkeypatch):
    _patch_search(monkeypatch, attractions=_many_attractions(20))
    state = _base_state(followup_target_days=None)   # rebuild every day

    await rebuild_targeted_days(state)

    def attraction_ids(day):
        return {i["listing_id"] for i in day["items"] if i["type"] == "attraction"}

    day_ids = [attraction_ids(d) for d in state.itinerary]
    assert day_ids[0], "day 1 should have real attractions to compare against"
    assert day_ids[0].isdisjoint(day_ids[1])
    assert day_ids[0].isdisjoint(day_ids[2])
    assert day_ids[1].isdisjoint(day_ids[2])


async def test_rebuilt_day_excludes_what_an_untouched_day_already_uses(monkeypatch):
    # Day 3 is untouched and already uses "a2" (see _base_state) - rebuilding
    # only day 2 must not pick "a2" again, even though it's a real, valid
    # candidate the search would otherwise return.
    _patch_search(monkeypatch, attractions=[_attraction("a1"), _attraction("a2")])
    state = _base_state(followup_target_days=[2])

    await rebuild_targeted_days(state)

    day2_ids = {i["listing_id"] for i in state.itinerary[1]["items"] if i["type"] == "attraction"}
    assert "a2" not in day2_ids   # day 3 (untouched) already has it


# ---- end-to-end: "fewer destinations per day" actually reduces the count,
# and persists across a second follow-up (Part 3, itinerary-quality/
# token-reduction pass). Chains the REAL fill_slots() (LLM mocked, same
# pattern as tests/test_slot_filling.py) into the REAL rebuild_targeted_days()
# - not a mock of either - since the bug this fixes was a gap ACROSS these
# two modules, not inside either one alone.

def _patch_fill_slots_llm(monkeypatch, extracted: ExtractedSlots):
    monkeypatch.setattr(slot_filling_module, "resolve_place", AsyncMock(return_value={
        "name": "Kandy", "lat": 7.29, "lon": 80.63, "district_id": "d1",
        "confidence": "high", "country": "Sri Lanka",
    }))
    mock_structured = MagicMock()
    mock_structured.ainvoke = AsyncMock(return_value=extracted)
    mock_llm_instance = MagicMock()
    mock_llm_instance.with_structured_output.return_value = mock_structured
    monkeypatch.setattr(slot_filling_module, "get_llm", MagicMock(return_value=mock_llm_instance))


def _three_item_day(day: int, date: str) -> dict:
    return {"day": day, "date": date, "day_cost": 0.0, "items": [
        {"time": t, "end_time": t, "type": "attraction", "listing_id": f"d{day}a{i}",
         "name": f"Attraction {day}-{i}", "lat": 7.30 + i * 0.001, "lon": 80.64 + i * 0.001,
         "est_cost": 0.0, "currency": "LKR", "notes": ""}
        for i, t in enumerate(["09:00", "10:00", "11:00"])
    ]}


async def test_fewer_destinations_per_day_reduces_the_count_end_to_end(monkeypatch):
    _patch_search(monkeypatch, attractions=_many_attractions(10, prefix="new"))
    state = TripState(
        user_input="Can we do fewer destinations per day", destination="Kandy", duration_days=2,
        is_followup=True, followup_scope="shape_only",
        trip_context={"destination_name": "Kandy", "district_id": "d1", "lat": 7.29, "lon": 80.63},
        itinerary=[_three_item_day(1, "2026-10-01"), _three_item_day(2, "2026-10-02")],
    )
    _patch_fill_slots_llm(monkeypatch, ExtractedSlots(items_per_day_delta=-1))

    state = await fill_slots(state)
    assert state.items_per_day == 2   # 3 (pace="balanced" default) - 1
    assert state.followup_scope == "shape_only"   # no real field changed, routes targeted

    await rebuild_targeted_days(state)

    for day in state.itinerary:
        attraction_count = len([i for i in day["items"] if i["type"] == "attraction"])
        assert attraction_count == 2


async def test_a_second_fewer_follow_up_compounds_not_resets(monkeypatch):
    # The actual regression: a second "even fewer" must decrement from what
    # the FIRST follow-up already set (2), landing on 1 - not silently
    # reset back to the original pace-derived count (3) each time.
    _patch_search(monkeypatch, attractions=_many_attractions(10, prefix="new"))
    state = TripState(
        user_input="Even fewer please", destination="Kandy", duration_days=2,
        is_followup=True, followup_scope="shape_only", items_per_day=2,   # carried from turn 2
        trip_context={"destination_name": "Kandy", "district_id": "d1", "lat": 7.29, "lon": 80.63},
        itinerary=[_three_item_day(1, "2026-10-01"), _three_item_day(2, "2026-10-02")],
    )
    _patch_fill_slots_llm(monkeypatch, ExtractedSlots(items_per_day_delta=-1))

    state = await fill_slots(state)
    assert state.items_per_day == 1   # 2 - 1, not 3 - 1

    await rebuild_targeted_days(state)

    for day in state.itinerary:
        attraction_count = len([i for i in day["items"] if i["type"] == "attraction"])
        assert attraction_count == 1


# ---- "make day 2 more relaxed" (live-found: came back unchanged) ---------

def _attraction_count(day: dict) -> int:
    return len([i for i in day["items"] if i["type"] == "attraction"])


async def _relax_day_2(monkeypatch, extracted: ExtractedSlots, **state_overrides) -> TripState:
    _patch_search(monkeypatch, attractions=_many_attractions(10, prefix="new"))
    state = TripState(
        user_input="make day 2 more relaxed", destination="Kandy", duration_days=3,
        is_followup=True, followup_scope="shape_only",
        trip_context={"destination_name": "Kandy", "district_id": "d1", "lat": 7.29, "lon": 80.63},
        itinerary=[_three_item_day(1, "2026-10-01"), _three_item_day(2, "2026-10-02"),
                   _three_item_day(3, "2026-10-03")],
        **state_overrides,
    )
    _patch_fill_slots_llm(monkeypatch, extracted)
    state = await fill_slots(state)
    return state


async def test_relaxed_day_2_drops_a_stop_when_the_model_extracts_nothing(monkeypatch):
    state = await _relax_day_2(monkeypatch, ExtractedSlots())
    original_day1, original_day3 = dict(state.itinerary[0]), dict(state.itinerary[2])

    await rebuild_targeted_days(state)

    assert _attraction_count(state.itinerary[1]) == 2   # was 3
    assert state.itinerary[0] == original_day1
    assert state.itinerary[2] == original_day3


async def test_relaxed_day_2_on_an_already_relaxed_trip_still_changes_day_2(monkeypatch):
    # pace="relaxed" both carried and extracted: the old full re-plan
    # rebuilt the same plan. Day 2 must still get lighter, and the trip-wide
    # pace / items_per_day must stay as they were.
    state = await _relax_day_2(monkeypatch, ExtractedSlots(pace="relaxed"), pace="relaxed")
    assert state.followup_scope == "shape_only"

    await rebuild_targeted_days(state)

    assert _attraction_count(state.itinerary[1]) == 2
    assert state.pace == "relaxed"
    assert state.items_per_day is None


async def test_relaxed_day_2_does_not_become_the_whole_trips_pace(monkeypatch):
    state = await _relax_day_2(monkeypatch, ExtractedSlots(pace="relaxed"), pace="balanced")
    assert state.pace == "balanced"
    assert state.items_per_day is None
