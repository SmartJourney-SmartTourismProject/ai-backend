# app/utils/slot_filling.py
from typing import List, Optional

from pydantic import BaseModel, Field
from langchain_google_genai import ChatGoogleGenerativeAI

from app.config.settings import settings
from app.core.state import TripState


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
        default_factory=list, description="List of travel interests/activity types mentioned (e.g. 'nature', 'food', 'history'). Empty list if none mentioned."
    )


_SYSTEM_PROMPT = """You extract structured trip-planning details from a
traveler's message. Only extract what is explicitly stated or clearly
inferable (e.g. "a week" -> 7 days, "my wife and kid" -> 3 travelers).
Never invent or assume a value that isn't grounded in the text. Leave a
field null/empty if it isn't mentioned."""


async def fill_slots(state: TripState) -> TripState:
    """
    Uses Gemini to extract destination/duration_days/budget/travelers/interests
    from state.user_input, filling only the fields the user actually
    mentioned. Fields already set on state are not overwritten. On any
    LLM/parsing failure, state is returned unchanged (errors logged to
    state.errors) rather than raising.
    """
    try:
        llm = ChatGoogleGenerativeAI(
            model="gemini-3.6-flash",
            google_api_key=settings.gemini_api_key,
            temperature=0,
        )
        structured_llm = llm.with_structured_output(_ExtractedSlots)

        result: _ExtractedSlots = await structured_llm.ainvoke([
            ("system", _SYSTEM_PROMPT),
            ("human", state.user_input),
        ])

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

    return state