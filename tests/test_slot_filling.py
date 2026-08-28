# tests/test_slot_filling.py
# No real Gemini calls - the LLM is mocked so this tests OUR merging/
# defaulting logic, not Google's model.

from unittest.mock import MagicMock, AsyncMock

import app.utils.slot_filling as slot_filling_module
from app.utils.slot_filling import fill_slots, _ExtractedSlots
from app.core.state import TripState


def _patch_llm(monkeypatch, extracted: _ExtractedSlots):
    mock_structured = MagicMock()
    mock_structured.ainvoke = AsyncMock(return_value=extracted)
    mock_llm_instance = MagicMock()
    mock_llm_instance.with_structured_output.return_value = mock_structured
    monkeypatch.setattr(slot_filling_module, "ChatGoogleGenerativeAI", MagicMock(return_value=mock_llm_instance))


async def test_full_details_extracted(monkeypatch):
    _patch_llm(monkeypatch, _ExtractedSlots(
        destination="Kandy", duration_days=7, budget=500.0, travelers=3, interests=["nature", "food"],
    ))
    state = TripState(user_input=(
        "I want to take my wife and kid to Kandy for a week, "
        "we love nature and food, budget around $500"
    ))

    result = await fill_slots(state)

    assert result.destination == "Kandy"
    assert result.duration_days == 7
    assert result.travelers == 3
    assert result.budget == 500.0
    assert result.interests == ["nature", "food"]
    assert result.clarification_needed is None


async def test_destination_only_defaults_duration_to_one_day(monkeypatch):
    _patch_llm(monkeypatch, _ExtractedSlots(destination="Galle"))
    state = TripState(user_input="I want to visit Galle")

    result = await fill_slots(state)

    assert result.destination == "Galle"
    assert result.duration_days == 1


async def test_no_destination_sets_clarification_needed(monkeypatch):
    _patch_llm(monkeypatch, _ExtractedSlots())
    state = TripState(user_input="Plan me a trip somewhere nice")

    result = await fill_slots(state)

    assert result.destination is None
    assert result.clarification_needed == "Which destination would you like to visit?"


async def test_known_user_profile_backfills_preferences(monkeypatch):
    # db_tool.get_user_profile is mocked directly rather than relying on
    # real mock data, since that's Member B's file and its actual contents
    # (real Supabase-backed or not) are outside this test's concern - this
    # only verifies fill_slots correctly USES whatever profile comes back.
    _patch_llm(monkeypatch, _ExtractedSlots(destination="Galle"))
    monkeypatch.setattr(
        slot_filling_module.db_tool, "get_user_profile",
        AsyncMock(return_value={
            "interests": ["culture", "food"],
            "travel_style": "budget",
            "budget": 300.0,
            "home_location": None,
        }),
    )
    state = TripState(user_input="I want to visit Galle", user_id="demo-user-1")

    result = await fill_slots(state)

    assert result.interests == ["culture", "food"]
    assert result.travel_style == "budget"
    assert result.budget == 300.0


async def test_no_user_profile_data_leaves_fields_unset(monkeypatch):
    # Documents current real behavior: db_tool.get_user_profile isn't wired
    # to real user data yet (always returns empty defaults), so the
    # backfill logic here has nothing to pull from today - not a bug in
    # this file, just a note for whoever wires up real profiles later.
    _patch_llm(monkeypatch, _ExtractedSlots(destination="Galle"))
    state = TripState(user_input="I want to visit Galle", user_id="demo-user-1")

    result = await fill_slots(state)

    assert result.interests == []
    assert result.travel_style is None
    assert result.budget is None


async def test_already_set_fields_are_not_overwritten(monkeypatch):
    _patch_llm(monkeypatch, _ExtractedSlots(destination="Somewhere else", budget=999.0))
    state = TripState(user_input="doesn't matter", destination="Kandy", budget=500.0)

    result = await fill_slots(state)

    assert result.destination == "Kandy"
    assert result.budget == 500.0


async def test_llm_failure_degrades_without_raising(monkeypatch):
    monkeypatch.setattr(
        slot_filling_module, "ChatGoogleGenerativeAI",
        MagicMock(side_effect=RuntimeError("API down")),
    )
    state = TripState(user_input="Plan a trip to Kandy")

    result = await fill_slots(state)

    assert any("slot_filling failed" in e for e in result.errors)
    # destination stays None since the LLM call never succeeded, so the
    # clarification path still kicks in instead of crashing.
    assert result.clarification_needed == "Which destination would you like to visit?"
