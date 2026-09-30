"""
A kind of stop the traveler ruled out must not appear in the plan.

Live failure, 2026-09-30: "give view points only" came back with two
restaurants in it. Every planner path passed `include_lunch=True,
include_dinner=True` as literals, so meals were inserted regardless of what was
asked. `must_avoid` could not express the request either - it filters listings
by subject tag ("no hiking"), not by kind of stop, so there was no way for the
model to say "no restaurants at all".

`exclude_categories` is that missing channel: the extractor puts the request
into a structured field, and one shared helper turns it into planner
constraints so a new planner path cannot quietly reintroduce the literals.
"""
from app.core.fallback import PlanningContext
from app.core.planner_shared import category_excluded, meal_slots
from app.core.state import TripState


def _state(**over) -> TripState:
    state = TripState(user_input="x")
    for key, value in over.items():
        setattr(state, key, value)
    return state


class TestMealSlots:
    def test_meals_are_included_by_default(self):
        assert meal_slots(_state()) == (True, True)

    def test_excluding_restaurants_drops_both_meal_slots(self):
        # Dropping only one would still put a restaurant in a "viewpoints
        # only" day, which is the bug this pins.
        assert meal_slots(_state(exclude_categories=["restaurant"])) == (False, False)

    def test_excluding_something_else_leaves_meals_alone(self):
        assert meal_slots(_state(exclude_categories=["hotel"])) == (True, True)

    def test_viewpoints_only_excludes_both_stays_and_meals(self):
        state = _state(exclude_categories=["hotel", "restaurant"])
        assert meal_slots(state) == (False, False)
        assert category_excluded(state, "hotel")
        assert category_excluded(state, "restaurant")
        # ...but never the attractions the traveler actually asked for.
        assert not category_excluded(state, "attraction")


class TestSubjectMatterIsSeparate:
    def test_must_avoid_does_not_exclude_a_category(self):
        # "no hiking" must not remove every restaurant from the trip; the two
        # fields answer different questions and are kept apart deliberately.
        state = _state(must_avoid=["hike"])
        assert meal_slots(state) == (True, True)
        assert not category_excluded(state, "restaurant")


class TestPlanningContextCarriesIt:
    def test_the_fallback_planner_receives_the_exclusion(self):
        # build_plan_core is pure and takes plain data, so the exclusion has to
        # ride on the context rather than be read from TripState inside it.
        from datetime import date

        ctx = PlanningContext(
            destination_name="Galle",
            district_id=None,
            duration_days=1,
            start_date=date(2026, 9, 30),
            budget=None,
            travelers=1,
            travel_style=None,
            exclude_categories=["restaurant"],
        )
        assert "restaurant" in ctx.exclude_categories

    def test_it_defaults_to_no_exclusions(self):
        from datetime import date

        ctx = PlanningContext(
            destination_name="Galle",
            district_id=None,
            duration_days=1,
            start_date=date(2026, 9, 30),
            budget=None,
            travelers=1,
            travel_style=None,
        )
        assert ctx.exclude_categories == []


class TestTheCheckThatCoversEveryPath:
    """The architectural point, as a test.

    Four separate places build a day: fallback.py, planner_shared's
    fill_missing_days, followup_replan.py, and the LLM's own build_day_plan
    tool in tools/registry.py. "give view points only" was fixed in three of
    them and shipped still broken, because the fourth never consulted the
    request.

    Enforcing a rule at each construction site scales with the number of sites
    and fails silently when one is missed. Asserting it on the FINISHED plan
    scales with the number of rules, catches every path including ones not
    written yet, and fails loudly - the repair loop then gets a named failure
    to fix rather than the user getting a wrong plan.
    """

    def _ctx(self, excluded):
        from app.core.output_validator import ValidationContext

        return ValidationContext(
            duration_days=1,
            valid_dates={"2026-09-30"},
            budget=None,
            destination={"lat": 6.03, "lon": 80.21},
            candidate_listing_ids=set(),
            excluded_categories=excluded,
        )

    def _plan(self, types):
        from app.models.schemas import ItineraryDay, ItineraryItem, PlannerOutput

        items = [
            ItineraryItem(
                time="10:00", end_time="11:00", type=t, name=f"{t}-1", lat=6.03, lon=80.21,
                est_cost=0.0, currency="LKR", listing_id=None,
            )
            for t in types
        ]
        return PlannerOutput(
            itinerary=[ItineraryDay(day=1, date="2026-09-30", items=items, day_cost=0.0)],
            estimated_cost=0.0,
            currency="LKR",
        )

    def test_an_excluded_stop_is_caught_whatever_produced_it(self):
        from app.core.output_validator import _categories_respected

        failure = _categories_respected(
            self._plan(["attraction", "restaurant"]), self._ctx({"restaurant"}),
        )
        assert failure is not None
        assert "restaurant" in failure

    def test_a_compliant_plan_passes(self):
        from app.core.output_validator import _categories_respected

        assert _categories_respected(
            self._plan(["attraction", "attraction"]), self._ctx({"restaurant", "hotel"}),
        ) is None

    def test_no_exclusion_means_nothing_to_check(self):
        from app.core.output_validator import _categories_respected

        assert _categories_respected(self._plan(["restaurant"]), self._ctx(set())) is None

    def test_the_check_is_registered_so_it_actually_runs(self):
        # A check that exists but is not in the registry is worse than none:
        # it reads as covered and enforces nothing.
        from app.core.output_validator import _L2_RULES

        assert any(name == "categories_respected" for name, _ in _L2_RULES)
