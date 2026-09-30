"""
Follow-up turns that ask a question rather than request a change.

Both behaviours pinned here were live defects, reported from the UI:

  1. "Show budget breakdown" rebuilt the whole itinerary. The user asked a
     question and got back a different set of places and a different total,
     because the only follow-up scopes were "full" and "shape_only" - both of
     which re-plan. A question must not mutate the plan.

  2. The rebuild also burned a full planning cycle (LLM + tools) to answer
     something already sitting on state, which is why "add a restaurant
     recommendation" appeared to do nothing while still costing tokens.
"""
from app.core.followup import classify_followup
from app.models.schemas import ExtractedSlots

EMPTY = ExtractedSlots()


class TestInformationalIsReadOnly:
    def test_budget_breakdown_is_informational(self):
        assert classify_followup("Show budget breakdown", EMPTY).scope == "informational"

    def test_phrasing_variants_all_classify_the_same(self):
        for text in ("show the cost", "cost breakdown", "how much will it cost?", "total cost"):
            assert classify_followup(text, EMPTY).scope == "informational", text

    def test_an_informational_turn_requests_no_rebuild(self):
        # target_days/cheaper drive the targeted-replan path; an informational
        # turn must carry neither, or it would still rebuild something.
        plan = classify_followup("show budget breakdown", EMPTY)
        assert plan.target_days is None
        assert plan.cheaper is False


class TestRealChangesStillReplan:
    def test_a_modification_is_not_swallowed_as_a_question(self):
        assert classify_followup("Add a restaurant recommendation", EMPTY).scope == "shape_only"

    def test_cheaper_still_replans(self):
        plan = classify_followup("Make it cheaper", EMPTY)
        assert plan.scope == "shape_only"
        assert plan.cheaper is True

    def test_a_question_that_also_instructs_replans(self):
        # "show the cost, and make it cheaper" is a change request with a
        # question attached - answering only the question would silently drop
        # the instruction, which is the worse failure of the two.
        assert classify_followup("show the cost and make it cheaper", EMPTY).scope == "shape_only"

    def test_a_question_scoped_to_one_day_replans_that_day(self):
        plan = classify_followup("budget breakdown for day 2", EMPTY)
        assert plan.scope == "shape_only"
        assert plan.target_days == [2]

    def test_a_changed_slot_still_forces_a_full_replan(self):
        assert classify_followup("how much does it cost", ExtractedSlots(duration_days=5)).scope == "full"


class TestBudgetBreakdownText:
    def _state(self):
        from app.core.state import TripState

        state = TripState(user_input="show budget breakdown")
        state.destination = "Colombo District"
        state.estimated_cost = 53394.4
        state.itinerary = [
            {
                "day": 1,
                "day_cost": 53394.4,
                "items": [
                    {"name": "British Hostel", "type": "hotel", "est_cost": 53394.4},
                    {"name": "War Memorial", "type": "attraction", "est_cost": 0.0},
                ],
            }
        ]
        return state

    def test_reports_the_totals_already_on_state(self):
        from app.core.orchestrator import _budget_breakdown_text

        text = _budget_breakdown_text(self._state())
        # The figures must match the plan the user is looking at, so they are
        # read off the itinerary rather than recomputed from a fresh plan.
        assert "53,394.40" in text
        assert "British Hostel" in text
        assert "Colombo District" in text

    def test_an_unpriced_item_is_not_shown_as_free(self):
        from app.core.orchestrator import _budget_breakdown_text

        text = _budget_breakdown_text(self._state())
        assert "no price data" in text


class TestWeatherQuestions:
    def test_rain_question_is_informational_weather(self):
        plan = classify_followup("will it rain on those days?", EMPTY)
        assert plan.scope == "informational" and plan.info_kind == "weather"

    def test_train_is_not_a_weather_question(self):
        # "rain" is a substring of "train" - must be whole-word matched.
        assert classify_followup("how do I get there by train?", EMPTY).info_kind != "weather"

    def test_budget_questions_stay_budget(self):
        assert classify_followup("show budget breakdown", EMPTY).info_kind == "budget"

    def test_weather_plus_instruction_still_replans(self):
        assert classify_followup("will it rain, and make it cheaper", EMPTY).scope == "shape_only"
