"""
Deterministic context resolution - replaces the Orchestrator ReAct agent
(itinerary-quality/token-reduction pass, "C2"). What used to be an LLM
"agent" here was six tool calls with essentially no reasoning between
them: resolve the destination, resolve a date window, fetch weather and
disaster info, done. safety_notes was ALREADY computed deterministically
(found live, 2026-09-06 - the LLM reliably fetched real disaster data but
did not reliably also write a note about it into its own free-text
field), and the agent invented dates a full year wrong until a "today"
field was bolted on - both are symptoms of using a model for work that
was never probabilistic. Recommendation (selecting and justifying) and
Planning (shaping the trip) remain real ReAct agents; this was always
plumbing.

Removes ~4 of a fresh plan's ~12 LLM calls (3 loop turns + 1 mandatory
finalize) and the entire CONTEXT_TOOLS schema payload from every request -
see docs/AI_BACKEND_OPTIMIZATION_PLAN.md and the itinerary-quality plan.

start_location is NOT resolved here - app/api/trip.py already calls
resolve_start_location() with the request's real client_gps/client_ip
before the graph even runs (TripState itself has no client_gps/client_ip
fields), so state.start_location is already whatever it's going to be by
the time this runs.
"""
from __future__ import annotations

import logging
from datetime import date as date_cls, datetime, timedelta, timezone
from typing import Optional

from app.core.destinations import display_name, resolve_destinations
from app.core.legs import plan_legs
from app.core.state import TripState
from app.utils.clock import today_local
from app.tools.calendar_tool import get_free_days
from app.tools.disaster_tool import get_disaster_info
from app.tools.geo_tool import resolve_district, resolve_place
from app.tools.weather_tool import get_weather

logger = logging.getLogger(__name__)

DEFAULT_DISASTER = {"safe": True, "active_events": [], "note": "disaster data unavailable"}


def _dates_in_window(start_date: str, end_date: str) -> list[str]:
    start = date_cls.fromisoformat(start_date)
    end = date_cls.fromisoformat(end_date)
    out, d = [], start
    while d <= end:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


async def _resolve_date_window(state: TripState) -> dict:
    """Order: an explicit carried trip_dates window first (a real prior
    answer, e.g. from an earlier turn), then the traveler's connected
    calendar's soonest free window long enough for the trip, then a plain
    default starting from today - never an invented date (the actual bug
    class this replaces)."""
    duration = state.duration_days or 1
    # Sri Lanka's civil date, not UTC's - see app/utils/clock.py for the
    # follow-up failure the two-answers version caused.
    today = today_local()

    if state.trip_dates:
        window = state.trip_dates[0]
        start, end = window.get("start_date"), window.get("end_date")
        # The source this window was ORIGINALLY resolved with (written back
        # by this function below) - not "user" unconditionally, which used
        # to relabel a carried calendar/default window as "user" the moment
        # it round-tripped through state, losing the distinction the
        # calendar branch below actually needs (see the "calendar" handling
        # a few lines down).
        source = window.get("source", "user")
        if start and end and source != "calendar":
            # A carried window's day count can go stale: a follow-up that
            # changes duration_days (e.g. "make it 5 days") doesn't touch
            # trip_dates, so a window resolved for the OLD duration would
            # otherwise be reused as-is - live-found 2026-09-26, a 1->N day
            # follow-up kept a 1-date window while duration_days became N,
            # so every day past day 1 failed L2's dates_in_window and the
            # request fell all the way to the deterministic fallback planner.
            # start_date is kept (it's the real answer to "when does the
            # trip begin"); only end_date/dates are recomputed against the
            # CURRENT duration.
            dates = _dates_in_window(start, end)
            if len(dates) != duration:
                end = (date_cls.fromisoformat(start) + timedelta(days=duration - 1)).isoformat()
                dates = _dates_in_window(start, end)
            return {"start_date": start, "end_date": end, "source": source, "dates": dates}
        # source == "calendar": deliberately NOT reused as-is even when
        # start/end are present - a calendar window was only ever checked
        # long enough for duration AT THE TIME it was resolved (the
        # `>= duration` guard below). Silently keeping it after duration grew
        # could book the user over their own busy days instead of
        # re-checking free_ranges against the new, larger duration.

    if state.user_id:
        try:
            free_ranges = await get_free_days(state.user_id)
        except Exception as e:
            logger.warning(f"context_resolver: get_free_days failed, degrading to default window: {e}")
            free_ranges = []
        for r in free_ranges:
            range_start = date_cls.fromisoformat(r["start_date"])
            range_end = date_cls.fromisoformat(r["end_date"])
            if (range_end - range_start).days + 1 >= duration:
                start_date = range_start
                end_date = start_date + timedelta(days=duration - 1)
                return {"start_date": start_date.isoformat(), "end_date": end_date.isoformat(),
                        "source": "calendar", "dates": _dates_in_window(start_date.isoformat(), end_date.isoformat())}

    end_date = today + timedelta(days=duration - 1)
    return {"start_date": today.isoformat(), "end_date": end_date.isoformat(),
            "source": "default", "dates": _dates_in_window(today.isoformat(), end_date.isoformat())}


def _build_disaster_summary(raw: dict) -> dict:
    """Shapes get_disaster_info()'s raw {safe, active_events[, note]} into
    the full DisasterSummary contract (app/models/schemas.py) - active_events
    is already severity-sorted (red first) by disaster_tool.py, so the first
    entry's severity is the max."""
    active_events = raw.get("active_events") or []
    max_severity = active_events[0]["severity"] if active_events else None
    return {
        "safe": raw.get("safe", True),
        "max_severity": max_severity,
        "active_events": active_events,
        "note": raw.get("note"),
    }


def _build_safety_notes(disaster: dict) -> list[str]:
    """Deterministic replacement for what the LLM was asked to write into
    safety_notes by hand - already proven more reliable than the model at
    this exact job (live-found 2026-09-06, see this module's docstring)."""
    red_events = [e for e in disaster["active_events"] if e.get("severity") == "red"]
    if not red_events:
        return []
    titles = ", ".join(e["title"] for e in red_events)
    return [f"Active red-level hazard(s) near your destination: {titles}."]


async def _resolve_places(destination: Optional[str]) -> list[dict]:
    """In-country places with a district, in the order named. Names that
    resolve abroad are dropped here - slot_filling.py has already refused a
    trip whose every place is outside Sri Lanka."""
    places = await resolve_destinations(
        destination, resolve_place=resolve_place, resolve_district=resolve_district,
    )
    return [
        p for p in places
        if p.get("confidence") != "out_of_country" and p.get("district_id")
    ]


async def _per_day_weather(places: list[dict], dates: list[str], duration: int) -> list[dict]:
    """The forecast for each day at the place that day is spent in. One
    place: its forecast as-is. Several: fetched per place (weather_tool
    caches per location) and each date taken from its leg's place."""
    if len(places) == 1:
        result = await get_weather(places[0]["lat"], places[0]["lon"], dates)
        return (result or {}).get("forecast") or []

    by_place = []
    for p in places:
        result = await get_weather(p["lat"], p["lon"], dates)
        by_place.append({w["date"]: w for w in ((result or {}).get("forecast") or [])})
    out = []
    for leg, date in zip(plan_legs(len(places), duration), dates):
        forecast = by_place[leg.place_index].get(date)
        if forecast is not None:
            out.append(forecast)
    return out


async def resolve_trip_context(state: TripState) -> None:
    """Mutates `state` in place - same contract as
    app/core/followup_replan.py's rebuild_targeted_days(), the other
    deterministic (non-agent) node in this graph. Never raises: any
    failure degrades to state.errors (the "orchestrator_failed" prefix is
    kept verbatim - it's a HARD error per _respond_node's
    _SOFT_ERROR_PREFIXES, matching the old agent's behavior exactly), with
    trip_context left as-is for downstream nodes to degrade around (the
    orchestrate->recommend graph edge is unconditional either way)."""
    try:
        places = await _resolve_places(state.destination)
        if not places:
            # One retry, same as the old prompt's RULE 2 - resolve_place's
            # own failures are usually a transient network/DB hiccup, not a
            # deterministic rejection (out-of-country already routed to
            # respond before this node is ever reached - see slot_filling.py).
            places = await _resolve_places(state.destination)
        if not places:
            state.errors.append(f"orchestrator_failed: could not resolve destination '{state.destination}'")
            # Asked, not guessed: without a district there is nothing to
            # plan from, and running the LLM agents anyway only spent tokens
            # on an empty plan (live-found 2026-10-01, "galle and matara"
            # before multi-place support). The graph routes straight to
            # respond on this - see _route_after_orchestrate.
            state.clarification_needed = (
                f"I couldn't find \"{state.destination}\" in Sri Lanka. Which town or district "
                "should I plan around? You can name more than one, e.g. \"Galle and Matara\"."
            )
            return

        # More places than days can't each get a day - keep the first ones,
        # in the order the traveller named them, and say so.
        duration = state.duration_days or 1
        if len(places) > duration:
            dropped = display_name(places[duration:])
            places = places[:duration]
            state.errors.append(
                f"location_note: {duration} day(s) isn't enough for every place named, "
                f"so this plan covers {display_name(places)} and leaves out {dropped}."
            )
        place = places[0]
        district_id = place["district_id"]

        date_window = await _resolve_date_window(state)

        per_day_weather = await _per_day_weather(places, date_window["dates"], duration)

        disaster_raw = await get_disaster_info(place["lat"], place["lon"]) or DEFAULT_DISASTER
        disaster = _build_disaster_summary(disaster_raw)
        safety_notes = _build_safety_notes(disaster)

        context_confidence = (
            "low" if place.get("confidence") != "high"
            else "medium" if date_window["source"] == "default"
            else "high"
        )

        state.trip_context = {
            "destination_name": place["name"] if len(places) == 1 else display_name(places),
            "district_id": district_id,
            "lat": place["lat"],
            "lon": place["lon"],
            # Every place of a multi-place trip, in order - the first is also
            # the fields above, so single-place consumers are unaffected.
            # app/core/legs.py decides which day is spent where.
            "places": [
                {"name": p["name"], "district_id": p["district_id"], "lat": p["lat"], "lon": p["lon"]}
                for p in places
            ],
            "start_location": state.start_location,
            "date_window": date_window,
            "per_day_weather": per_day_weather,
            "disaster": disaster,
            "safety_notes": safety_notes,
            "context_confidence": context_confidence,
        }
        state.trip_dates = [{
            "start_date": date_window["start_date"], "end_date": date_window["end_date"],
            "source": date_window["source"],
        }]
        state.weather = {"forecast": per_day_weather}
        state.disaster = disaster
        if safety_notes:
            state.errors.extend(f"safety_note: {n}" for n in safety_notes)

        state.react_traces["orchestrator"] = {"steps_used": 0, "tools_used": [
            "resolve_place", "resolve_district", "get_weather", "get_disaster_info",
        ], "stopped_by": "deterministic"}
    except Exception as e:
        logger.warning(f"context_resolver failed: {e}")
        state.errors.append(f"orchestrator_failed: {e}")
