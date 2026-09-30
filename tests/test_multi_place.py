# tests/test_multi_place.py
# Multi-place trips (2026-10-01): "3 days around down south (Galle and
# Matara)" failed with "could not resolve destination 'galle and matara'" and
# came back as a card of empty days. Covers the place resolver, the per-day
# leg split, the leg-by-leg day builder, the graph's early stop on an
# unresolvable destination, and the validator's distance check.
from datetime import date
from unittest.mock import AsyncMock

import app.core.orchestrator as orchestrator_module
from app.core.destinations import display_name, resolve_destinations
from app.core.legs import plan_legs
from app.core.orchestrator import orchestrator
from app.core.output_validator import ValidationContext, _geo_near_dest
from app.core.planner_shared import build_leg_days, is_multi_place
from app.core.state import TripState
from app.models.schemas import ItineraryDay, ItineraryItem, PlannerOutput

_KNOWN = {
    "galle": {"name": "Galle District", "lat": 6.05, "lon": 80.22, "district_id": "d-galle",
              "confidence": "high", "country": "Sri Lanka"},
    "matara": {"name": "Matara District", "lat": 5.95, "lon": 80.55, "district_id": "d-matara",
               "confidence": "high", "country": "Sri Lanka"},
    "hambantota": {"name": "Hambantota District", "lat": 6.12, "lon": 81.12, "district_id": "d-hamb",
                   "confidence": "high", "country": "Sri Lanka"},
    "paris": {"name": "Paris", "lat": 48.85, "lon": 2.35, "district_id": None,
              "confidence": "out_of_country", "country": "France"},
}


async def _fake_place(name):
    return _KNOWN.get(name.strip().lower())


async def _no_district(lat, lon):
    return None


async def _resolve(text):
    return await resolve_destinations(text, resolve_place=_fake_place, resolve_district=_no_district)


# ─────────────────────────── resolve_destinations ───────────────────────────

async def test_two_places_joined_with_and_resolve_separately():
    places = await _resolve("galle and matara")
    assert [p["district_id"] for p in places] == ["d-galle", "d-matara"]


async def test_other_separators_and_filler_words():
    assert [p["district_id"] for p in await _resolve("Galle, Matara")] == ["d-galle", "d-matara"]
    assert [p["district_id"] for p in await _resolve("around Galle & the Matara area")] == ["d-galle", "d-matara"]


async def test_region_alias_never_reaches_the_geocoder():
    # "down south" looked up whole was Down South, Washington, USA.
    places = await _resolve("down south")
    assert [p["district_id"] for p in places] == ["d-galle", "d-matara", "d-hamb"]


async def test_single_place_is_unchanged():
    assert [p["district_id"] for p in await _resolve("Galle")] == ["d-galle"]


async def test_duplicates_are_dropped():
    assert [p["district_id"] for p in await _resolve("Galle and galle")] == ["d-galle"]


async def test_nothing_found_is_empty():
    assert await _resolve("Xyzabc") == []
    assert await _resolve("Xyzabc and Qwerty") == []


async def test_foreign_only_is_reported_as_abroad_but_mixed_keeps_sri_lanka():
    abroad = await _resolve("Paris")
    assert abroad[0]["confidence"] == "out_of_country"
    mixed = await _resolve("Paris and Galle")
    assert [p["district_id"] for p in mixed] == ["d-galle"]


def test_display_name_drops_district_suffix():
    assert display_name([_KNOWN["galle"], _KNOWN["matara"]]) == "Galle & Matara"


# ─────────────────────────── plan_legs ───────────────────────────

def _summary(legs):
    return [(l.place_index, l.checkin, l.checkout, l.nights) for l in legs]


def test_three_days_two_places_one_night_each():
    assert _summary(plan_legs(2, 3)) == [
        (0, True, False, 1),    # day 1 Galle, check in
        (1, True, False, 1),    # day 2 Matara, check in
        (1, False, True, 1),    # day 3 Matara, check out
    ]


def test_two_days_two_places_second_is_a_day_trip():
    assert _summary(plan_legs(2, 2)) == [(0, True, False, 1), (1, False, False, 0)]


def test_five_days_two_places_nights_split_evenly():
    legs = plan_legs(2, 5)
    assert [l.place_index for l in legs] == [0, 0, 1, 1, 1]
    assert [l.nights for l in legs] == [2, 2, 2, 2, 2]
    assert [l.day for l in legs if l.checkin] == [1, 3]
    assert [l.day for l in legs if l.checkout] == [5]


def test_one_place_matches_the_single_place_rules():
    assert _summary(plan_legs(1, 3)) == [(0, True, False, 2), (0, False, False, 2), (0, False, True, 2)]
    assert _summary(plan_legs(1, 1)) == [(0, False, False, 0)]


def test_more_places_than_days_is_capped():
    assert [l.place_index for l in plan_legs(3, 2)] == [0, 1]


# ─────────────────────────── build_leg_days ───────────────────────────

def _listing(id_, district, lat, lon, **extra):
    return {"id": id_, "name": id_, "district_id": district, "lat": lat, "lon": lon, "tags": [], **extra}


def _multi_state(**overrides):
    galle, matara = _KNOWN["galle"], _KNOWN["matara"]
    state = TripState(
        user_input="x", destination="Galle and Matara", duration_days=3,
        trip_context={
            "destination_name": "Galle & Matara", "district_id": "d-galle", "lat": galle["lat"], "lon": galle["lon"],
            "places": [
                {"name": "Galle District", "district_id": "d-galle", "lat": galle["lat"], "lon": galle["lon"]},
                {"name": "Matara District", "district_id": "d-matara", "lat": matara["lat"], "lon": matara["lon"]},
            ],
            "date_window": {"start_date": "2026-10-05", "end_date": "2026-10-07",
                            "dates": ["2026-10-05", "2026-10-06", "2026-10-07"]},
            "per_day_weather": [],
        },
        hotels=[_listing("h-galle", "d-galle", 6.03, 80.21, price_per_night=10000),
                _listing("h-matara", "d-matara", 5.94, 80.54, price_per_night=6000)],
        restaurants=[_listing(f"r-galle-{i}", "d-galle", 6.03 + i * 0.002, 80.21) for i in range(3)]
        + [_listing(f"r-matara-{i}", "d-matara", 5.94 + i * 0.002, 80.54) for i in range(4)],
        attractions=[_listing(f"a-galle-{i}", "d-galle", 6.03 + i * 0.004, 80.215) for i in range(4)]
        + [_listing(f"a-matara-{i}", "d-matara", 5.945 + i * 0.004, 80.545) for i in range(6)],
    )
    for key, value in overrides.items():
        setattr(state, key, value)
    return state


def _stop_districts(day: ItineraryDay) -> set[str]:
    return {i.listing_id.split("-")[1] for i in day.items if i.listing_id}


def test_each_leg_uses_its_own_hotel_and_stops():
    state = _multi_state()
    assert is_multi_place(state)

    days = build_leg_days(state, {})

    assert [d.day for d in days] == [1, 2, 3]
    assert _stop_districts(days[0]) == {"galle"}
    assert _stop_districts(days[1]) == {"matara"}
    assert _stop_districts(days[2]) == {"matara"}
    # Day 1 opens at Galle's hotel, day 2 at Matara's - one night billed each.
    assert (days[0].items[0].type, days[0].items[0].listing_id, days[0].items[0].est_cost) == ("hotel", "h-galle", 10000.0)
    assert (days[1].items[0].type, days[1].items[0].listing_id, days[1].items[0].est_cost) == ("hotel", "h-matara", 6000.0)
    # Day 3 ends by leaving the Matara hotel, at no extra charge.
    assert (days[2].items[-1].type, days[2].items[-1].listing_id, days[2].items[-1].est_cost) == ("hotel", "h-matara", 0.0)
    assert days[0].theme == "Galle" and days[1].theme == "Matara"


def test_no_attraction_repeats_across_days():
    days = build_leg_days(_multi_state(), {})
    ids = [i.listing_id for d in days for i in d.items if i.type == "attraction"]
    assert len(ids) == len(set(ids))


def test_single_place_state_is_not_multi_place():
    state = _multi_state()
    state.trip_context = {**state.trip_context, "places": state.trip_context["places"][:1]}
    assert not is_multi_place(state)


# ─────────────────────────── graph ───────────────────────────

async def _passthrough(state):
    return state


async def test_unresolvable_destination_asks_and_skips_the_llm_agents(monkeypatch):
    async def _unresolved(state):
        state.errors.append("orchestrator_failed: could not resolve destination 'xyzabc'")
        state.clarification_needed = "I couldn't find \"xyzabc\" in Sri Lanka."

    recommend = AsyncMock()
    monkeypatch.setattr(orchestrator_module, "fill_slots", AsyncMock(side_effect=_passthrough))
    monkeypatch.setattr(orchestrator_module, "resolve_trip_context", _unresolved)
    monkeypatch.setattr(orchestrator_module, "RecommendationAgent", lambda: recommend)

    result = await orchestrator.ainvoke(TripState(user_input="x", destination="xyzabc", duration_days=3))

    assert result["completed_steps"] == ["validate", "policy", "slot_fill", "orchestrate", "respond"]
    recommend.execute.assert_not_called()
    assert result["itinerary"] == []
    assert "couldn't find" in result["final_response"]


async def test_multi_place_plan_skips_the_llm_planner(monkeypatch):
    state = _multi_state()

    async def _context(s):
        s.trip_context = state.trip_context

    class _Recommend:
        async def execute(self, s):
            s.hotels, s.restaurants, s.attractions = state.hotels, state.restaurants, state.attractions
            s.recommendations = state.attractions
            s.candidate_listing_ids = [i["id"] for i in state.hotels + state.restaurants + state.attractions]

    planner = AsyncMock()
    monkeypatch.setattr(orchestrator_module, "fill_slots", AsyncMock(side_effect=_passthrough))
    monkeypatch.setattr(orchestrator_module, "resolve_trip_context", _context)
    monkeypatch.setattr(orchestrator_module, "RecommendationAgent", lambda: _Recommend())
    monkeypatch.setattr(orchestrator_module, "PlannerAgent", lambda: planner)
    monkeypatch.setattr(orchestrator_module, "_fetch_cost_table", AsyncMock(return_value={}))

    result = await orchestrator.ainvoke(TripState(user_input="x", destination="Galle and Matara", duration_days=3))

    planner.execute.assert_not_called()
    assert len(result["itinerary"]) == 3
    assert all(day["items"] for day in result["itinerary"])


async def test_empty_plan_is_not_returned(monkeypatch):
    async def _context(s):
        s.trip_context = {"destination_name": "Kandy", "district_id": "d1", "lat": 7.29, "lon": 80.63}

    class _NoRecs:
        async def execute(self, s):
            return None

    class _EmptyFallback:
        itinerary = [{"day": 1, "date": "2026-10-01", "items": [], "day_cost": 0.0}]
        estimated_cost = 0.0
        budget_notes = None

    monkeypatch.setattr(orchestrator_module, "fill_slots", AsyncMock(side_effect=_passthrough))
    monkeypatch.setattr(orchestrator_module, "resolve_trip_context", _context)
    monkeypatch.setattr(orchestrator_module, "RecommendationAgent", lambda: _NoRecs())
    monkeypatch.setattr(orchestrator_module, "build_plan", AsyncMock(return_value=_EmptyFallback()))

    result = await orchestrator.ainvoke(TripState(user_input="x", destination="Kandy", duration_days=1))

    assert result["itinerary"] == []
    assert result["estimated_cost"] is None


# ─────────────────────────── validator ───────────────────────────

def test_geo_check_measures_to_the_nearest_place():
    far_east = ItineraryItem(time="09:00", end_time="10:00", type="attraction", listing_id="x",
                             name="Yala", lat=6.37, lon=81.52, est_cost=0.0, currency="LKR", notes="")
    plan = PlannerOutput(itinerary=[ItineraryDay(day=1, date="2026-10-05", items=[far_east], day_cost=0.0)],
                         estimated_cost=0.0, currency="LKR", budget_notes=None)
    colombo = {"lat": 6.93, "lon": 79.85}
    base = dict(duration_days=1, valid_dates={"2026-10-05"}, budget=None, destination=colombo,
                candidate_listing_ids={"x"})

    assert _geo_near_dest(plan, ValidationContext(**base)) is not None
    assert _geo_near_dest(plan, ValidationContext(**base, destinations=[colombo, {"lat": 6.12, "lon": 81.12}])) is None
