"""
The app must have exactly one answer to "what day is it".

Live failure this pins, reproduced 2026-09-30 at 02:03 Sri Lanka time:
`_resolve_date_window` built a trip's default window from
`datetime.now(timezone.utc).date()` while `validate_trip_state` rejected past
windows against `date.today()`. Between 00:00 and 05:30 local those disagree,
because UTC is still on the previous date. Every fresh plan was therefore built
starting *yesterday*, and the next follow-up on that session failed with
"trip_dates window is entirely in the past", handing back the previous
itinerary unchanged and a null estimated_cost - which read to the user as "I
asked for something different and it ignored me".

Neither module was wrong alone, so neither module's own tests could catch it:
each asserted against the clock it already used. These tests assert the two
agree, which is the property that was actually broken.
"""
from datetime import date, datetime, timedelta, timezone

from app.core.state import TripState
from app.utils.clock import SRI_LANKA_TZ, now_local, today_local
from app.utils.validators import validate_trip_state


class TestOneClock:
    def test_today_is_sri_lankas_civil_date(self):
        expected = datetime.now(SRI_LANKA_TZ).date()
        assert today_local() == expected

    def test_offset_is_utc_plus_five_thirty(self):
        # Sri Lanka has had no daylight saving since 2006, so this is exact
        # rather than seasonal - if it ever changes, this is the failure that
        # should prompt a look rather than a silently wrong date.
        assert now_local().utcoffset() == timedelta(hours=5, minutes=30)

    def test_today_is_never_more_than_a_day_from_utc(self):
        # A sanity bound: a bad timezone would show up here as a large drift,
        # not as the off-by-one that hid for so long.
        assert abs((today_local() - datetime.now(timezone.utc).date()).days) <= 1


class TestAFreshWindowIsNeverAlreadyExpired:
    """The regression proper: whatever "today" means, a window built for it
    must survive the validator. These run at whatever wall-clock time CI
    happens to fire, including inside the 00:00-05:30 band that broke."""

    def _validated(self, start: date, end: date) -> list[str]:
        state = TripState(user_input="x")
        state.trip_dates = [{"start_date": start.isoformat(), "end_date": end.isoformat()}]
        return [e for e in validate_trip_state(state).errors if "in the past" in e]

    def test_a_single_day_trip_starting_today_is_accepted(self):
        today = today_local()
        assert self._validated(today, today) == []

    def test_a_multi_day_trip_starting_today_is_accepted(self):
        today = today_local()
        assert self._validated(today, today + timedelta(days=2)) == []

    def test_a_genuinely_past_window_is_still_rejected(self):
        # The check being fixed must keep doing its job - a window that really
        # has expired should still be caught.
        long_ago = today_local() - timedelta(days=30)
        assert self._validated(long_ago, long_ago + timedelta(days=1)) != []
