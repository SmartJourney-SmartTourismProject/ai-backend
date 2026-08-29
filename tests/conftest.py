# tests/conftest.py
# Shared fixtures for the whole test suite.

import pytest

import app.tools.calendar_tool as calendar_tool
import app.tools.weather_tool as weather_tool
import app.tools.disaster_tool as disaster_tool
import app.utils.session_store as session_store


class _FakeCache:
    """In-memory stand-in for app.utils.cache's real (Redis-backed)
    cache_get/cache_set. The real implementation fails open (always a
    cache miss) when Redis isn't running, which it isn't in this test
    environment - so without this fake, every "does caching work" test
    would see zero real caching and fail. This fake actually stores
    values, so weather_tool/disaster_tool's own caching logic can still
    be verified without needing a real Redis server."""

    def __init__(self):
        self.store: dict = {}

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ttl_seconds):
        self.store[key] = value


@pytest.fixture(autouse=True)
def _fake_tool_cache(monkeypatch):
    """Replaces the cache_get/cache_set names already imported into
    weather_tool.py and disaster_tool.py with a fresh fake for every test,
    so cached values never leak between tests."""
    fake = _FakeCache()
    monkeypatch.setattr(weather_tool, "cache_get", fake.get)
    monkeypatch.setattr(weather_tool, "cache_set", fake.set)
    monkeypatch.setattr(disaster_tool, "cache_get", fake.get)
    monkeypatch.setattr(disaster_tool, "cache_set", fake.set)
    return fake


@pytest.fixture(autouse=True)
def _isolate_calendar_token_store(tmp_path, monkeypatch):
    """Redirects calendar_tool's persistent token file to a throwaway path
    for every test, so tests never read/write the real calendar_tokens.json
    and never leak state between tests."""
    monkeypatch.setattr(calendar_tool, "_CREDENTIAL_STORE_PATH", tmp_path / "calendar_tokens.json")


@pytest.fixture(autouse=True)
def _isolate_session_store(tmp_path, monkeypatch):
    """Same isolation as above, for the multi-turn conversation session store."""
    monkeypatch.setattr(session_store, "_SESSION_STORE_PATH", tmp_path / "trip_sessions.json")
