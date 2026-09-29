# tests/test_calendar_token_store.py
# Checks the persistent credential store from calendar_tool.py: the
# `google_oauth_tokens` table when a database is configured, falling back to
# a local JSON file otherwise. Uses the isolated temp path from conftest.py's
# autouse fixture, so this never touches the real calendar_tokens.json.

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import app.tools.calendar_tool as calendar_tool


async def test_persists_to_disk_and_rereads_fresh_each_time():
    await calendar_tool.save_credentials("demo-user-1", {"access_token": "a", "refresh_token": "b"})

    # Read the raw file directly - proves it's actually on disk, not just
    # held in a Python variable that would vanish if the process restarted.
    raw = json.loads(calendar_tool._CREDENTIAL_STORE_PATH.read_text())
    assert raw["demo-user-1"] == {"access_token": "a", "refresh_token": "b"}

    # get_stored_credentials re-reads from disk on every call (no in-memory
    # cache in this module), so this is equivalent to what a fresh process
    # restart would see.
    creds = await calendar_tool.get_stored_credentials("demo-user-1")
    assert creds == {"access_token": "a", "refresh_token": "b"}


async def test_unknown_user_returns_none():
    result = await calendar_tool.get_stored_credentials("nobody-connected-yet")
    assert result is None


async def test_second_user_does_not_clobber_first():
    await calendar_tool.save_credentials("demo-user-1", {"access_token": "a", "refresh_token": None})
    await calendar_tool.save_credentials("demo-user-2", {"access_token": "z", "refresh_token": None})

    still_there = await calendar_tool.get_stored_credentials("demo-user-1")
    assert still_there == {"access_token": "a", "refresh_token": None}


async def test_corrupted_file_does_not_crash():
    calendar_tool._CREDENTIAL_STORE_PATH.write_text("not valid json{{{")
    result = await calendar_tool.get_stored_credentials("anyone")
    assert result is None


class _FakePool:
    """Stands in for an asyncpg pool. `rows` is what fetchrow returns; every
    statement and its arguments are recorded so save tests can assert on the
    values actually bound."""

    def __init__(self, rows=None):
        self._rows = rows if rows is not None else []
        self.calls: list[tuple] = []

    async def fetchrow(self, sql, *args):
        self.calls.append((sql, args))
        return self._rows[0] if self._rows else None

    async def execute(self, sql, *args):
        self.calls.append((sql, args))


def _patch_pool(monkeypatch, rows=None) -> _FakePool:
    pool = _FakePool(rows)
    monkeypatch.setattr(calendar_tool, "get_pool", AsyncMock(return_value=pool))
    return pool


async def test_get_stored_credentials_reads_from_database(monkeypatch):
    _patch_pool(monkeypatch, [{
        "access_token": "real-token", "refresh_token": "real-refresh",
        "token_expiry": None, "scope": None,
    }])

    creds = await calendar_tool.get_stored_credentials("user-1")

    assert creds == {
        "access_token": "real-token", "refresh_token": "real-refresh",
        "token_expiry": None, "scope": None,
    }


async def test_unknown_user_in_database_returns_none_without_local_fallback(monkeypatch):
    # A reachable database saying "no row" is authoritative - it must NOT
    # fall through to the local file, or a disconnected calendar could be
    # resurrected from a stale token on disk.
    calendar_tool._save_local_store({"nobody": {"access_token": "stale"}})
    _patch_pool(monkeypatch, [])

    creds = await calendar_tool.get_stored_credentials("nobody")

    assert creds is None


async def test_save_credentials_writes_to_database(monkeypatch):
    pool = _patch_pool(monkeypatch)

    await calendar_tool.save_credentials("user-1", {"access_token": "a", "refresh_token": "b"})

    sql, args = pool.calls[0]
    assert "INSERT INTO google_oauth_tokens" in sql
    assert "ON CONFLICT (user_id) DO UPDATE" in sql
    assert args == ("user-1", "a", "b", None, None)
    # The database write succeeded, so the local file fallback must NOT have been used.
    assert not calendar_tool._CREDENTIAL_STORE_PATH.exists()


async def test_token_expiry_iso_string_is_coerced_to_datetime(monkeypatch):
    # google_oauth.py stores expiry as an ISO string, but the column is
    # timestamptz and asyncpg won't coerce a string the way Supabase's REST
    # layer did - it has to be bound as a real datetime.
    pool = _patch_pool(monkeypatch)

    await calendar_tool.save_credentials("user-1", {
        "access_token": "a", "refresh_token": "b",
        "token_expiry": "2026-09-01T00:00:00+00:00", "scope": "calendar.freebusy",
    })

    _, args = pool.calls[0]
    assert args[3] == datetime(2026, 9, 1, tzinfo=timezone.utc)
    assert args[4] == "calendar.freebusy"


async def test_unparseable_token_expiry_stores_null_rather_than_crashing(monkeypatch):
    pool = _patch_pool(monkeypatch)

    await calendar_tool.save_credentials("user-1", {
        "access_token": "a", "refresh_token": "b", "token_expiry": "not-a-date",
    })

    _, args = pool.calls[0]
    assert args[3] is None


async def test_database_error_falls_back_to_local_file(monkeypatch):
    monkeypatch.setattr(calendar_tool, "get_pool", AsyncMock(side_effect=RuntimeError("down")))

    await calendar_tool.save_credentials("user-1", {"access_token": "a", "refresh_token": "b"})
    creds = await calendar_tool.get_stored_credentials("user-1")

    assert creds == {"access_token": "a", "refresh_token": "b"}
    assert calendar_tool._CREDENTIAL_STORE_PATH.exists()
