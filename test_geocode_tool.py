# test_geocode_tool.py
# Run from your project root: python3 test_geocode_tool.py
# Makes REAL calls to Nominatim (OpenStreetMap) - free, no API key needed.

import asyncio
from app.tools.geocode_tool import geocode_destination


async def main():
    cases = [
        ("Kandy", True),
        ("Galle", True),
        ("Ella", True),
        ("Colombo", True),
        ("Nuwara Eliya", True),
        ("Xyzzyplonkqqq123", False),  # nonsense destination - should fail gracefully
    ]

    checks = []
    for destination, expect_found in cases:
        result = await geocode_destination(destination)
        print(f"{destination!r} -> {result}")

        found = result is not None
        checks.append((f"{destination}: found={found} (expected {expect_found})", found == expect_found))

        if result:
            checks.append((
                f"{destination}: lat/lon look like Sri Lanka (roughly 5-10 lat, 79-82 lon)",
                5.0 <= result["lat"] <= 10.5 and 79.0 <= result["lon"] <= 82.5
            ))

        # Nominatim's usage policy asks for max 1 request/second from a client.
        await asyncio.sleep(1)

    passed = 0
    for label, ok in checks:
        if ok:
            passed += 1
        print(f"[{'PASS' if ok else 'FAIL'}] {label}")

    print(f"\n{passed}/{len(checks)} checks passed")


if __name__ == "__main__":
    asyncio.run(main())
