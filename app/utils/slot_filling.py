"""
LLM-assisted slot-filling: extracts destination/duration/budget/travelers/
interests from a user's free-text message.

The frontend chat UI (ChatPanel.tsx) only ever sends `user_input` as free
text - it has no destination/duration/budget form fields - so without this
step, `state.destination` stays None for every real chat message and the
whole pipeline fails at the weather/disaster stage with "No destination
provided." Planned in BUILD_PLAN.md §5 but not implemented until now.

Only fills fields that are still unset - never overwrites values already
provided explicitly on the request (e.g. from a structured form).
"""
from __future__ import annotations
import json
import logging
from typing import Any, Optional

from langchain_google_genai import ChatGoogleGenerativeAI

from app.config.settings import settings
from app.core.state import TripState
from app.data.sri_lanka_districts import DISTRICT_NAMES

logger = logging.getLogger(__name__)

_SLOT_FILLING_PROMPT = """Extract trip-planning details from the traveler's message below.

Valid destinations are ONLY these Sri Lanka districts (match the closest one
mentioned, e.g. "Ella" or "Nine Arches Bridge" -> "Badulla"; "Mirissa" -> "Matara"):
{districts}

Return ONLY a JSON object, no markdown, no explanation, with exactly these keys:
{{
  "destination": "<one of the districts above, or null if none mentioned/unclear>",
  "duration_days": <integer number of days, or null if not mentioned>,
  "budget": <number (USD), or null if not mentioned>,
  "travelers": <integer number of people, or null if not mentioned>,
  "interests": [<short lowercase interest tags mentioned, e.g. "culture", "beach", "hiking">]
}}

Never guess a value that isn't actually stated or clearly implied - leave it null.

Traveler's message: "{user_input}"
"""


def _extract_text(content: Any) -> str:
    """
    Newer Gemini models return `response.content` as a list of content
    blocks (e.g. [{"type": "text", "text": "...", "extras": {...}}]) rather
    than a plain string - concatenate just the text blocks.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            block.get("text", "") for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return str(content)


async def extract_trip_slots(user_input: str) -> dict[str, Any]:
    """
    Calls the LLM to extract trip slots from free text. Returns a dict with
    keys destination/duration_days/budget/travelers/interests, all None (or
    an empty list for interests) on any failure - never raises, since this
    is a best-effort enrichment step, not a hard requirement.
    """
    empty: dict[str, Any] = {
        "destination": None, "duration_days": None,
        "budget": None, "travelers": None, "interests": [],
    }

    try:
        llm = ChatGoogleGenerativeAI(model=settings.llm_model, temperature=0)
        prompt = _SLOT_FILLING_PROMPT.format(
            districts=", ".join(DISTRICT_NAMES),
            user_input=user_input,
        )
        response = await llm.ainvoke(prompt)
        text = _extract_text(response.content)
        text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        data = json.loads(text)
    except Exception as e:
        logger.warning(f"Slot-filling extraction failed, leaving fields unset: {e}")
        return empty

    return {
        "destination": data.get("destination") or None,
        "duration_days": data.get("duration_days") or None,
        "budget": data.get("budget") or None,
        "travelers": data.get("travelers") or None,
        "interests": data.get("interests") or [],
    }


async def fill_missing_slots(state: TripState) -> None:
    """Mutates `state` in place, filling only fields that are still unset."""
    # interests is intentionally excluded from this check: an empty list is
    # a normal, complete state (not every trip has stated interests), not
    # evidence that slot-filling still has work to do - requiring it here
    # would force an LLM call on every structured-field request that simply
    # didn't mention interests.
    if state.destination and state.duration_days and state.budget and state.travelers:
        return  # core fields already provided explicitly - skip the LLM call

    slots = await extract_trip_slots(state.user_input)

    if not state.destination and slots["destination"]:
        state.destination = slots["destination"]
    if not state.duration_days and slots["duration_days"]:
        state.duration_days = slots["duration_days"]
    if not state.budget and slots["budget"]:
        state.budget = slots["budget"]
    if not state.travelers and slots["travelers"]:
        state.travelers = slots["travelers"]
    if not state.interests and slots["interests"]:
        state.interests = slots["interests"]
