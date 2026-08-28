# app/tools/calendar_tool.py
import json
from datetime import datetime, timedelta, timezone, date
from pathlib import Path

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

from app.config.settings import settings

SCOPES = ["https://www.googleapis.com/auth/calendar.freebusy"]


# --- Credential storage -------------------------------------------------
# TODO(Member B): move this to a real DB table (google_oauth_tokens) once
# Supabase is wired in - see handoff note in member_B.md. Until then, a
# local JSON file so tokens survive an app restart instead of resetting
# every time like the previous in-memory dict did. Not safe for multiple
# server instances (no locking, no shared storage) - fine for local dev.
_CREDENTIAL_STORE_PATH = Path(__file__).resolve().parent.parent.parent / "calendar_tokens.json"


def _load_store() -> dict:
    if not _CREDENTIAL_STORE_PATH.exists():
        return {}
    try:
        return json.loads(_CREDENTIAL_STORE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_store(store: dict) -> None:
    _CREDENTIAL_STORE_PATH.write_text(json.dumps(store, indent=2))


async def get_stored_credentials(user_id: str) -> dict | None:
    return _load_store().get(user_id)


async def save_credentials(user_id: str, creds: dict) -> None:
    store = _load_store()
    store[user_id] = creds
    _save_store(store)
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