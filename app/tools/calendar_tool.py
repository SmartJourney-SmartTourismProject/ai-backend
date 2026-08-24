# app/tools/calendar_tool.py
from datetime import datetime, timedelta, timezone, date

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

from app.config.settings import settings

SCOPES = ["https://www.googleapis.com/auth/calendar.freebusy"]


# --- Placeholder credential storage -----------------------------------
# TODO(Member B): back these with a real DB table (see handoff note).
# For now, an in-memory dict so calendar_tool.py is fully testable without
# a real DB — this resets every time the app restarts, which is fine for
# local dev but not persistence across sessions.
_FAKE_CREDENTIAL_STORE: dict[str, dict] = {}


async def get_stored_credentials(user_id: str) -> dict | None:
    return _FAKE_CREDENTIAL_STORE.get(user_id)


async def save_credentials(user_id: str, creds: dict) -> None:
    _FAKE_CREDENTIAL_STORE[user_id] = creds
# ------------------------------------------------------------------------


def _creds_dict_to_google_credentials(creds: dict) -> Credentials:
    return Credentials(
        token=creds.get("access_token"),
        refresh_token=creds.get("refresh_token"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=settings.google_calendar_client_id,
        client_secret=settings.google_calendar_client_secret,
        scopes=SCOPES,
    )


def _group_into_ranges(free_days: list[str]) -> list[dict]:
    """Collapse a sorted list of ISO date strings into contiguous ranges."""
    if not free_days:
        return []

    ranges = []
    start = prev = date.fromisoformat(free_days[0])

    for day_str in free_days[1:]:
        day = date.fromisoformat(day_str)
        if (day - prev).days == 1:
            prev = day
            continue
        ranges.append({"start_date": start.isoformat(), "end_date": prev.isoformat()})
        start = prev = day

    ranges.append({"start_date": start.isoformat(), "end_date": prev.isoformat()})
    return ranges


async def get_free_days(user_id: str, search_window_days: int = 30) -> list[dict]:
    """
    Returns contiguous free-day ranges within the next `search_window_days`,
    e.g. [{"start_date": "2026-08-20", "end_date": "2026-08-22"}, ...].
    If no calendar is connected (or anything fails), returns [] rather
    than raising — calendar failures should never block the graph.

    Flow:
        user_id
          -> get stored Google credentials (none -> return [])
          -> check/refresh OAuth token
          -> connect to Google Calendar
          -> ask Google for busy periods (freebusy().query)
          -> collect busy dates
          -> check next `search_window_days` days
          -> group the free (non-busy) days into contiguous ranges
    """
    stored = await get_stored_credentials(user_id)
    if not stored:
        return []

    try:
        google_creds = _creds_dict_to_google_credentials(stored)

        if google_creds.expired and google_creds.refresh_token:
            google_creds.refresh(Request())
            await save_credentials(user_id, {
                "access_token": google_creds.token,
                "refresh_token": google_creds.refresh_token,
            })

        service = build("calendar", "v3", credentials=google_creds)

        now = datetime.now(timezone.utc)
        window_end = now + timedelta(days=search_window_days)

        body = {
            "timeMin": now.isoformat(),
            "timeMax": window_end.isoformat(),
            "items": [{"id": "primary"}],
        }
        result = service.freebusy().query(body=body).execute()
        busy_periods = result["calendars"]["primary"]["busy"]

        busy_dates = set()
        for period in busy_periods:
            start = datetime.fromisoformat(period["start"].replace("Z", "+00:00")).date()
            end = datetime.fromisoformat(period["end"].replace("Z", "+00:00")).date()
            d = start
            while d <= end:
                busy_dates.add(d.isoformat())
                d += timedelta(days=1)

        free_days = []
        d = now.date()
        for _ in range(search_window_days):
            if d.isoformat() not in busy_dates:
                free_days.append(d.isoformat())
            d += timedelta(days=1)

        return _group_into_ranges(free_days)

    except Exception:
        return []