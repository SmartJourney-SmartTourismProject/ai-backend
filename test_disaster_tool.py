# test_disaster_tool.py
# Run from your project root: python3 test_disaster_tool.py
# Makes REAL calls to EONET, USGS, and GDACS (all free, no API key needed).

import asyncio
from app.tools.disaster_tool import get_disaster_info, _distance_km


async def main():
    # Kandy, Sri Lanka coordinates
    lat, lon = 7.2906, 80.6337

    result = await get_disaster_info(lat, lon)

    print("Result:", result)

    checks = []
    checks.append(("Returns a dict (not None)", result is not None))
    checks.append(("Has 'safe' key (bool)", isinstance(result.get("safe"), bool)))
    checks.append(("Has 'active_events' key (list)", isinstance(result.get("active_events"), list)))

    for event in result.get("active_events", []):
        checks.append((
            f"event has required keys: {event.get('title')}",
            all(k in event for k in ("type", "severity", "title", "source", "distance_km"))
        ))
        checks.append((
            f"event severity is valid: {event.get('title')}",
            event.get("severity") in ("red", "orange", "green")
        ))
        checks.append((
            f"event is within requested radius: {event.get('title')}",
            event.get("distance_km") is None or event["distance_km"] <= 300
        ))

    # Sanity check on the haversine helper itself - Kandy to Colombo is
    # roughly 90-100km as the crow flies.
    kandy_colombo_km = _distance_km(7.2906, 80.6337, 6.9271, 79.8612)
    checks.append((
        f"_distance_km sanity check (Kandy-Colombo ~90-100km, got {kandy_colombo_km:.1f})",
        90 <= kandy_colombo_km <= 100
    ))

    passed = 0
    for label, ok in checks:
        if ok:
            passed += 1
        print(f"[{'PASS' if ok else 'FAIL'}] {label}")

    print(f"\n{passed}/{len(checks)} checks passed")

    # Also check the all-sources-fail fallback shape at a nonsense location,
    # just to see the "note" field appear if every source legitimately
    # returns nothing nearby (not a network failure, just no events).
    far_result = await get_disaster_info(0.0, 0.0, radius_km=10)
    print("\nFar-away location result (expect safe=True, likely empty):", far_result)


if __name__ == "__main__":
    asyncio.run(main())
