"""
Slot-filling extraction prompt - live-wired in app/utils/slot_filling.py.
Moved here from that file's inline `_SYSTEM_PROMPT` (project concern #5) -
that inline string is exactly the case the prompt-lint test
(tests/test_prompts_centralized.py) exists to catch.
"""
from app.models.schemas import ExtractedSlots
from app.prompts._base import PromptSpec

SLOT_FILLING_SYSTEM_PROMPT = """You extract structured trip-planning details from a
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
interests/must_avoid) - do not use a "reasonable default." A missing value
is the correct output when the user didn't say anything about that field.

When listing interests or must_avoid items, use short singular lowercase
tags (e.g. "beach", "hike", "culture") - not plurals or full phrases.

If the traveler mentions where they are starting/departing from - their
origin, separate from their destination - extract it as origin_location.
Do not confuse origin with destination; if only a destination is
mentioned, leave origin_location null.

If the traveler mentions things to avoid (health limits, dislikes, hard
constraints - e.g. "no hiking, my knees are bad", "not a fan of crowds"),
extract them as must_avoid tags in the same style as interests. Do not
infer must_avoid from a lack of enthusiasm - only from an explicit
exclusion.

If the traveler indicates how busy they want each day to be ("relaxed",
"take it easy", "want to see as much as possible", "packed schedule"),
extract it as pace: relaxed, balanced, or packed. Leave pace null if
nothing was said about it - do not default to "balanced"."""

SLOT_FILLING_SPEC = PromptSpec(
    name="slot_filling",
    version="1.1.0",   # bumped from the original inline prompt: added must_avoid + pace fields
    system=SLOT_FILLING_SYSTEM_PROMPT,
    output_schema=ExtractedSlots,
)
