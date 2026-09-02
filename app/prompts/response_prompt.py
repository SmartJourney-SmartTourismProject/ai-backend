"""
Response narration prompt - Phase 6 target. Off by default
(ENABLE_RESPONSE_NARRATION=false, decision D6c) - the template fallback is
free, deterministic, and always available even when the LLM isn't, so
narration is the one call in the system that's opt-in rather than the
default path. No output_schema: this call produces plain chat text, not a
structured object - the validated plan itself is never regenerated here,
only described.
"""
from app.prompts._base import PromptSpec

RESPONSE_SYSTEM_PROMPT = """You write a short, warm chat message summarizing a trip plan
that has ALREADY been built and validated. You do not change any fact in it.

RULES
1.  Summarize in 2-4 sentences: the destination, number of days, and a highlight or two
    from the itinerary. Mention estimated_cost and budget_notes if present.
2.  Never state a number (cost, distance, day count) that isn't already in the plan data
    you were given - you are describing it, not recalculating it.
3.  If budget_notes says the plan is over budget, say so plainly and warmly - do not
    hide or soften a real budget shortfall.
4.  Keep it conversational, not a bulleted list - the itinerary itself already has the
    structured detail; this message is what appears in the chat bubble above it.
5.  Return only the message text. No JSON, no markdown formatting.
"""

RESPONSE_SPEC = PromptSpec(
    name="response",
    version="1.0.0",
    system=RESPONSE_SYSTEM_PROMPT,
    output_schema=None,
)


def template_final_response(destination: str, duration_days: int, estimated_cost: float,
                            currency: str, budget_notes: str | None) -> str:
    """The deterministic, free, always-available fallback this prompt exists
    alongside - used whenever ENABLE_RESPONSE_NARRATION is false or the LLM
    call itself fails. Matches app/core/fallback.py's own template exactly,
    so narration on/off never changes the plan's substance, only its prose."""
    text = (
        f"Here's your trip plan for {destination}: {duration_days} day(s) planned, "
        f"estimated cost {estimated_cost:,.0f} {currency}."
    )
    if budget_notes:
        text += f"\n\nBudget note: {budget_notes}"
    return text
