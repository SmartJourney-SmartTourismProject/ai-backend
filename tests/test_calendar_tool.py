# tests/test_calendar_tool.py
# Mocks Google's API so this tests OUR logic (range-grouping, busy-date
# filtering) without needing real OAuth tokens or network access.

from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

import app.tools.calendar_tool as calendar_tool


async def test_get_free_days_groups_ranges_and_excludes_busy_days():
    user_id = "test-user-1"
    await calendar_tool.save_credentials(user_id, {
        "access_token": "fake-access-token",
        "refresh_token": "fake-refresh-token",
    })

    now = datetime.now(timezone.utc)
    busy_start_1 = (now + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
    busy_end_1 = (now + timedelta(days=2)).replace(hour=17, minute=0, second=0, microsecond=0)
    busy_start_2 = (now + timedelta(days=5)).replace(hour=9, minute=0, second=0, microsecond=0)
    busy_end_2 = (now + timedelta(days=5)).replace(hour=17, minute=0, second=0, microsecond=0)

    fake_freebusy_response = {
        "calendars": {
            "primary": {
                "busy": [
                    {"start": busy_start_1.isoformat(), "end": busy_end_1.isoformat()},
                    {"start": busy_start_2.isoformat(), "end": busy_end_2.isoformat()},
                ]
            }
        }
    }

    with patch("app.tools.calendar_tool.Credentials") as MockCreds, \
         patch("app.tools.calendar_tool.build") as mock_build:

        mock_creds_instance = MagicMock()
        mock_creds_instance.expired = False
        MockCreds.return_value = mock_creds_instance

        mock_service = MagicMock()
        mock_service.freebusy.return_value.query.return_value.execute.return_value = fake_freebusy_response
        mock_build.return_value = mock_service

        result = await calendar_tool.get_free_days(user_id, search_window_days=10)

    assert isinstance(result, list)
    assert len(result) > 0
    assert all(isinstance(r, dict) and "start_date" in r and "end_date" in r for r in result)
    assert result[0]["start_date"] == now.date().isoformat()


async def test_get_free_days_no_stored_credentials_returns_empty():
    result = await calendar_tool.get_free_days("nobody-connected", search_window_days=5)
    assert result == []


async def test_get_free_days_google_api_error_returns_empty_not_raises():
    await calendar_tool.save_credentials("test-user-2", {
        "access_token": "fake-access-token",
        "refresh_token": "fake-refresh-token",
    })

    with patch("app.tools.calendar_tool.Credentials") as MockCreds, \
         patch("app.tools.calendar_tool.build") as mock_build:
        mock_creds_instance = MagicMock()
        mock_creds_instance.expired = False
        MockCreds.return_value = mock_creds_instance
        mock_build.side_effect = RuntimeError("Google API unreachable")

        result = await calendar_tool.get_free_days("test-user-2", search_window_days=10)

    assert result == []
