"""
The deterministic targeted-day rebuild for a shape-only follow-up
(app/core/followup.py's classifier decided this turn qualifies). No LLM
call anywhere in this module - it reuses the exact same deterministic
building blocks the fallback planner does (app/core/scoring.rank(),
app/core/itinerary.build_day_plan()), just scoped to the day(s) the
follow-up actually asked to change. Every day NOT targeted is copied
verbatim from the carried itinerary (app/utils/session_store.py's
"itinerary" carry-over field) - that's what guarantees days 1/3 stay
byte-identical when only day 2 was asked to change
(PROJECT_MASTER_PLAN.md's golden scenario 5).

Being all-deterministic here is a deliberate choice, not laziness: the
recommendation/planner ReAct agents' structured-output reliability against
the current free-tier models is a real, tracked, open problem
(ai-backend/TODO.md) - a feature whose own test explicitly requires
byte-for-byte reproducibility (days 1/3 unchanged) has no business routing
through that unreliable path when a pure-Python rebuild does the job and
is reproducible by construction.
"""
from __future__ import annotations

import logging
from typing import Optional

from app.core.budget import CostReferenceTable, estimate_item_cost
from app.core.itinerary import DayConstraints, DaySelections, build_day_plan
from app.core.scoring import ScoringContext, TravelMatrix, rank
from app.core.state import TripState
from app.tools.db_tool import DataUnavailable, search_listings_by_district
from app.utils.db_pool import get_pool

logger = logging.getLogger(__name__)

_PACE_ITEMS = {"relaxed": 2, "balanced": 3, "packed": 5}

# When "cheaper" is requested, try this price ceiling (1-4 scale) first;
# if hard-filtering to it wipes out every real candidate (real listing data
# in Sri Lanka is sparse on price_level - see PROJECT_MASTER_PLAN.md §7's
# "Restaurant/attraction pricing is not in OSM" risk), fall back to
# unconstrained rather than produce an emptier day - same soft-fallback
# convention app/tools/db_tool.py's interest filter already uses.
_CHEAPER_PRICE_CEILING = 2


async def _fetch_cost_table() -> CostReferenceTable:
    """Duplicated from app/agents/planner_agent.py's identical helper
    rather than shared - see that module's own docstring for why (a small,
    self-contained, independently-degrading read isn't worth the shared-
    module risk for a third caller)."""
    cost_table: CostReferenceTable = {}
    try:
        pool = await get_pool()
    except Exception as e:
        logger.warning(f"followup_replan: get_pool() failed unexpectedly, degrading to empty table: {e}")
        return cost_table
    if pool is None:
        return cost_table
    try:
        rows = await pool.fetch(
            "SELECT district_id, category, price_level, unit, typical_cost, currency FROM cost_reference"
        )
        cost_table = {
            (str(r["district_id"]) if r["district_id"] else None, r["category"], r["price_level"]):
                {"unit": r["unit"], "typical_cost": r["typical_cost"], "currency": r["currency"]}
            for r in rows
        }
    except Exception as e:
        logger.warning(f"followup_replan: cost_reference fetch failed, degrading to empty table: {e}")
    return cost_table


def _cost_lookup_for(items: list[dict], category: str, district_id: Optional[str],
                     cost_table: CostReferenceTable) -> dict[str, float]:
    out = {}
    for i in items:
        est = estimate_item_cost(i, category, district_id, cost_table)
        if est.value is not None:
            out[i["id"]] = est.value
    return out


def _find_hotel_anchor(itinerary: list[dict]) -> Optional[dict]:
    """The trip's hotel doesn't change on a shape-only follow-up (that
    would be a destination/duration change, which routes to a full
    re-plan) - reuse whichever hotel-type item is already in the carried
    itinerary as the anchor for later days, rather than re-selecting one."""
    for day in itinerary:
        for item in day.get("items", []):
            if item.get("type") == "hotel":
                return {"lat": item["lat"], "lon": item["lon"]}
    return None


async def rebuild_targeted_days(state: TripState) -> TripState:
    """Mutates and returns `state`: rebuilds state.followup_target_days (or
    every day, if none were named) using fresh category pools, leaves every
    other day untouched. Degrades to a full re-plan (sets
    state.followup_scope = "full" and leaves state.itinerary alone) when
    there's nothing real to rebuild from - a missing district_id, an empty
    carried itinerary, or a real DB failure - rather than silently doing
    nothing or crashing."""
    ctx = state.trip_context or {}
    district_id = ctx.get("district_id")
    itinerary = list(state.itinerary or [])

    if not itinerary or not district_id:
        logger.info("followup_replan: nothing to rebuild from (no carried itinerary or district_id) - falling back to a full re-plan.")
        state.followup_scope = "full"
        return state

    target_days = set(state.followup_target_days) if state.followup_target_days else {d["day"] for d in itinerary}
    price_ceiling = _CHEAPER_PRICE_CEILING if state.followup_cheaper else None

    # Fetch the FULL pool (no price filter at the SQL level) so _rank()'s
    # own soft-fallback below has something to fall back TO if the cheaper
    # ceiling eliminates every candidate - pre-filtering here would defeat
    # that fallback before it ever runs.
    try:
        hotels_obs = await search_listings_by_district(district_id, "hotel", limit=40)
        restaurants_obs = await search_listings_by_district(district_id, "restaurant", limit=40)
        attractions_obs = await search_listings_by_district(district_id, "attraction", limit=40)
    except DataUnavailable as e:
        logger.warning(f"followup_replan: candidate fetch failed ({e}) - falling back to a full re-plan.")
        state.errors.append(f"targeted_replan_failed: {e}")
        state.followup_scope = "full"
        return state

    cost_table = await _fetch_cost_table()
    matrix = TravelMatrix()
    hotel_anchor = _find_hotel_anchor(itinerary) or {"lat": ctx.get("lat", 0.0), "lon": ctx.get("lon", 0.0)}
    start_anchor = state.start_location or hotel_anchor

    def _rank(observation: dict, category: str, ceiling: Optional[int]) -> list[dict]:
        items = observation.get("items") or []
        cost_lookup = _cost_lookup_for(items, category, district_id, cost_table)
        scoring_ctx = ScoringContext(
            interests=state.interests, anchor=hotel_anchor, matrix=matrix,
            cost_estimates=cost_lookup, must_avoid=state.must_avoid, max_price_level=ceiling,
        )
        ranked = [r.item for r in rank(items, scoring_ctx, category)]
        if not ranked and ceiling is not None:
            # Hard-filtering to the cheaper ceiling wiped out every
            # candidate (sparse real price data) - degrade to unconstrained
            # rather than hand build_day_plan an empty pool.
            scoring_ctx.max_price_level = None
            ranked = [r.item for r in rank(items, scoring_ctx, category)]
        return ranked

    # Hotels get the SAME price ceiling as everything else when "cheaper" is
    # requested - found live (2026-09-03): excluding them on the theory
    # that "the hotel itself isn't what cheaper swaps" was actually wrong.
    # Hotels are the one category with reliable real price data in this
    # dataset (Booking.com prices, via app/data/connectors/booking_prices.py)
    # - restaurants/attractions mostly have no price_level at all (OSM
    # doesn't carry it), so EXCLUDING the category most able to actually
    # produce a cheaper day defeated the whole point for most real requests.
    ranked_hotels = _rank(hotels_obs, "hotel", price_ceiling)
    ranked_restaurants = _rank(restaurants_obs, "restaurant", price_ceiling)
    ranked_attractions = _rank(attractions_obs, "attraction", price_ceiling)

    all_cost_lookup = {
        **_cost_lookup_for(ranked_hotels, "hotel", district_id, cost_table),
        **_cost_lookup_for(ranked_restaurants, "restaurant", district_id, cost_table),
        **_cost_lookup_for(ranked_attractions, "attraction", district_id, cost_table),
    }

    last_day_num = max((d["day"] for d in itinerary), default=1)
    new_days = []
    for day in itinerary:
        day_num = day.get("day")
        if day_num not in target_days:
            new_days.append(day)
            continue

        day_anchor = start_anchor if day_num == 1 else hotel_anchor
        constraints = DayConstraints(
            items_target=_PACE_ITEMS.get(state.pace or "balanced", 3),
            outdoor_tags=frozenset(),
            need_hotel_checkin=(day_num == 1 and bool(ranked_hotels)),
            need_hotel_checkout=(day_num == last_day_num and bool(ranked_hotels)),
            include_lunch=True, include_dinner=True,
            prefer_price_level_max=price_ceiling,
            cost_lookup=all_cost_lookup,
        )
        selections = DaySelections(
            hotels=ranked_hotels, restaurants=ranked_restaurants, attractions=ranked_attractions,
        )
        plan = build_day_plan(day_num, day.get("date", ""), day_anchor, selections, constraints, matrix)
        new_days.append({
            "day": plan.day, "date": plan.date,
            "items": [
                {"time": it.time, "end_time": it.end_time, "type": it.type, "listing_id": it.listing_id,
                 "name": it.name, "lat": it.lat, "lon": it.lon, "est_cost": it.est_cost,
                 "currency": it.currency, "notes": it.notes}
                for it in plan.items
            ],
            "day_cost": plan.day_cost,
        })

    state.itinerary = new_days
    state.estimated_cost = round(sum(d.get("day_cost", 0.0) for d in new_days), 2)
    state.plan_source = "fallback"   # deterministic, same label the zero-LLM planner uses
    return state
