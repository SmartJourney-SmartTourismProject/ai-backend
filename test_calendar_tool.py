# test_calendar_tool.py
# Run from your project root: python3 test_calendar_tool.py
# Mocks Google's API so this tests YOUR logic (range-grouping, busy-date
# filtering) without needing real OAuth tokens or network access.

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

import app.tools.calendar_tool as calendar_tool


async def main():
    user_id = "test-user-1"

    await calendar_tool.save_credentials(user_id, {
        "access_token": "fake-access-token",
        "refresh_token": "fake-refresh-token",
    })

    now = datetime.now(timezone.utc)

    # Fake "busy" response: busy tomorrow + day after, then free, then
    # busy again 5 days out.
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

    print("Result:", result)

    checks = []
    checks.append(("Returns a list", isinstance(result, list)))
    checks.append(("Non-empty (busy days shouldn't consume everything)", len(result) > 0))
    if result:
        checks.append(("Each entry is a dict with start_date/end_date", all(
            isinstance(r, dict) and "start_date" in r and "end_date" in r for r in result
        )))
        today_str = now.date().isoformat()
        checks.append((f"First range starts today ({today_str})", result[0]["start_date"] == today_str))

    passed = 0
    for label, ok in checks:
        if ok:
            passed += 1
        print(f"[{'PASS' if ok else 'FAIL'}] {label}")

    print(f"\n{passed}/{len(checks)} checks passed")


if __name__ == "__main__":
    asyncio.run(main())