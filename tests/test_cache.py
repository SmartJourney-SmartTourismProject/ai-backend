# tests/test_cache.py
# app/utils/cache.py is a thin async Redis wrapper that fails open (treats
# any Redis error, or no REDIS_URL, as a cache miss rather than raising).
# No real Redis server is expected to be running in this test environment,
# so these tests exercise exactly that fail-open path - see conftest.py's
# _fake_tool_cache fixture for how weather_tool/disaster_tool's own
# caching logic is tested with a real (fake) working cache instead.

from app.utils.cache import cache_get, cache_set


async def test_get_fails_open_when_redis_unavailable():
    result = await cache_get("some-key-nobody-set")
    assert result is None


async def test_set_does_not_raise_when_redis_unavailable():
    # Should degrade silently (log a warning internally) rather than
    # propagate a connection error up to the caller.
    await cache_set("some-key", {"a": 1}, ttl_seconds=60)


async def test_get_returns_none_when_redis_url_is_empty(monkeypatch):
    from app.config.settings import settings
    monkeypatch.setattr(settings, "redis_url", "")

    # Force a fresh client lookup so the empty redis_url actually takes
    # effect (the module caches its client instance after first use).
    import app.utils.cache as cache_module
    monkeypatch.setattr(cache_module, "_client", None)
    monkeypatch.setattr(cache_module, "_connection_failed", False)

    assert await cache_get("x") is None
