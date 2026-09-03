# tests/test_slot_filling.py
# No real Gemini calls - the LLM is mocked so this tests OUR merging/
# defaulting logic, not Google's model. No real geocoding either -
# resolve_place() is mocked to a benign Sri Lanka match by default (via
# _patch_llm, since every test that sets a destination now triggers the
# country-scoping check), with dedicated tests below overriding it to
# exercise the out-of-country path specifically.

from unittest.mock import MagicMock, AsyncMock

import app.utils.slot_filling as slot_filling_module
from app.utils.slot_filling import fill_slots
from app.models.schemas import ExtractedSlots
from app.core.state import TripState

_DEFAULT_SL_PLACE = {
    "name": "Sri Lanka Place", "lat": 7.0, "lon": 80.0,
    "district_id": "district-uuid", "confidence": "high", "country": "Sri Lanka",
}


def _patch_llm(monkeypatch, extracted: ExtractedSlots, place=None):
    # get_llm() (app/core/llm.py) is the single construction point now -
    # slot_filling.py calls get_llm("slots").with_structured_output(...),
    # so that's the seam to mock, not a module-level ChatGoogleGenerativeAI
    # symbol (which no longer exists here after the D6b rewiring).
    monkeypatch.setattr(
        slot_filling_module, "resolve_place",
        AsyncMock(return_value=place if place is not None else _DEFAULT_SL_PLACE),
    )
    mock_structured = MagicMock()
    mock_structured.ainvoke = AsyncMock(return_value=extracted)
    mock_llm_instance = MagicMock()
    mock_llm_instance.with_structured_output.return_value = mock_structured
    monkeypatch.setattr(slot_filling_module, "get_llm", MagicMock(return_value=mock_llm_instance))


async def test_full_details_extracted(monkeypatch):
    _patch_llm(monkeypatch, ExtractedSlots(
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
    _patch_llm(monkeypatch, ExtractedSlots(destination="Galle"))
    state = TripState(user_input="I want to visit Galle")

    result = await fill_slots(state)

    assert result.destination == "Galle"
    assert result.duration_days == 1


async def test_no_destination_sets_clarification_needed(monkeypatch):
    _patch_llm(monkeypatch, ExtractedSlots())
    state = TripState(user_input="Plan me a trip somewhere nice")

    result = await fill_slots(state)

    assert result.destination is None
    assert result.clarification_needed == "Which destination would you like to visit?"


async def test_known_user_profile_backfills_preferences(monkeypatch):
    # db_tool.get_user_profile is mocked directly rather than relying on
    # real mock data, since that's Member B's file and its actual contents
    # (real Supabase-backed or not) are outside this test's concern - this
    # only verifies fill_slots correctly USES whatever profile comes back.
    _patch_llm(monkeypatch, ExtractedSlots(destination="Galle"))
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
    _patch_llm(monkeypatch, ExtractedSlots(destination="Galle"))
    state = TripState(user_input="I want to visit Galle", user_id="demo-user-1")

    result = await fill_slots(state)

    assert result.interests == []
    assert result.travel_style is None
    assert result.budget is None


async def test_already_set_fields_are_not_overwritten(monkeypatch):
    _patch_llm(monkeypatch, ExtractedSlots(destination="Somewhere else", budget=999.0))
    state = TripState(user_input="doesn't matter", destination="Kandy", budget=500.0)

    result = await fill_slots(state)

    assert result.destination == "Kandy"
    assert result.budget == 500.0


async def test_followup_overwrites_already_set_fields(monkeypatch):
    # On a follow-up turn (is_followup=True), an extracted value should
    # OVERWRITE the carried-over one - "make it cheaper" must actually
    # change budget, unlike the first-turn "don't overwrite" behavior
    # tested above.
    _patch_llm(monkeypatch, ExtractedSlots(budget=600.0))
    state = TripState(
        user_input="Actually make the budget 600 dollars instead",
        destination="Kandy", budget=300.0, duration_days=2,
        is_followup=True,
    )

    result = await fill_slots(state)

    assert result.budget == 600.0
    assert result.destination == "Kandy"  # untouched field stays as carried over


async def test_followup_skips_defaulting_and_clarification(monkeypatch):
    # Destination-only defaulting, profile backfill, and the "ask for a
    # destination" clarification are first-turn-only concerns - a follow-up
    # already has a full state from the previous turn.
    _patch_llm(monkeypatch, ExtractedSlots())
    state = TripState(
        user_input="Add more food options please",
        destination="Kandy", duration_days=2, user_id="demo-user-1",
        is_followup=True,
    )

    result = await fill_slots(state)

    assert result.duration_days == 2  # not reset to 1
    assert result.clarification_needed is None


async def test_followup_empty_extraction_leaves_fields_untouched(monkeypatch):
    _patch_llm(monkeypatch, ExtractedSlots())
    state = TripState(
        user_input="Sounds great, thanks!",
        destination="Kandy", budget=300.0, interests=["culture"],
        is_followup=True,
    )

    result = await fill_slots(state)

    assert result.destination == "Kandy"
    assert result.budget == 300.0
    assert result.interests == ["culture"]


async def test_origin_location_is_geocoded_when_start_location_missing(monkeypatch):
    _patch_llm(monkeypatch, ExtractedSlots(destination="Kandy", origin_location="Polonnaruwa"))
    monkeypatch.setattr(
        slot_filling_module, "geocode_destination",
        AsyncMock(return_value={"lat": 7.9403, "lon": 81.0188}),
    )
    state = TripState(user_input="Plan a trip to Kandy, I'm starting from Polonnaruwa")

    result = await fill_slots(state)

    assert result.start_location == {"lat": 7.9403, "lon": 81.0188, "source": "text"}


async def test_origin_location_does_not_override_existing_start_location(monkeypatch):
    # GPS/IP resolution (done by the API layer before this ever runs) is
    # more precise than geocoding a place name from text, so it always wins.
    _patch_llm(monkeypatch, ExtractedSlots(destination="Kandy", origin_location="Polonnaruwa"))
    geocode_mock = AsyncMock(return_value={"lat": 7.9403, "lon": 81.0188})
    monkeypatch.setattr(slot_filling_module, "geocode_destination", geocode_mock)
    state = TripState(
        user_input="Plan a trip to Kandy, I'm starting from Polonnaruwa",
        start_location={"lat": 6.9271, "lon": 79.8612, "source": "gps"},
    )

    result = await fill_slots(state)

    assert result.start_location == {"lat": 6.9271, "lon": 79.8612, "source": "gps"}
    geocode_mock.assert_not_called()


async def test_origin_geocode_failure_leaves_start_location_unset(monkeypatch):
    _patch_llm(monkeypatch, ExtractedSlots(destination="Kandy", origin_location="Nowhereville"))
    monkeypatch.setattr(slot_filling_module, "geocode_destination", AsyncMock(return_value=None))
    state = TripState(user_input="Plan a trip to Kandy, starting from Nowhereville")

    result = await fill_slots(state)

    assert result.start_location is None


async def test_no_origin_mentioned_does_not_call_geocode(monkeypatch):
    _patch_llm(monkeypatch, ExtractedSlots(destination="Kandy"))
    geocode_mock = AsyncMock(return_value={"lat": 1.0, "lon": 1.0})
    monkeypatch.setattr(slot_filling_module, "geocode_destination", geocode_mock)
    state = TripState(user_input="Plan a trip to Kandy")

    result = await fill_slots(state)

    assert result.start_location is None
    geocode_mock.assert_not_called()


async def test_origin_location_works_on_followup_turn(monkeypatch):
    # The exact scenario found during manual demo testing: origin mentioned
    # in a later message, after the destination was already set on turn 1.
    _patch_llm(monkeypatch, ExtractedSlots(origin_location="Polonnaruwa"))
    monkeypatch.setattr(
        slot_filling_module, "geocode_destination",
        AsyncMock(return_value={"lat": 7.9403, "lon": 81.0188}),
    )
    state = TripState(
        user_input="I'm starting from Polonnaruwa",
        destination="Kandy", duration_days=3, is_followup=True,
    )

    result = await fill_slots(state)

    assert result.destination == "Kandy"  # unaffected
    assert result.start_location == {"lat": 7.9403, "lon": 81.0188, "source": "text"}


async def test_llm_failure_degrades_without_raising(monkeypatch):
    monkeypatch.setattr(
        slot_filling_module, "get_llm",
        MagicMock(side_effect=RuntimeError("API down")),
    )
    state = TripState(user_input="Plan a trip to Kandy")

    result = await fill_slots(state)

    assert any("slot_filling failed" in e for e in result.errors)
    # destination stays None since the LLM call never succeeded, so the
    # clarification path still kicks in instead of crashing.
    assert result.clarification_needed == "Which destination would you like to visit?"


# ---- country scoping: Sri Lanka only (project decision, 2026-09-02) --------

async def test_out_of_country_destination_sets_clarification_needed(monkeypatch):
    _patch_llm(monkeypatch, ExtractedSlots(destination="New York"), place={
        "name": "New York, United States", "lat": 40.71, "lon": -74.0,
        "district_id": None, "confidence": "out_of_country", "country": "United States",
    })
    state = TripState(user_input="Plan a 3-day trip to New York")

    result = await fill_slots(state)

    assert result.destination == "New York"   # extracted normally - only the check gates it
    assert result.clarification_needed is not None
    assert "Sri Lanka" in result.clarification_needed
    assert "United States" in result.clarification_needed
    # everything downstream of slot_fill must be skipped for this turn -
    # duration must NOT get defaulted to 1, profile lookup must not run.
    assert result.duration_days is None


async def test_sri_lanka_destination_does_not_set_clarification(monkeypatch):
    _patch_llm(monkeypatch, ExtractedSlots(destination="Kandy"))
    state = TripState(user_input="Plan a trip to Kandy")

    result = await fill_slots(state)

    assert result.destination == "Kandy"
    assert result.clarification_needed is None


async def test_out_of_country_check_runs_on_followup_turn_too(monkeypatch):
    """A follow-up can change the destination too ("actually, let's go to
    Paris instead") - the check must not be skipped just because
    is_followup's early-return comes later in the function."""
    _patch_llm(monkeypatch, ExtractedSlots(destination="Paris"), place={
        "name": "Paris, France", "lat": 48.85, "lon": 2.35,
        "district_id": None, "confidence": "out_of_country", "country": "France",
    })
    state = TripState(
        user_input="actually let's go to Paris instead",
        destination="Kandy", duration_days=3, is_followup=True,
    )

    result = await fill_slots(state)

    assert result.destination == "Paris"
    assert result.clarification_needed is not None
    assert "France" in result.clarification_needed


async def test_resolve_place_failure_does_not_block_a_valid_destination(monkeypatch):
    """resolve_place() itself failing (network/DB down) must degrade like
    every other tool in this codebase - log and continue, never crash the
    whole slot-filling step over a check that's advisory, not required."""
    _patch_llm(monkeypatch, ExtractedSlots(destination="Kandy"))
    # _patch_llm sets a benign default resolve_place mock - override it
    # afterward so this call actually raises instead of returning a value.
    monkeypatch.setattr(slot_filling_module, "resolve_place",
                        AsyncMock(side_effect=RuntimeError("Nominatim unreachable")))
    state = TripState(user_input="Plan a trip to Kandy")

    result = await fill_slots(state)

    assert result.destination == "Kandy"
    assert result.clarification_needed is None   # not blocked by the failed check
    assert any("destination country check failed" in e for e in result.errors)


# ---- must_avoid / pace (app/models/schemas.py additions, Phase 5) ---------

async def test_must_avoid_and_pace_extracted_on_first_turn(monkeypatch):
    _patch_llm(monkeypatch, ExtractedSlots(
        destination="Kandy", must_avoid=["hike"], pace="relaxed",
    ))
    state = TripState(user_input="Plan a relaxed trip to Kandy, no hiking, my knees are bad")

    result = await fill_slots(state)

    assert result.must_avoid == ["hike"]
    assert result.pace == "relaxed"


async def test_must_avoid_and_pace_do_not_overwrite_on_first_turn(monkeypatch):
    _patch_llm(monkeypatch, ExtractedSlots(destination="Kandy", must_avoid=["crowds"], pace="packed"))
    state = TripState(user_input="doesn't matter", must_avoid=["hike"], pace="relaxed")

    result = await fill_slots(state)

    assert result.must_avoid == ["hike"]   # already set - not overwritten on a first turn
    assert result.pace == "relaxed"


async def test_must_avoid_and_pace_overwrite_on_followup(monkeypatch):
    _patch_llm(monkeypatch, ExtractedSlots(must_avoid=["crowds"], pace="packed"))
    state = TripState(
        user_input="actually let's pack the schedule and also avoid crowds",
        destination="Kandy", must_avoid=["hike"], pace="relaxed", is_followup=True,
    )

    result = await fill_slots(state)

    assert result.must_avoid == ["crowds"]   # follow-up overwrites, same as budget/interests
    assert result.pace == "packed"


async def test_must_avoid_and_pace_default_to_empty_and_none():
    # No _patch_llm involved - just confirms TripState's own defaults, since
    # these are new fields (Phase 5 addition) and must not require every
    # caller to set them explicitly.
    state = TripState(user_input="anything")
    assert state.must_avoid == []
    assert state.pace is None
