# tests/conftest.py
# Shared fixtures for the whole test suite.

import pytest

import app.tools.calendar_tool as calendar_tool
from app.utils.cache import _store as _cache_store


@pytest.fixture(autouse=True)
def _clear_shared_cache():
    """The weather/disaster in-process cache is a module-level dict, so
    without this, a cache hit in one test could leak into an unrelated
    test and hide a bug (or cause a flaky pass/fail depending on test
    order)."""
    _cache_store.clear()
    yield
    _cache_store.clear()


@pytest.fixture(autouse=True)
def _isolate_calendar_token_store(tmp_path, monkeypatch):
    """Redirects calendar_tool's persistent token file to a throwaway path
    for every test, so tests never read/write the real calendar_tokens.json
    and never leak state between tests."""
    monkeypatch.setattr(calendar_tool, "_CREDENTIAL_STORE_PATH", tmp_path / "calendar_tokens.json")
