# app/tools/calendar_tool.py
import json
import logging
from datetime import datetime, timedelta, timezone, date
from pathlib import Path
from typing import Optional

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

from app.config.settings import settings

# Try to import supabase, but make it optional - same pattern as db_tool.py.
try:
    from supabase import acreate_client, AsyncClient
    _SUPABASE_AVAILABLE = True
except ImportError:
    _SUPABASE_AVAILABLE = False
    acreate_client = None
    AsyncClient = None

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/calendar.freebusy"]


# --- Credential storage -------------------------------------------------
# Real persistence: Supabase's `google_oauth_tokens` table (user_id,
# access_token, refresh_token, token_expiry, scope - see member_B.md).
# Falls back to a local JSON file when Supabase isn't configured (e.g. a
# fresh clone with no .env yet), so this still works without a database -
# it just won't survive across multiple server instances or, if Supabase
# itself is down, past this process's restart.
_CREDENTIAL_STORE_PATH = Path(__file__).resolve().parent.parent.parent / "calendar_tokens.json"

_client: Optional["AsyncClient"] = None


async def _get_client() -> Optional["AsyncClient"]:
    """Lazily create and cache a single AsyncClient for the process."""
    global _client
    if not _SUPABASE_AVAILABLE or not settings.supabase_url or not settings.supabase_key:
        return None
    if _client is None:
        _client = await acreate_client(settings.supabase_url, settings.supabase_key)
    return _client


def _load_local_store() -> dict:
    if not _CREDENTIAL_STORE_PATH.exists():
        return {}
    try:
        return json.loads(_CREDENTIAL_STORE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_local_store(store: dict) -> None:
    _CREDENTIAL_STORE_PATH.write_text(json.dumps(store, indent=2))


async def get_stored_credentials(user_id: str) -> dict | None:
    try:
        client = await _get_client()
        if client:
            response = await (
                client.table("google_oauth_tokens")
                .select("*")
                .eq("user_id", user_id)
                .limit(1)
                .execute()
            )
            if response.data:
                row = response.data[0]
                return {
                    "access_token": row.get("access_token"),
                    "refresh_token": row.get("refresh_token"),
                    "token_expiry": row.get("token_expiry"),
                    "scope": row.get("scope"),
                }
    except Exception as e:
        logger.warning(f"Supabase token lookup failed for user '{user_id}', trying local store: {e}")

    return _load_local_store().get(user_id)


async def save_credentials(user_id: str, creds: dict) -> None:
    try:
        client = await _get_client()
        if client:
            await client.table("google_oauth_tokens").upsert({
                "user_id": user_id,
                "access_token": creds.get("access_token"),
                "refresh_token": creds.get("refresh_token"),
                "token_expiry": creds.get("token_expiry"),
                "scope": creds.get("scope"),
            }).execute()
            return
    except Exception as e:
        logger.warning(f"Supabase token save failed for user '{user_id}', falling back to local store: {e}")

    store = _load_local_store()
    store[user_id] = creds
    _save_local_store(store)
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
                "token_expiry": google_creds.expiry.isoformat() if google_creds.expiry else None,
                "scope": stored.get("scope"),
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