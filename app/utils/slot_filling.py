# app/utils/slot_filling.py
from typing import List, Optional

from pydantic import BaseModel, Field
from langchain_google_genai import ChatGoogleGenerativeAI

from app.config.settings import settings
from app.core.state import TripState
from app.tools import db_tool



class _ExtractedSlots(BaseModel):
    """
    Schema for what we ask the LLM to extract. Every field is Optional —
    the model must leave anything not mentioned in user_input as null,
    never guess. Downstream defaulting logic (user profile lookup, asking
    the user, etc.) is a separate step's job, not this one's.
    """
    destination: Optional[str] = Field(
        None, description="The travel destination, if mentioned. Null if not mentioned."
    )
    duration_days: Optional[int] = Field(
        None, description="Trip length in days, if mentioned or inferable from phrases like 'a week' (=7). Null if not mentioned."
    )
    budget: Optional[float] = Field(
        None, description="Total trip budget as a number, if mentioned. Null if not mentioned."
    )
    travelers: Optional[int] = Field(
        None, description="Total number of travelers including the user, if mentioned or inferable (e.g. 'my wife and kid' = 3). Null if not mentioned."
    )
    interests: List[str] = Field(
        default_factory=list,
        description=(
            "List of travel interests/activity types mentioned, as short, "
            "singular, lowercase tags (e.g. 'beach' not 'beaches', 'hike' not "
            "'hiking trips'). Empty list if none mentioned."
        ),
    )


_SYSTEM_PROMPT = """You extract structured trip-planning details from a
traveler's message. Only extract what is explicitly stated or clearly
inferable from specific wording (e.g. "a week" -> 7 days, "my wife and
kid" -> 3 travelers).

Do NOT fill in a field just because a trip is being discussed. In
particular:
- Do not default travelers to 1 just because the message is about a
  trip. Only set travelers when the message actually indicates who is
  going (e.g. "just me", "solo", "my family", a specific count).
- Do not guess a duration, budget, or destination that isn't stated or
  clearly implied.

If a field is not mentioned, leave it null (or an empty list for
interests) — do not use a "reasonable default." A missing value is the
correct output when the user didn't say anything about that field.

When listing interests, use short singular lowercase tags (e.g. "beach",
"hike", "culture") - not plurals or full phrases."""


async def fill_slots(state: TripState) -> TripState:
    """
    Uses Gemini to extract destination/duration_days/budget/travelers/interests
    from state.user_input.

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
        llm = ChatGoogleGenerativeAI(
            model=settings.llm_model,
            google_api_key=settings.gemini_api_key,
            temperature=0,
        )
        structured_llm = llm.with_structured_output(_ExtractedSlots)

        result: _ExtractedSlots = await structured_llm.ainvoke([
            ("system", _SYSTEM_PROMPT),
            ("human", state.user_input),
        ])

        if state.is_followup:
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

    except Exception as e:
        state.errors.append(f"slot_filling failed: {e}")

    if state.is_followup:
        return state

    if state.destination and state.duration_days is None:
        # Destination-only request: default to 1 day of activities + travel
        # time, per BUILD_PLAN.md §2, instead of leaving duration unset.
        state.duration_days = 1

    if state.user_id:
        profile = await db_tool.get_user_profile(state.user_id)
        if not state.interests and profile.get("interests"):
            state.interests = profile["interests"]
        if state.travel_style is None and profile.get("travel_style"):
            state.travel_style = profile["travel_style"]
        if state.budget is None and profile.get("budget"):
            state.budget = profile["budget"]

    if state.destination is None:
        state.clarification_needed = "Which destination would you like to visit?"

    return state
