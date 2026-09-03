# app/utils/slot_filling.py
from app.core.followup import classify_followup
from app.core.llm import get_llm
from app.core.state import TripState
from app.models.schemas import ExtractedSlots
from app.prompts import get_prompt
from app.tools import db_tool
from app.tools.db_tool import DataUnavailable
from app.tools.geocode_tool import geocode_destination
from app.tools.geo_tool import resolve_place

_PROMPT = get_prompt("slot_filling")   # app/prompts/slot_filling_prompt.py - the single source
                                        # of both the prompt text and ExtractedSlots' schema


async def fill_slots(state: TripState) -> TripState:
    """
    Uses Gemini to extract destination/duration_days/budget/travelers/
    interests/must_avoid/pace from state.user_input.

    Two modes:
    - First turn (state.is_followup is False): only fills fields the user
      hasn't already set elsewhere (e.g. via the API request). Missing
      destination triggers a clarification question rather than a guess.
    - Follow-up turn (state.is_followup is True - a session_id matched a
      prior turn, see app/utils/session_store.py): every field already has
      a carried-over value from last time, so extracted fields OVERWRITE
      instead of only filling gaps - "make it cheaper" should actually
      change state.budget, not be ignored because budget was already set.
      Defaulting/profile-lookup/clarification below only make sense for a
      first turn, so they're skipped entirely on a follow-up.

    On any LLM/parsing failure, state is returned unchanged (errors logged
    to state.errors) rather than raising.
    """
    try:
        structured_llm = get_llm("slots").with_structured_output(_PROMPT.output_schema)

        result: ExtractedSlots = await structured_llm.ainvoke([
            ("system", _PROMPT.system),
            ("human", state.user_input),
        ])

        if state.is_followup:
            # Classified from THIS turn's raw extraction, before the merge
            # below overwrites state - classify_followup needs to see what
            # the message itself asked to change, not the already-carried
            # values sitting on state.
            plan = classify_followup(state.user_input, result)
            state.followup_scope = plan.scope
            state.followup_target_days = plan.target_days
            state.followup_cheaper = plan.cheaper

            if result.destination:
                state.destination = result.destination
            if result.duration_days:
                state.duration_days = result.duration_days
            if result.budget:
                state.budget = result.budget
            if result.travelers:
                state.travelers = result.travelers
            if result.interests:
                state.interests = result.interests
            if result.must_avoid:
                state.must_avoid = result.must_avoid
            if result.pace:
                state.pace = result.pace
        else:
            if state.destination is None and result.destination:
                state.destination = result.destination
            if state.duration_days is None and result.duration_days:
                state.duration_days = result.duration_days
            if state.budget is None and result.budget:
                state.budget = result.budget
            if state.travelers is None and result.travelers:
                state.travelers = result.travelers
            if not state.interests and result.interests:
                state.interests = result.interests
            if not state.must_avoid and result.must_avoid:
                state.must_avoid = result.must_avoid
            if state.pace is None and result.pace:
                state.pace = result.pace

        # A named origin only matters when we don't already have a real
        # start_location - GPS/IP resolution (done by the API layer before
        # this ever runs) is more precise than geocoding a place name, so
        # it always takes priority. This applies on both first turns and
        # follow-ups: "I'm starting from Polonnaruwa" is exactly as useful
        # said on message 2 as on message 1, whenever GPS/IP failed.
        if state.start_location is None and result.origin_location:
            origin_coords = await geocode_destination(result.origin_location)
            if origin_coords:
                state.start_location = {
                    "lat": origin_coords["lat"],
                    "lon": origin_coords["lon"],
                    "source": "text",
                }

    except Exception as e:
        state.errors.append(f"slot_filling failed: {e}")

    # Country-scoping check (project decision, 2026-09-02): SmartJourney
    # covers Sri Lanka only. Runs for both first-turn and follow-up turns -
    # a follow-up can change the destination too ("actually let's go to
    # Paris instead"), so this must run before the is_followup early return
    # below, not after it. geo_tool.resolve_place() distinguishes a genuine
    # foreign destination from a real Sri Lankan place via a settlement-class
    # filter on a country-restricted Nominatim search (see geo_tool.py's
    # module docstring) - it does not just check whether geocoding succeeded.
    if state.destination:
        try:
            place = await resolve_place(state.destination)
        except Exception as e:
            place = None
            state.errors.append(f"destination country check failed: {e}")
        if place and place["confidence"] == "out_of_country":
            state.clarification_needed = (
                f"SmartJourney currently covers destinations within Sri Lanka only. "
                f"{state.destination} is in {place['country']} — is there a Sri Lankan "
                f"destination I can help you plan instead?"
            )
            return state

    if state.is_followup:
        return state

    if state.destination and state.duration_days is None:
        # Destination-only request: default to 1 day of activities + travel
        # time, per BUILD_PLAN.md §2, instead of leaving duration unset.
        state.duration_days = 1

    if state.user_id:
        # Profile lookup is advisory (fills gaps, never required to plan a
        # trip) - a DB outage here degrades gracefully with a soft note
        # (same "advisory, not fatal" pattern as orchestrator.py's
        # location_unresolved), not a hard failure of slot filling.
        try:
            profile = await db_tool.get_user_profile(state.user_id)
            if not state.interests and profile.get("interests"):
                state.interests = profile["interests"]
            if state.travel_style is None and profile.get("travel_style"):
                state.travel_style = profile["travel_style"]
            if state.budget is None and profile.get("budget"):
                state.budget = profile["budget"]
        except DataUnavailable as e:
            state.errors.append(f"profile_unavailable: {e}")

    if state.destination is None:
        state.clarification_needed = "Which destination would you like to visit?"

    return state
