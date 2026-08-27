############THIS IS NOT FINALIZE YET BEACUSE THERE CAN BE LOOPHOLES IN THE POLICY GUARD. THIS IS JUST A TESTING FILE FOR NOW.############

# app/utils/policy_guard.py
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

    # illegal wildlife / goods trade (relevant for a tourism assistant)
    "poach", "ivory trade", "endangered species smuggling",

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