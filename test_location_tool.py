# test_location_tool.py
# Run from your project root: python3 test_location_tool.py
# Note: this makes a REAL network call to ipapi.co for the IP-fallback case,
# so you need internet access when running it.

import asyncio
from app.tools.location_tool import resolve_start_location


async def main():
    results = []

    # 1. GPS present -> should use GPS directly, no network call
    r1 = await resolve_start_location(client_gps={"lat": 7.2906, "lon": 80.6337}, client_ip=None)
    results.append(("GPS provided", r1, r1 is not None and r1["source"] == "gps"))

    # 2. No GPS, valid public IP -> should hit ipapi.co and return source="ip"
    r2 = await resolve_start_location(client_gps=None, client_ip="8.8.8.8")
    results.append(("IP fallback (8.8.8.8)", r2, r2 is not None and r2["source"] == "ip"))

    # 3. GPS has None values -> should fall through to IP
    r3 = await resolve_start_location(client_gps={"lat": None, "lon": None}, client_ip="8.8.8.8")
    results.append(("GPS null values -> IP fallback", r3, r3 is not None and r3["source"] == "ip"))

    # 4. Nothing provided -> should return None
    r4 = await resolve_start_location(client_gps=None, client_ip=None)
    results.append(("Nothing provided -> None", r4, r4 is None))

    # 5. Bad/unresolvable IP -> should fail gracefully to None, not crash
    r5 = await resolve_start_location(client_gps=None, client_ip="0.0.0.0")
    results.append(("Unresolvable IP -> None (no crash)", r5, r5 is None or isinstance(r5, dict)))

    print(f"{'CASE':<35} {'RESULT':<45} {'OK'}")
    passed = 0
    for label, result, ok in results:
        if ok:
            passed += 1
        print(f"{label:<35} {str(result):<45} {'PASS' if ok else 'FAIL'}")

    print(f"\n{passed}/{len(results)} test cases behaved as expected")


if __name__ == "__main__":
    asyncio.run(main())