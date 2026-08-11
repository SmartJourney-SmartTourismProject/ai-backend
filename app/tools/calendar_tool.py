# app/tools/calendar_tool.py
from datetime import datetime, timedelta, timezone

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


async def get_free_days(user_id: str, search_window_days: int = 30) -> list[str]:
    """
    Returns a list of ISO date strings (YYYY-MM-DD) within the next
    `search_window_days` where the user has no calendar events.
    If no calendar is connected (or anything fails), returns [] rather
    than raising — calendar failures should never block the graph.
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

        return free_days

    except Exception:
        return []


    '''
    User ID
   │
   ▼
Get stored Google credentials
   │
   ├── No credentials ──► return []
   │
   ▼
Check/refresh OAuth token
   │
   ▼
Connect to Google Calendar
   │
   ▼
Ask Google for busy periods
   │
   ▼
Collect busy dates
   │
   ▼
Check next 30 days
   │
   ▼
Return dates that are not busy
    '''