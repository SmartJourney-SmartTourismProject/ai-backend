# test_orchestrator.py
# Run from your project root: python3 test_orchestrator.py
# Runs the full graph end-to-end. Makes REAL calls to Gemini (slot_fill)
# and OpenWeather (context) if location/calendar resolve successfully.
# No user_id is set in these cases, so calendar_tool short-circuits to []
# (no calendar connected) rather than needing real OAuth tokens.

import asyncio
from app.core.state import TripState
from app.core.orchestrator import orchestrator


async def run_case(label: str, user_input: str):
    state = TripState(user_input=user_input)

    print(f"\n{'=' * 60}")
    print(f"CASE: {label}")
    print(f"Input: {user_input!r}")
    print("=" * 60)

    try:
        result = await orchestrator.ainvoke(state)
    except Exception as e:
        print(f"[CRASH] Graph raised an exception: {type(e).__name__}: {e}")
        return False

    destination = result.get("destination")
    errors = result.get("errors")
    completed_steps = result.get("completed_steps")
    final_response = result.get("final_response")

    print("destination:", destination)
    print("completed_steps:", completed_steps)
    print("errors:", errors)
    print("final_response:", final_response)

    ok = final_response is not None and isinstance(completed_steps, list) and len(completed_steps) > 0
    print(f"[{'PASS' if ok else 'FAIL'}] graph completed without crashing and produced a final_response")
    return ok


async def main():
    cases = [
        ("Full details", "I want to take my wife and kid to Kandy for a week, we love nature and food, budget around $500"),
        ("Destination only", "I want to visit Galle"),
        ("No details at all", "Plan me a trip somewhere nice"),
    ]

    passed = 0
    for label, text in cases:
        if await run_case(label, text):
            passed += 1

    print(f"\n{'=' * 60}")
    print(f"{passed}/{len(cases)} cases completed without crashing")


if __name__ == "__main__":
    asyncio.run(main())