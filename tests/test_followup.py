# tests/test_followup.py
# app/core/followup.py's deterministic classifier - no LLM, no I/O. Decides
# whether a follow-up turn only changes plan SHAPE (targeted rebuild) or a
# real trip parameter (full re-plan).

from app.core.followup import classify_followup
from app.models.schemas import ExtractedSlots


def _slots(**overrides) -> ExtractedSlots:
    return ExtractedSlots(**overrides)


def test_no_extracted_fields_and_no_keyword_is_shape_only():
    plan = classify_followup("make it nicer", _slots())
    assert plan.scope == "shape_only"
    assert plan.target_days is None
    assert plan.cheaper is False


def test_day_number_and_cheaper_keyword_detected():
    plan = classify_followup("make day 2 cheaper", _slots())
    assert plan.scope == "shape_only"
    assert plan.target_days == [2]
    assert plan.cheaper is True


def test_multiple_day_numbers_all_captured_sorted_deduped():
    plan = classify_followup("make day 3 and day 1 cheaper, also day 1 again", _slots())
    assert plan.target_days == [1, 3]


def test_changed_destination_forces_full_replan():
    plan = classify_followup("actually let's go to Galle instead", _slots(destination="Galle"))
    assert plan.scope == "full"


def test_changed_budget_forces_full_replan():
    plan = classify_followup("budget is now 80000", _slots(budget=80000.0))
    assert plan.scope == "full"


def test_changed_interests_forces_full_replan():
    plan = classify_followup("I also like nature now", _slots(interests=["nature"]))
    assert plan.scope == "full"


def test_changed_origin_location_forces_full_replan():
    # Matches golden scenario 6 - a new start_location legitimately needs
    # distances rescored and the itinerary reordered, not a shape tweak.
    plan = classify_followup("I'm starting from Polonnaruwa", _slots(origin_location="Polonnaruwa"))
    assert plan.scope == "full"


def test_wants_different_places_phrase_forces_full_even_with_no_slot_change():
    plan = classify_followup("swap the temple for something indoors", _slots())
    assert plan.scope == "full"


def test_dont_like_phrase_forces_full():
    plan = classify_followup("I don't like the hotel you picked", _slots())
    assert plan.scope == "full"


def test_cheaper_without_a_day_number_targets_every_day():
    plan = classify_followup("make it cheaper overall", _slots())
    assert plan.scope == "shape_only"
    assert plan.target_days is None
    assert plan.cheaper is True
