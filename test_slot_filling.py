# test_slot_filling.py
# Run from your project root: python3 test_slot_filling.py
# Makes REAL calls to Gemini using your settings.gemini_api_key.

import asyncio
from app.core.state import TripState
from app.utils.slot_filling import fill_slots


async def run_case(label: str, user_input: str, expect_fields: dict):
    state = TripState(user_input=user_input)
    result = await fill_slots(state)

    print(f"\n--- {label} ---")
    print("Input:", user_input)
    print("destination:", result.destination)
    print("duration_days:", result.duration_days)
    print("budget:", result.budget)
    print("travelers:", result.travelers)
    print("interests:", result.interests)
    print("errors:", result.errors)

    ok = True
    for field, expected in expect_fields.items():
        actual = getattr(result, field)
        if expected == "NOT_NONE":
            if actual is None or actual == []:
                ok = False
                print(f"  [FAIL] {field} expected non-empty, got {actual!r}")
        elif expected == "NONE":
            if actual not in (None, []):
                ok = False
                print(f"  [FAIL] {field} expected None/empty, got {actual!r}")
        else:
            if actual != expected:
                ok = False
                print(f"  [FAIL] {field} expected {expected!r}, got {actual!r}")

    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    return ok


async def main():
    cases = [
        ("Full details", "I want to take my wife and kid to Kandy for a week, we love nature and food, budget around $500",
         {"destination": "Kandy", "duration_days": 7, "travelers": 3, "budget": 500.0, "interests": "NOT_NONE"}),

        ("Destination only", "I want to visit Galle",
         {"destination": "Galle", "duration_days": "NONE", "travelers": "NONE", "budget": "NONE"}),

        ("No details at all", "Plan me a trip somewhere nice",
         {"destination": "NONE"}),

        ("Solo traveler phrasing", "Just me, heading to Ella for 3 days, love hiking",
         {"destination": "Ella", "duration_days": 3, "travelers": 1, "interests": "NOT_NONE"}),
    ]

    passed = 0
    for label, text, expected in cases:
        if await run_case(label, text, expected):
            passed += 1

    print(f"\n{passed}/{len(cases)} cases behaved as expected")


if __name__ == "__main__":
    asyncio.run(main())