# tests/test_followup_replan.py
# app/core/followup_replan.py's deterministic targeted-day rebuild - no
# LLM, real DB calls mocked (search_listings_by_district, get_pool).

from unittest.mock import AsyncMock

import app.core.followup_replan as followup_replan_module
from app.core.followup_replan import rebuild_targeted_days
from app.core.state import TripState


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
