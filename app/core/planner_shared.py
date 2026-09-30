"""
The planner agent's human-message payload, factored out so
app/agents/planner_agent.py's normal run and app/core/orchestrator.py's
_repair_node build the exact same context from TripState - a repair attempt
that saw a DIFFERENT view of the candidates/budget than the original planning
call would be reasoning about a request that was never actually made.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date as date_cls, timedelta
from typing import Optional

from app.config.settings import settings
from app.core.budget import CostReferenceTable, cost_lookup_for
from app.core.itinerary import DayConstraints, DaySelections, attraction_candidates, build_day_plan
from app.core.output_validator import WEATHER_RAIN_THRESHOLD
from app.core.react import TraceStep
from app.core.scoring import TravelMatrix
from app.core.state import TripState
from app.models.schemas import ItineraryDay

logger = logging.getLogger(__name__)


def resolve_planner_max_steps(duration_days: int | None) -> int:
    """The planner (and repair) need one build_day_plan call per day, plus a
    turn for estimate_costs/check_budget - REACT_MAX_STEPS' flat default of
    3 (app/core/react.py's ReActConfig) starves any trip longer than ~2 days
    even when the prompt asks the model to batch same-turn tool calls
    (parallel calls still only count as ONE step - see run_react's
    asyncio.gather). Live-found (fallback investigation, 2026-09-25): a real
    3-day request only reached build_day_plan for 2 of 3 days before running
    out of turns. +1 over duration_days leaves room for a check_budget/
    estimate_costs turn without uncapping it for a pathological input."""
    return max(settings.react_max_steps, (duration_days or 1) + 1)

# Single source of truth (itinerary-quality/token-reduction pass) - this
# exact dict used to be copied in app/core/followup_replan.py and inlined
# again in app/core/orchestrator.py's _fallback_node, and
# app/prompts/planner_prompt.py's RULE 1 separately claimed "packed=4-5",
# disagreeing with the code's flat 5. One place to change now; the prompt
# interpolates from PACE_ITEMS too, so it cannot drift again.
PACE_ITEMS: dict[str, int] = {"relaxed": 2, "balanced": 3, "packed": 5}


def category_excluded(state: TripState, category: str) -> bool:
    """Did the traveler rule this kind of stop out entirely?

    "give viewpoints only" used to come back with two restaurants in it,
    because every planner path passed include_lunch=True/include_dinner=True
    as literals and nothing consulted the request. must_avoid could not
    express this either - it filters listings by subject tag ("no hiking"),
    not by kind of stop.

    One helper, used by all three planner paths, so a new path cannot quietly
    reintroduce the hardcoded meals.
    """
    return category in (state.exclude_categories or [])


def meal_slots(state: TripState) -> tuple[bool, bool]:
    """(include_lunch, include_dinner) for this trip."""
    wants_restaurants = not category_excluded(state, "restaurant")
    return wants_restaurants, wants_restaurants


def resolve_items_per_day(state: TripState) -> int:
    """The one place that turns a traveler's pace/itinerary-density request
    into a concrete daily item count. Currently just PACE_ITEMS keyed by
    state.pace (default "balanced"=3) - Part 3 (follow-up "fewer
    destinations per day") extends this to prefer an explicit
    state.items_per_day when the traveler has set one directly, since
    pace's 3-value enum can't express "one fewer than before"."""
    if state.items_per_day:
        return state.items_per_day
    return PACE_ITEMS.get(state.pace or "balanced", 3)


@dataclass
class DayContext:
    """Real, server-known facts about this request's trip window - passed to
    build_planning_tools() so the build_day_plan tool can correct a model's
    day/date/check-in/check-out/exclude_outdoor/items_target argument against
    ground truth instead of trusting it outright (Part 5, server-side
    enforcement: these are exactly the fields output_validator.py already
    checks the model's FINISHED output against - day_count, dates_in_window,
    weather_respect, items_per_day_respected - so correcting them at the
    source means the plan can no longer fail those checks in the first
    place, not just get caught failing them after the fact)."""
    start_date: date_cls
    duration_days: int
    per_day_rain_probability: dict[str, float] = field(default_factory=dict)
    expected_items_per_day: int = 3
    # Kinds of stop the traveler ruled out. Server-known ground truth, exactly
    # like the fields above: the model does not get to decide whether to honour
    # "viewpoints only", any more than it decides what day 2's date is.
    exclude_categories: list[str] = field(default_factory=list)


def resolve_day_context(state: TripState) -> DayContext:
    """The one place app/agents/planner_agent.py's normal run and
    app/core/orchestrator.py's _repair_node both build this from - same
    reasoning as build_planner_human_message's own docstring: a repair
    attempt must be corrected against the SAME ground truth the original
    planning call was."""
    ctx = state.trip_context or {}
    date_window = ctx.get("date_window") or {}
    try:
        start_date = date_cls.fromisoformat(date_window["start_date"])
    except (KeyError, ValueError, TypeError):
        start_date = date_cls.today()
    per_day_rain = {w["date"]: w["rain_probability"] for w in (ctx.get("per_day_weather") or [])}
    return DayContext(
        start_date=start_date,
        duration_days=state.duration_days or 1,
        per_day_rain_probability=per_day_rain,
        expected_items_per_day=resolve_items_per_day(state),
        exclude_categories=state.exclude_categories or [],
    )

# O2 (AI_BACKEND_OPTIMIZATION_PLAN.md): exactly the fields the planner
# prompt's own RULES reference. Previously every selection dict went out at
# full width (all 15 db_tool.py fields, including description/photo_url/
# opening_hours) AND the entire candidate_items map was sent alongside it -
# the same data, twice, at full width. build_day_plan gets its real item
# data from the tool call it makes, not from this prompt, so trimming here
# costs the planner nothing it actually uses.
_PLANNER_FIELDS = ("id", "name", "tags", "lat", "lon", "price_level", "rating")


def _strip(items: list[dict]) -> list[dict]:
    return [{k: item[k] for k in _PLANNER_FIELDS if k in item} for item in items]


def assemble_planner_days(
    trace: list[TraceStep], llm_days: list[ItineraryDay],
) -> tuple[list[ItineraryDay], list[str], list[int]]:
    """Rebuilds the final itinerary from the planner's own build_day_plan
    tool calls instead of trusting the model's finalize step to copy that
    output verbatim into its structured answer - live-found (fallback
    investigation, 2026-09-25) that it reliably didn't: real repaired plans
    showed the same hotel twice in a day (no_duplicates), every day's cost
    set to the whole-trip total (cost_consistent), and items ending after
    the curfew (day_ends_by_curfew) - all three values build_day_plan
    itself already returns correctly. Since that tool is deterministic
    (app/core/itinerary.py), reading its own observation is strictly more
    trustworthy than the model's transcription of it.

    Keyed by the `day` argument each build_day_plan call was made with (the
    LAST call per day wins, matching a real re-plan-that-day pattern); a day
    with no such observation - the model ran out of ReAct steps before
    reaching it (reason 3/4 in the same investigation) - falls back to
    whatever the model's own finalize output said for that day, and is
    reported as a warning so the caller can log/surface it rather than
    silently trusting an unbacked day.

    Also returns the day numbers that fell back this way ("unbacked") - the
    third tuple element. fill_missing_days() (2026-09-26) treats these the
    same as a genuinely missing day: an unbacked day is precisely the one
    build_day_plan never actually checked, so it's also the one MOST likely
    to fail output_validator's L2 rules - and today, a day this function had
    to fall back for still sailed through to validation completely
    unchecked, unlike an entirely absent day (which fill_missing_days always
    rebuilt). Callers that don't need this (existing tests) can ignore it."""
    by_day: dict[int, tuple[dict, dict]] = {}   # day -> (call.args, call.observation)
    for step in trace:
        for call in step.tool_calls:
            if call.tool != "build_day_plan" or call.error:
                continue
            day_num = call.args.get("day")
            if isinstance(day_num, int) and isinstance(call.observation, dict):
                by_day[day_num] = (call.args, call.observation)

    llm_by_day = {d.day: d for d in llm_days}
    warnings: list[str] = []
    unbacked: list[int] = []
    assembled: list[ItineraryDay] = []

    for day_num in sorted(set(by_day) | set(llm_by_day)):
        llm_day = llm_by_day.get(day_num)
        observed = by_day.get(day_num)
        if observed is not None:
            args, obs = observed
            try:
                # obs["date"] (Part 5, server-side enforcement) is the real,
                # server-derived date the build_day_plan tool computed from
                # day/start_date, not whatever the model happened to pass or
                # claim - prefer it when present. Older traces (and this
                # module's own tests) never had that field, so this falls
                # through to the pre-existing llm_day/args priority exactly
                # as before when it's absent.
                date = obs.get("date") or (llm_day.date if llm_day else None) or args.get("date", "")
                assembled.append(ItineraryDay(
                    day=day_num,
                    date=date,
                    theme=(llm_day.theme if llm_day else ""),
                    items=obs.get("items", []),
                    day_cost=obs.get("day_cost", 0.0),
                ))
                continue
            except Exception as e:
                warnings.append(f"day {day_num}: build_day_plan observation failed to validate ({e}), using the model's own version")
        if llm_day is not None:
            assembled.append(llm_day)
            unbacked.append(day_num)
            warnings.append(f"day {day_num}: no build_day_plan observation, used the model's own (unverified) day - will be rebuilt deterministically")
        else:
            warnings.append(f"day {day_num}: no build_day_plan observation and no model output - day dropped")

    # Cross-day repeats: the model can issue several build_day_plan calls in
    # ONE turn (executed concurrently), so no call sees what the others
    # scheduled and the same attraction can land on two days (live-found
    # 2026-09-30: Bembewa on days 1 and 2 of a Hambantota trip). A later day
    # repeating an earlier day's attraction is rebuilt the same way as an
    # unbacked day - fill_missing_days excludes the earlier days' stops.
    seen: set[str] = set()
    for day in sorted(assembled, key=lambda d: d.day):
        ids = {i.listing_id for i in day.items if i.type == "attraction" and i.listing_id}
        if ids & seen and day.day not in unbacked:
            unbacked.append(day.day)
            warnings.append(f"day {day.day}: repeated an attraction from an earlier day - rebuilt")
        seen |= ids

    return assembled, warnings, unbacked


def build_planner_human_message(state: TripState) -> str:
    return json.dumps({
        "trip_context": state.trip_context or {},
        "hotels": _strip(state.hotels),
        "restaurants": _strip(state.restaurants),
        "attractions": _strip(state.attractions),
        "events": _strip(state.events),
        "budget": state.budget,
        "travelers": state.travelers,
        "duration_days": state.duration_days,
        "pace": state.pace,
        "pace_items_per_day": resolve_items_per_day(state),
    })


def enforce_budget_notes(budget_notes: Optional[str], estimated_cost: float, budget: Optional[float]) -> Optional[str]:
    """output_validator.py's budget_honest rule requires a non-null
    budget_notes whenever estimated_cost exceeds budget - PLANNER_SYSTEM_PROMPT's
    RULE 6 already asks the model for this, but doesn't always get it
    (live-found, fallback investigation 2026-09-25). When that happens the
    model didn't get anything about the PLAN wrong, only forgot to narrate
    the one honest sentence the rule requires - filling it here removes an
    entire class of otherwise-unnecessary repair round trips for a failure
    that was never really about the itinerary's shape."""
    if budget is None or estimated_cost <= budget or budget_notes:
        return budget_notes
    over_by = estimated_cost - budget
    return (
        f"This plan comes to {estimated_cost:,.0f} LKR, about {over_by:,.0f} LKR over "
        f"your {budget:,.0f} LKR budget."
    )


def fill_missing_days(
    state: TripState, assembled_days: list[ItineraryDay], cost_table: CostReferenceTable,
    force_rebuild_days: Optional[list[int]] = None,
) -> tuple[list[ItineraryDay], list[str]]:
    """Part 5 (server-side enforcement): assemble_planner_days() already
    falls back to the model's own (unverified) day, or drops the day
    entirely, when the model never reached build_day_plan for it within its
    turn budget (live-found, fallback investigation 2026-09-25 - a real
    3-day request only got 2 of 3 days built). Neither of those outcomes can
    pass output_validator's day_count/days_have_items - which sends a plan
    that's otherwise entirely fine to a full second LLM repair call, or all
    the way to the fallback planner, over ONE missing day. This builds just
    the missing day(s) with the exact same deterministic tool
    (app/core/itinerary.build_day_plan) the model's own call would have used,
    from the same ranked hotel/restaurant/attraction lists the recommendation
    agent already selected - "what the model's own tool call would have
    produced, had it gotten the turn to make it", not a different code path.
    Days the model DID reach are never touched.

    `force_rebuild_days` (2026-09-26): day numbers to rebuild the SAME way
    even though they're technically present in `assembled_days` - used for
    two distinct callers, both really the same problem ("a day sailed
    through to validation without ever being checked by the deterministic
    tool that enforces the real business rules"): assemble_planner_days()'s
    own `unbacked` return value (the model's day was accepted with no
    build_day_plan observation behind it at all), and
    app/core/orchestrator.py's `_repair_node` fast path (a day whose
    observation WAS real but still failed a specific, day-scoped
    output_validator rule - see output_validator.day_scoped_repair_target).
    Treated identically to a genuinely missing day below: excluded from
    `have_days` up front, so it flows through the exact same "missing"
    branch, with the exact same fresh-candidate/anti-duplication logic."""
    duration_days = state.duration_days or len(assembled_days) or 1
    force_rebuild = set(force_rebuild_days or [])
    kept_days = [d for d in assembled_days if d.day not in force_rebuild]
    have_days = {d.day for d in kept_days}
    missing = [d for d in range(1, duration_days + 1) if d not in have_days]
    if not missing:
        return assembled_days, []

    day_ctx = resolve_day_context(state)
    district_id = (state.trip_context or {}).get("district_id")
    items_per_day = day_ctx.expected_items_per_day
    matrix = TravelMatrix()

    hotels, restaurants, attractions = state.hotels, state.restaurants, state.attractions
    wants_restaurants = not category_excluded(state, "restaurant")
    # Cleared, not just unscheduled: build_day_plan draws on these pools to
    # backfill a short day and would put an excluded stop straight back in.
    if not wants_restaurants:
        restaurants = []
    if category_excluded(state, "hotel"):
        hotels = []
    if category_excluded(state, "attraction"):
        attractions = []
    anchor = state.start_location or (hotels[0] if hotels else {"lat": 0.0, "lon": 0.0})
    hotel_anchor = hotels[0] if hotels else anchor

    used_restaurant_ids = {
        i.listing_id for d in kept_days for i in d.items if i.type == "restaurant" and i.listing_id
    }
    used_attraction_ids = {
        i.listing_id for d in kept_days for i in d.items if i.type == "attraction" and i.listing_id
    }

    filled = list(kept_days)
    warnings: list[str] = []
    for day_num in missing:
        day_date = (day_ctx.start_date + timedelta(days=day_num - 1)).isoformat()
        rain_p = day_ctx.per_day_rain_probability.get(day_date, 0.0)
        day_anchor = anchor if day_num == 1 else hotel_anchor

        # The recommendation agent's picks first, then the rest of the raw
        # db_search pool it observed - see itinerary.attraction_candidates
        # for why the picks alone were too thin to plan a day from.
        attraction_pool = [] if category_excluded(state, "attraction") else (state.candidate_pools or {}).get("attraction", [])
        fresh_attractions = attraction_candidates(
            attractions, attraction_pool, used_attraction_ids, hotel_anchor if day_num > 1 else (hotels[0] if hotels else anchor),
        )
        fresh_restaurants = [r for r in restaurants if r.get("id") not in used_restaurant_ids] or restaurants
        cost_lookup = {
            **cost_lookup_for(hotels, "hotel", district_id, cost_table),
            **cost_lookup_for(fresh_restaurants, "restaurant", district_id, cost_table),
            **cost_lookup_for(fresh_attractions, "attraction", district_id, cost_table),
        }
        constraints = DayConstraints.for_day(
            day_num=day_num,
            total_days=duration_days,
            items_target=items_per_day,
            has_hotels=bool(hotels),
            wants_restaurants=wants_restaurants,
            rain_probability=rain_p,
            cost_lookup=cost_lookup,
        )
        selections = DaySelections(hotels=hotels, restaurants=fresh_restaurants, attractions=fresh_attractions)
        plan = build_day_plan(day_num, day_date, day_anchor, selections, constraints, matrix)

        for it in plan.items:
            if it.type == "restaurant" and it.listing_id:
                used_restaurant_ids.add(it.listing_id)
            elif it.type == "attraction" and it.listing_id:
                used_attraction_ids.add(it.listing_id)

        filled.append(ItineraryDay(
            day=plan.day, date=plan.date, theme="",
            items=[
                {"time": i.time, "end_time": i.end_time, "type": i.type, "listing_id": i.listing_id,
                 "name": i.name, "lat": i.lat, "lon": i.lon, "est_cost": i.est_cost,
                 "currency": i.currency, "notes": i.notes}
                for i in plan.items
            ],
            day_cost=plan.day_cost,
        ))
        if day_num in force_rebuild:
            warnings.append(f"day {day_num}: rebuilt deterministically (was present but unverified or failed a day-scoped validation rule)")
        else:
            warnings.append(f"day {day_num}: no build_day_plan observation reachable within the model's turn budget, filled deterministically")

    filled.sort(key=lambda d: d.day)
    logger.warning(f"fill_missing_days: filled {len(missing)} day(s) {missing} deterministically")
    return filled, warnings


# ─────────────────────────── multi-place trips ───────────────────────────

def trip_places(state: TripState) -> list[dict]:
    """trip_context["places"] - every place of the trip, in order. A trip
    resolved before multi-place support (carried in a session) has none;
    it's a one-place trip."""
    return [p for p in ((state.trip_context or {}).get("places") or []) if p.get("district_id")]


def is_multi_place(state: TripState) -> bool:
    return len(trip_places(state)) > 1


def _in_district(items: list[dict], district_id: str) -> list[dict]:
    return [i for i in items if str(i.get("district_id") or "") == str(district_id)]


def _merge_unique(*lists: list[dict]) -> list[dict]:
    seen: set = set()
    out = []
    for items in lists:
        for item in items:
            key = item.get("id")
            if key in seen:
                continue
            seen.add(key)
            out.append(item)
    return out


def build_leg_days(state: TripState, cost_table: CostReferenceTable) -> list[ItineraryDay]:
    """Every day of a multi-place trip, built deterministically leg by leg
    (app/core/legs.py decides which place each day is in).

    Each place gets its own hotel - the recommender's best pick in that
    place's district, else the best raw candidate there - and each day draws
    its stops and meals from its own place, anchored on that place's hotel
    (or centre, on a day trip with no hotel). Same builder and repeat
    avoidance as fill_missing_days, so a day here obeys every rule a
    single-place day does.

    Used instead of the LLM planner for these trips: nothing would check
    that a model put the right hotel on the right day, and the recommender's
    LLM picks already decide WHAT is visited. Also used by the fallback, from
    the raw candidate pools, when the recommender produced nothing."""
    from app.core.legs import plan_legs

    places = trip_places(state)
    day_ctx = resolve_day_context(state)
    duration_days = day_ctx.duration_days
    items_per_day = day_ctx.expected_items_per_day
    matrix = TravelMatrix()
    pools = state.candidate_pools or {}

    wants_restaurants = not category_excluded(state, "restaurant")
    wants_hotels = not category_excluded(state, "hotel")
    wants_attractions = not category_excluded(state, "attraction")

    all_restaurants = _merge_unique(state.restaurants, pools.get("restaurant", [])) if wants_restaurants else []
    all_attraction_pool = pools.get("attraction", []) if wants_attractions else []
    picked_attractions = state.attractions if wants_attractions else []

    used_restaurant_ids: set[str] = set()
    used_attraction_ids: set[str] = set()
    days: list[ItineraryDay] = []

    for leg in plan_legs(len(places), duration_days):
        place = places[leg.place_index]
        district_id = place["district_id"]
        centre = {"lat": place["lat"], "lon": place["lon"]}

        hotels = (
            _merge_unique(_in_district(state.hotels, district_id), _in_district(pools.get("hotel", []), district_id))
            if wants_hotels and leg.nights > 0 else []
        )
        hotels = hotels[:1]
        # A place's own restaurants/attractions first; the whole trip's only
        # when that place has none, rather than serve an empty day.
        restaurants = _in_district(all_restaurants, district_id) or all_restaurants
        leg_picks = _in_district(picked_attractions, district_id)
        leg_pool = _in_district(all_attraction_pool, district_id) or all_attraction_pool

        day_anchor = (state.start_location if leg.day == 1 and state.start_location else None) \
            or (hotels[0] if hotels else centre)
        base = hotels[0] if hotels else centre

        day_date = (day_ctx.start_date + timedelta(days=leg.day - 1)).isoformat()
        fresh_attractions = attraction_candidates(leg_picks, leg_pool, used_attraction_ids, base)
        fresh_restaurants = [r for r in restaurants if r.get("id") not in used_restaurant_ids] or restaurants
        cost_lookup = {
            **cost_lookup_for(hotels, "hotel", district_id, cost_table),
            **cost_lookup_for(fresh_restaurants, "restaurant", district_id, cost_table),
            **cost_lookup_for(fresh_attractions, "attraction", district_id, cost_table),
        }
        constraints = DayConstraints.for_day(
            day_num=leg.day,
            total_days=duration_days,
            items_target=items_per_day,
            has_hotels=bool(hotels),
            wants_restaurants=wants_restaurants,
            rain_probability=day_ctx.per_day_rain_probability.get(day_date, 0.0),
            cost_lookup=cost_lookup,
            checkin=leg.checkin,
            checkout=leg.checkout,
            nights=leg.nights,
        )
        selections = DaySelections(hotels=hotels, restaurants=fresh_restaurants, attractions=fresh_attractions)
        plan = build_day_plan(leg.day, day_date, day_anchor, selections, constraints, matrix)

        for it in plan.items:
            if it.type == "restaurant" and it.listing_id:
                used_restaurant_ids.add(it.listing_id)
            elif it.type == "attraction" and it.listing_id:
                used_attraction_ids.add(it.listing_id)

        days.append(ItineraryDay(
            day=plan.day, date=plan.date,
            theme=re.sub(r"\s+District$", "", place["name"], flags=re.IGNORECASE),
            items=[
                {"time": i.time, "end_time": i.end_time, "type": i.type, "listing_id": i.listing_id,
                 "name": i.name, "lat": i.lat, "lon": i.lon, "est_cost": i.est_cost,
                 "currency": i.currency, "notes": i.notes}
                for i in plan.items
            ],
            day_cost=plan.day_cost,
        ))
    return days
