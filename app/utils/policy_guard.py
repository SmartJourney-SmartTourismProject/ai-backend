# app/utils/policy_guard.py
"""
Policy Agent (SAD §8.2 package diagram): rule-based check that blocks
clearly illegal/harmful requests before any paid LLM call runs.

Deliberate design choice, not a placeholder: a substring blocklist over
specific phrases (not bare words) is inherently bypassable by paraphrasing,
misspelling, or translation - it stops the obvious case, not a determined
bad actor. That trade-off is accepted for this capstone's scope; a
production-grade replacement would be an LLM-based content classifier.
Entries are deliberately whole phrases rather than single words to avoid
over-blocking real travel queries - "gun" alone would block "gun museum",
"ivory" alone would block "Ivory Coast" (a real destination). Keep new
entries specific enough to survive that test before adding them.
"""
from app.core.state import TripState

# Grouped by category for maintainability — expand within a category
# deliberately, and watch for over-blocking travel-relevant words
# (e.g. "gun" could appear in "gun museum", "knife" in "knife-making
# workshop" — keep entries specific enough to avoid common false hits).
_BLOCKED_KEYWORDS = [
    # weapons / explosives
    "bomb", "explosive", "detonator", "grenade", "assault rifle",
    "buy a gun", "ammunition",

    # trafficking / smuggling
    "drug trafficking", "human trafficking", "smuggle", "smuggling",
    "illegal border crossing", "fake passport", "forged visa",
    "counterfeit currency",

    # violence / harm
    "kill someone", "hire a hitman", "murder for hire", "acid attack",

    # illegal wildlife / goods trade (relevant for a tourism assistant) -
    # "ivory"/"poach" alone would false-positive on real destinations and
    # legitimate wildlife-viewing queries (e.g. "Ivory Coast", "poaching
    # prevention safari"), so these stay as specific phrases.
    "poach an animal", "ivory trade", "ivory market", "buy ivory",
    "sell ivory", "endangered species smuggling",

    # exploitation
    "child exploitation", "sex trafficking", "forced labor",

    # scams / fraud targeting the assistant itself
    "money laundering", "bribe an official",
]


def check_policy(state: TripState) -> TripState:
    """
    Rule-based check on state.user_input for disallowed content.
    Appends to state.errors and returns the same state so it composes
    cleanly as a LangGraph node, same pattern as validate_trip_state.
    """
    text = state.user_input.lower()

    for keyword in _BLOCKED_KEYWORDS:
        if keyword in text:
            state.errors.append(f"request blocked by policy: contains disallowed term '{keyword}'")
            break

    return state