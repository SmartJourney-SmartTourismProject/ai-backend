# app/utils/cache.py
import time

# Simple in-process TTL cache. Not shared across multiple server instances
# and resets on restart - fine for now since weather/disaster tolerate a
# cache miss (they just re-fetch), unlike the calendar tokens in Step 7
# which needed real persistence. Swap for Redis later if this needs to be
# shared across processes (see BUILD_PLAN.md §8).
_store: dict[str, tuple[float, dict]] = {}

def cache_get(key: str) -> dict | None:
    entry = _store.get(key)
    if not entry:
        return None
    expires_at, value = entry
    if time.time() > expires_at:
        del _store[key]
        return None
    return value

def cache_set(key: str, value: dict, ttl_seconds: int) -> None:
    _store[key] = (time.time() + ttl_seconds, value)
