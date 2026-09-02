# tests/test_session_store.py
# Phase 7: session_store.py is now async and DB-backed (ai_session table)
# instead of a sync JSON file. The autouse `_isolate_session_store` fixture
# in conftest.py swaps in a fake asyncpg pool, so these tests never touch a
# real database - same isolation intent as before, different mechanism.

from app.core.state import TripState
from app.utils.session_store import load_session, save_session


async def test_unknown_session_returns_none():
    assert await load_session("nope") is None


async def test_save_and_load_round_trips_carry_over_fields():
    state = TripState(
        user_input="Plan a trip to Kandy",
        destination="Kandy",
        duration_days=2,
        budget=300.0,
        interests=["culture"],
        must_avoid=["hike"],
        pace="relaxed",
        itinerary=[{"day": 1, "items": []}],
        trip_context={"destination_name": "Kandy", "district_id": "d1", "lat": 7.29, "lon": 80.63},
    )

    await save_session("session-1", state)
    loaded = await load_session("session-1")

    assert loaded["destination"] == "Kandy"
    assert loaded["duration_days"] == 2
    assert loaded["budget"] == 300.0
    assert loaded["interests"] == ["culture"]
    assert loaded["must_avoid"] == ["hike"]
    assert loaded["pace"] == "relaxed"
    assert loaded["itinerary"] == [{"day": 1, "items": []}]
    assert loaded["trip_context"]["district_id"] == "d1"


async def test_per_turn_fields_are_not_carried_over():
    # user_input, errors, clarification_needed, completed_steps,
    # final_response, and every candidate_*/weather/disaster/react_traces/
    # plan_source field describe a single turn or are re-derived, never the
    # ongoing session (AGENT_ARCHITECTURE.md §5) - none of them should be
    # part of what's saved/loaded.
    state = TripState(
        user_input="anything",
        destination="Galle",
        errors=["some error"],
        clarification_needed="Which destination?",
        completed_steps=["validate", "policy"],
        final_response="some response",
        candidate_attractions=[{"id": "x"}],
        weather={"current": {}},
        disaster={"safe": True},
        plan_source="fallback",
    )

    await save_session("session-2", state)
    loaded = await load_session("session-2")

    for field in ("user_input", "errors", "clarification_needed", "completed_steps",
                  "final_response", "candidate_attractions", "weather", "disaster", "plan_source"):
        assert field not in loaded


async def test_second_session_does_not_clobber_first():
    await save_session("session-a", TripState(user_input="x", destination="Kandy"))
    await save_session("session-b", TripState(user_input="x", destination="Galle"))

    assert (await load_session("session-a"))["destination"] == "Kandy"
    assert (await load_session("session-b"))["destination"] == "Galle"


async def test_loading_a_previous_session_and_applying_it_to_a_new_state():
    original = TripState(
        user_input="Plan a trip to Kandy, budget 300",
        destination="Kandy",
        budget=300.0,
        duration_days=2,
    )
    await save_session("session-3", original)

    carried_over = await load_session("session-3")
    follow_up_state = TripState(
        user_input="Make the budget 600 instead",
        session_id="session-3",
        is_followup=True,
        **carried_over,
    )

    assert follow_up_state.destination == "Kandy"
    assert follow_up_state.budget == 300.0  # not yet updated - that's fill_slots' job
    assert follow_up_state.is_followup is True


# ---- selections carried as ids only, not full candidate rows ---------------

async def test_selections_are_carried_as_ids_only_not_full_rows():
    state = TripState(user_input="x", destination="Kandy")
    state.hotels = [{"id": "h1", "category": "hotel", "name": "Hotel A", "lat": 7.29,
                      "rank": 1, "score": 0.9, "reason": "central"}]
    state.attractions = [{"id": "a1", "category": "attraction", "name": "Temple", "lat": 7.30}]

    await save_session("session-4", state)

    # load_session strips "selections" back out (not a real TripState field -
    # TripState(**carried_over) would reject an unknown kwarg), so read the
    # fake pool directly to check what was actually persisted.
    import app.utils.session_store as session_store_module
    pool = await session_store_module.get_pool()
    import json
    raw = json.loads(pool.rows["session-4"]["state"])

    assert raw["selections"]["hotels"] == [{"id": "h1", "category": "hotel"}]
    assert raw["selections"]["attractions"] == [{"id": "a1", "category": "attraction"}]
    assert "name" not in raw["selections"]["hotels"][0]
    assert "lat" not in raw["selections"]["hotels"][0]


async def test_loaded_session_never_carries_a_selections_key():
    state = TripState(user_input="x", destination="Kandy")
    state.hotels = [{"id": "h1", "category": "hotel"}]
    await save_session("session-5", state)

    loaded = await load_session("session-5")
    assert "selections" not in loaded


# ---- non-UUID user_id degrades gracefully, doesn't break the save ----------

async def test_non_uuid_user_id_does_not_prevent_session_save():
    state = TripState(user_input="x", destination="Kandy", user_id="not-a-real-uuid")
    await save_session("session-6", state)

    loaded = await load_session("session-6")
    assert loaded["destination"] == "Kandy"


# ---- degradation when the database is unavailable --------------------------

async def test_load_returns_none_when_pool_unavailable(monkeypatch):
    import app.utils.session_store as session_store_module

    async def _no_pool():
        return None

    monkeypatch.setattr(session_store_module, "get_pool", _no_pool)
    assert await load_session("anything") is None


async def test_save_does_not_raise_when_pool_unavailable(monkeypatch):
    import app.utils.session_store as session_store_module

    async def _no_pool():
        return None

    monkeypatch.setattr(session_store_module, "get_pool", _no_pool)
    await save_session("anything", TripState(user_input="x"))   # must not raise
