"""
One definition of "today" for the whole app.

This exists because there were two, and they disagreed. `_resolve_date_window`
built a trip's default window from `datetime.now(timezone.utc).date()`, while
`validate_state` rejected past windows against `date.today()` - the server's
local date. Those are the same day for most of the clock, so it looked fine,
but between 00:00 and 05:30 Sri Lanka time UTC is still on the previous date.
In that window every fresh plan was built starting *yesterday*, and then any
follow-up on it failed validation with "trip_dates window is entirely in the
past", returning the previous itinerary unchanged and a null cost. Reproduced
live 2026-09-30 at 02:03 local.

Neither side was wrong on its own; having two answers was. The app plans travel
*in Sri Lanka*, so the civil date there is the meaningful one: a traveler
asking at 1am on the 30th means the 30th, whatever UTC says and whatever
timezone the server happens to run in. Using the server's local date would work
only while the server sits in Sri Lanka, which is not something the code should
depend on.

Sri Lanka has observed UTC+05:30 with no daylight saving since 2006, so the
fixed offset is exact. ZoneInfo is used when the platform has tzdata (it
carries the historical record and any future change); the fixed offset is the
fallback so a missing tzdata package degrades to the right answer rather than
to an exception.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

try:  # pragma: no cover - depends on the platform's tz database
    from zoneinfo import ZoneInfo

    SRI_LANKA_TZ = ZoneInfo("Asia/Colombo")
except Exception:  # pragma: no cover - Windows without the tzdata package
    SRI_LANKA_TZ = timezone(timedelta(hours=5, minutes=30), name="+0530")


def now_local() -> datetime:
    """Current time as a traveler in Sri Lanka would read it."""
    return datetime.now(SRI_LANKA_TZ)


def today_local() -> date:
    """The civil date in Sri Lanka - the app's single notion of "today"."""
    return now_local().date()
