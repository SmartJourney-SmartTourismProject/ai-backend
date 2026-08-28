# tests/test_calendar_token_store.py
# Checks the persistent (JSON-file-backed) credential store from calendar_tool.py.
# Uses the isolated temp path from conftest.py's autouse fixture, so this
# never touches the real calendar_tokens.json.

import json

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
