# test_cache.py
# Run from your project root: python3 test_cache.py
# Checks the in-process TTL cache (app/utils/cache.py) directly, then
# confirms get_weather actually uses it (second call for the same
# coordinates should be near-instant instead of hitting OpenWeather again).

import asyncio
import time

from app.utils.cache import cache_get, cache_set
from app.tools.weather_tool import get_weather


def test_cache_primitives():
    checks = []

    checks.append(("Miss on an unset key returns None", cache_get("nope") is None))

    cache_set("greeting", {"msg": "hello"}, ttl_seconds=5)
    checks.append(("Hit right after set returns the same value", cache_get("greeting") == {"msg": "hello"}))

    cache_set("short_lived", {"msg": "bye"}, ttl_seconds=1)
    time.sleep(1.5)
    checks.append(("Entry is gone after its TTL expires", cache_get("short_lived") is None))

    passed = 0
    for label, ok in checks:
        if ok:
            passed += 1
        print(f"[{'PASS' if ok else 'FAIL'}] {label}")
    print(f"{passed}/{len(checks)} cache primitive checks passed\n")


async def test_weather_uses_cache():
    lat, lon = 7.2906, 80.6337
    dates = ["2026-08-29"]

    t0 = time.time()
    r1 = await get_weather(lat, lon, dates)
    t1 = time.time()
    print(f"First call took {t1 - t0:.3f}s, got result: {r1 is not None}")

    t2 = time.time()
    r2 = await get_weather(lat, lon, dates)
    t3 = time.time()
    print(f"Second call took {t3 - t2:.3f}s (should be much faster - cache hit)")

    checks = []
    checks.append(("First call succeeded (got a result)", r1 is not None))
    checks.append(("Second call returned the identical cached object", r1 == r2))
    checks.append((
        "Second call was meaningfully faster than the first (cache hit)",
        r1 is None or (t3 - t2) < (t1 - t0) / 2
    ))

    passed = 0
    for label, ok in checks:
        if ok:
            passed += 1
        print(f"[{'PASS' if ok else 'FAIL'}] {label}")
    print(f"{passed}/{len(checks)} weather-cache checks passed")


if __name__ == "__main__":
    test_cache_primitives()
    asyncio.run(test_weather_uses_cache())
