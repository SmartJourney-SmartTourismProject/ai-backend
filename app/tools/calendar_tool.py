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
from app.utils.db_pool import get_pool

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/calendar.freebusy"]


# --- Credential storage -------------------------------------------------
# Real persistence: the `google_oauth_tokens` table (user_id, access_token,
# refresh_token, token_expiry, scope). This table is the AI backend's alone
# to write - see backend/docs/BACKEND_PLAN.md §2.
# Falls back to a local JSON file when DATABASE_URL isn't configured (e.g. a
# fresh clone with no .env yet), so this still works without a database - it
# just won't survive across multiple server instances or, if the database
# itself is down, past this process's restart.
_CREDENTIAL_STORE_PATH = Path(__file__).resolve().parent.parent.parent / "calendar_tokens.json"


def _coerce_expiry(value) -> Optional[datetime]:
    """
    google_oauth.py stores token_expiry as an ISO string, but the column is
    timestamptz and asyncpg won't coerce a string for us (the Supabase REST
    layer used to). Accept either and hand asyncpg a real datetime.
    """
    if value is None or isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        logger.warning(f"Unparseable token_expiry {value!r}; storing NULL.")
        return None



def _load_local_store() -> dict:
    if not _CREDENTIAL_STORE_PATH.exists():
        return {}
    try:
        return json.loads(_CREDENTIAL_STORE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_local_store(store: dict) -> None:
    _CREDENTIAL_STORE_PATH.write_text(json.dumps(store, indent=2))


_SELECT_TOKENS = """
    SELECT access_token, refresh_token, token_expiry, scope
    FROM google_oauth_tokens
    WHERE user_id = $1
"""

_UPSERT_TOKENS = """
    INSERT INTO google_oauth_tokens
        (user_id, access_token, refresh_token, token_expiry, scope)
    VALUES ($1, $2, $3, $4, $5)
    ON CONFLICT (user_id) DO UPDATE SET
        access_token  = EXCLUDED.access_token,
        refresh_token = EXCLUDED.refresh_token,
        token_expiry  = EXCLUDED.token_expiry,
        scope         = EXCLUDED.scope,
        updated_at    = now()
"""


async def get_stored_credentials(user_id: str) -> dict | None:
    try:
        pool = await get_pool()
        if pool:
            row = await pool.fetchrow(_SELECT_TOKENS, user_id)
            if row:
                return {
                    "access_token": row["access_token"],
                    "refresh_token": row["refresh_token"],
                    "token_expiry": row["token_expiry"],
                    "scope": row["scope"],
                }
            # Database reachable and authoritative: no row means this user
            # genuinely hasn't connected a calendar (or revoked it). Do NOT
            # fall through to the local file here - that could resurrect a
            # token the user deliberately disconnected. The local store is a
            # fallback for "no database", not for "database says no".
            return None
    except Exception as e:
        logger.warning(f"Token lookup failed for user '{user_id}', trying local store: {e}")

    return _load_local_store().get(user_id)


async def save_credentials(user_id: str, creds: dict) -> None:
    try:
        pool = await get_pool()
        if pool:
            await pool.execute(
                _UPSERT_TOKENS,
                user_id,
                creds.get("access_token"),
                creds.get("refresh_token"),
                _coerce_expiry(creds.get("token_expiry")),
                creds.get("scope"),
            )
            return
    except Exception as e:
        logger.warning(f"Token save failed for user '{user_id}', falling back to local store: {e}")

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