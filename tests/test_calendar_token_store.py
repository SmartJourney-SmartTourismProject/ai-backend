# tests/test_calendar_token_store.py
# Checks the persistent credential store from calendar_tool.py: Supabase's
# `google_oauth_tokens` table when configured, falling back to a local
# JSON file otherwise. Uses the isolated temp path from conftest.py's
# autouse fixture, so this never touches the real calendar_tokens.json.

import json
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


class _FakeQuery:
    def __init__(self, data, sink: list | None = None):
        self._data = data
        self._sink = sink  # records upsert() payloads for save tests

    def select(self, *args, **kwargs):
        return self

    def eq(self, *args, **kwargs):
        return self

    def limit(self, *args, **kwargs):
        return self

    def upsert(self, payload, *args, **kwargs):
        if self._sink is not None:
            self._sink.append(payload)
        return self

    async def execute(self):
        from types import SimpleNamespace
        return SimpleNamespace(data=self._data)


class _FakeSupabaseClient:
    def __init__(self, table_data: dict, sink: list | None = None):
        self._table_data = table_data
        self._sink = sink

    def table(self, name: str):
        return _FakeQuery(self._table_data.get(name, []), self._sink)


def _patch_supabase(monkeypatch, table_data: dict, sink: list | None = None):
    monkeypatch.setattr(calendar_tool, "_SUPABASE_AVAILABLE", True)
    monkeypatch.setattr(calendar_tool.settings, "supabase_url", "https://fake.supabase.co")
    monkeypatch.setattr(calendar_tool.settings, "supabase_key", "fake-key")
    monkeypatch.setattr(
        calendar_tool, "_get_client",
        AsyncMock(return_value=_FakeSupabaseClient(table_data, sink)),
    )


async def test_get_stored_credentials_reads_from_real_supabase(monkeypatch):
    _patch_supabase(monkeypatch, {
        "google_oauth_tokens": [{
            "user_id": "user-1", "access_token": "real-token", "refresh_token": "real-refresh",
        }],
    })

    creds = await calendar_tool.get_stored_credentials("user-1")

    assert creds == {
        "access_token": "real-token", "refresh_token": "real-refresh",
        "token_expiry": None, "scope": None,
    }


async def test_get_stored_credentials_unknown_user_in_supabase_returns_none(monkeypatch):
    _patch_supabase(monkeypatch, {"google_oauth_tokens": []})

    creds = await calendar_tool.get_stored_credentials("nobody")

    assert creds is None


async def test_save_credentials_writes_to_real_supabase(monkeypatch):
    sink = []
    _patch_supabase(monkeypatch, {"google_oauth_tokens": []}, sink=sink)

    await calendar_tool.save_credentials("user-1", {"access_token": "a", "refresh_token": "b"})

    assert sink == [{
        "user_id": "user-1", "access_token": "a", "refresh_token": "b",
        "token_expiry": None, "scope": None,
    }]
    # Supabase succeeded, so the local file fallback must NOT have been used.
    assert not calendar_tool._CREDENTIAL_STORE_PATH.exists()


async def test_token_expiry_and_scope_round_trip_through_supabase(monkeypatch):
    sink = []
    _patch_supabase(monkeypatch, {"google_oauth_tokens": [
        {"user_id": "user-1", "access_token": "a", "refresh_token": "b",
         "token_expiry": "2026-09-01T00:00:00+00:00", "scope": "calendar.freebusy"},
    ]}, sink=sink)

    creds = await calendar_tool.get_stored_credentials("user-1")
    assert creds == {
        "access_token": "a", "refresh_token": "b",
        "token_expiry": "2026-09-01T00:00:00+00:00", "scope": "calendar.freebusy",
    }

    await calendar_tool.save_credentials("user-1", {
        "access_token": "a", "refresh_token": "b",
        "token_expiry": "2026-09-01T00:00:00+00:00", "scope": "calendar.freebusy",
    })
    assert sink == [{
        "user_id": "user-1", "access_token": "a", "refresh_token": "b",
        "token_expiry": "2026-09-01T00:00:00+00:00", "scope": "calendar.freebusy",
    }]


async def test_supabase_error_falls_back_to_local_file(monkeypatch):
    monkeypatch.setattr(calendar_tool, "_SUPABASE_AVAILABLE", True)
    monkeypatch.setattr(calendar_tool.settings, "supabase_url", "https://fake.supabase.co")
    monkeypatch.setattr(calendar_tool.settings, "supabase_key", "fake-key")
    monkeypatch.setattr(calendar_tool, "_get_client", AsyncMock(side_effect=RuntimeError("down")))

    await calendar_tool.save_credentials("user-1", {"access_token": "a", "refresh_token": "b"})
    creds = await calendar_tool.get_stored_credentials("user-1")

    assert creds == {"access_token": "a", "refresh_token": "b"}
    assert calendar_tool._CREDENTIAL_STORE_PATH.exists()
