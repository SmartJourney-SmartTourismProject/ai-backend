# tests/conftest.py
# Shared fixtures for the whole test suite.

import pytest

import app.tools.calendar_tool as calendar_tool
import app.tools.weather_tool as weather_tool
import app.tools.disaster_tool as disaster_tool
import app.utils.session_store as session_store
from app.config.settings import settings

# Keys that gate a code path BEFORE the mocked call it protects. Both of these
# short-circuit on an empty value - llm.py drops Groq from the provider chain
# (`return bool(settings.groq_api_key)`), and routing_tool.py returns a
# haversine estimate without issuing any HTTP request at all - so a suite run
# on a machine whose .env lacks them fails tests that respx has correctly
# mocked, for reasons that have nothing to do with the code under test.
#
# CI already supplies exactly these as `ci-placeholder` env vars
# (.github/workflows/ci.yml), which is why those runs are green while a local
# run is not. Setting them here makes the suite self-contained instead of
# depending on whichever keys a given developer happens to hold.
#
# Never a real key, and never a real call: every test that reaches a provider
# mocks the transport. A test that specifically needs a key ABSENT monkeypatches
# it to "" itself (see test_no_ors_key_falls_back_to_haversine_without_a_call),
# and that runs after this fixture, so it still wins.
_PLACEHOLDER_KEYS = {
    "gemini_api_key": "test-placeholder",
    "groq_api_key": "test-placeholder",
    "ors_api_key": "test-placeholder",
}


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
def _placeholder_api_keys(monkeypatch):
    """Gives every test the same key-gated code paths CI gets, regardless of
    what is in the developer's .env."""
    for field, value in _PLACEHOLDER_KEYS.items():
        if not getattr(settings, field, ""):
            monkeypatch.setattr(settings, field, value)


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


class _FakeSessionPool:
    """Stands in for the asyncpg pool session_store.py now uses (Phase 7 -
    it used to be a JSON file, isolated the same way calendar_tool's token
    store still is above). Session id -> row dict; ignores expires_at
    entirely, since no test here exercises TTL expiry - that's
    app/scheduler.py's session_gc job's own concern, not this module's."""

    def __init__(self):
        self.rows: dict[str, dict] = {}

    async def fetchrow(self, sql, *args):
        session_id = args[0]
        row = self.rows.get(session_id)
        return {"state": row["state"]} if row else None

    async def execute(self, sql, *args):
        session_id, user_id, state_json, react_trace_json = args
        self.rows[session_id] = {"user_id": user_id, "state": state_json, "react_trace": react_trace_json}
        return "INSERT 0 1"


@pytest.fixture(autouse=True)
def _isolate_session_store(monkeypatch):
    """Same isolation intent as _isolate_calendar_token_store above, for the
    multi-turn conversation session store - now DB-backed (ai_session), so
    a fake pool takes the place of the old throwaway JSON file path."""
    fake_pool = _FakeSessionPool()

    async def _fake_get_pool():
        return fake_pool

    monkeypatch.setattr(session_store, "get_pool", _fake_get_pool)
    return fake_pool
