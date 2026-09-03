"""
The zero-LLM fallback planner (decision D3, docs/master_plan/
PROJECT_MASTER_PLAN.md Phase 4). Ships as a first-class component, not an
error handler - this is the single biggest lever on the "90% end-to-end, no
errors" target, because it converts an LLM failure (quota exhausted, model
returned unparseable output twice) into a slightly-plainer *success*
instead of an error.

Two layers, deliberately split:
  - `build_plan_core()` - pure, synchronous, no I/O. Takes everything as
    plain data (PlanningContext + pre-fetched candidates/tags/costs/matrix).
    This is what the determinism test calls directly with a fixed fixture -
    testable with zero mocking, and the only way to actually prove
    bit-reproducibility rather than assume it.
  - `build_plan()` - the async entry point. Fetches whatever build_plan_core
    needs (outdoor tags, cost_reference rows, a travel matrix) and delegates.
    Called by app/core/orchestrator.py's `_fallback_node` on an LLM planner
    failure or a second validation failure.

Response narration here is a template, not a second LLM call by design
(decision D6c) - the fallback path exists specifically for when the LLM is
unavailable, so it cannot depend on the LLM for its own prose.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional

from app.core.budget import (
    CostReferenceTable, budget_per_day, check_budget, estimate_item_cost, feasibility,
)
from app.core.itinerary import DayConstraints, DaySelections, build_day_plan
from app.core.scoring import ScoringContext, TravelMatrix, rank


@dataclass
class PlanningContext:
    """Everything build_plan_core needs, as plain data - the shape
    TripState should grow toward in Phase 6 (district_id/must_avoid/pace
    don't exist on TripState yet)."""
    destination_name: str
    district_id: Optional[str]
    duration_days: int
    start_date: date
    budget: Optional[float]
    travelers: int
    travel_style: Optional[str]
    interests: list[str] = field(default_factory=list)
    must_avoid: list[str] = field(default_factory=list)
    pace_items_per_day: int = 3
    start_location: Optional[dict] = None       # {"lat":..., "lon":...} - day 1's anchor before check-in
    per_day_rain_probability: dict[str, float] = field(default_factory=dict)   # "YYYY-MM-DD" -> 0..1
    disaster: Optional[dict] = None
    max_price_level: Optional[int] = None


@dataclass
class FallbackPlanResult:
    itinerary: list[dict]           # [{"day":, "date":, "items":[...], "day_cost":}]
    estimated_cost: float
    currency: str
    budget_notes: Optional[str]
    final_response: str
    plan_source: str = "fallback"


RAIN_THRESHOLD = 0.6   # matches DETERMINISM_AND_VALIDATION.md §5's weather_respect validator rule


def _cost_lookup_for(items: list[dict], category: str, district_id: Optional[str],
                     cost_table: CostReferenceTable) -> dict[str, float]:
    out = {}
    for i in items:
        est = estimate_item_cost(i, category, district_id, cost_table)
        if est.value is not None:
            out[i["id"]] = est.value
    return out


def build_plan_core(
    ctx: PlanningContext,
    candidate_hotels: list[dict],
    candidate_restaurants: list[dict],
    candidate_attractions: list[dict],
    candidate_events: list[dict],
    outdoor_tags: frozenset[str],
    cost_table: CostReferenceTable,
    matrix: Optional[TravelMatrix] = None,
) -> FallbackPlanResult:
    """Pure and synchronous - no network, no database, no LLM. Every input
    is plain data. Called directly by the determinism test with a fixed
    fixture; called by build_plan() below with live-fetched data."""
    matrix = matrix or TravelMatrix()

    budget_per_day_by_category = budget_per_day(
        ctx.budget, ctx.duration_days, ctx.travel_style, ctx.travelers,
    )

    # Rank once per category, over the whole candidate pool - the hotel
    # choice then anchors every subsequent day's proximity scoring.
    anchor = ctx.start_location or (candidate_hotels[0] if candidate_hotels else {"lat": 0.0, "lon": 0.0})

    def _rank(items: list[dict], category: str) -> list[dict]:
        cost_lookup = _cost_lookup_for(items, category, ctx.district_id, cost_table)
        scoring_ctx = ScoringContext(
            interests=ctx.interests, anchor=anchor, matrix=matrix,
            budget_per_day=budget_per_day_by_category, cost_estimates=cost_lookup,
            must_avoid=ctx.must_avoid, disaster=ctx.disaster, max_price_level=ctx.max_price_level,
        )
        return [r.item for r in rank(items, scoring_ctx, category)]

    ranked_hotels = _rank(candidate_hotels, "hotel")
    ranked_restaurants = _rank(candidate_restaurants, "restaurant")
    ranked_attractions = _rank(candidate_attractions, "attraction")
    # events currently unused by build_day_plan directly - reserved for
    # Phase 6's ReAct planner, which can weave them into a day's theme.

    feas = feasibility(
        ranked_hotels, ranked_restaurants, ranked_attractions,
        ctx.duration_days, ctx.budget, ctx.district_id, cost_table,
    )

    all_cost_lookup = {
        **_cost_lookup_for(ranked_hotels, "hotel", ctx.district_id, cost_table),
        **_cost_lookup_for(ranked_restaurants, "restaurant", ctx.district_id, cost_table),
        **_cost_lookup_for(ranked_attractions, "attraction", ctx.district_id, cost_table),
    }

    days: list[dict] = []
    day_costs_for_budget_check: list[dict] = []
    hotel_anchor = ranked_hotels[0] if ranked_hotels else anchor

    # Attractions/restaurants used on an EARLIER day are excluded from later
    # days' candidate lists - found live (2026-09-03, real demo run): with
    # 40+ real attractions available for the district, every day was still
    # showing the identical top-N attractions, because every day picked
    # fresh from the SAME full ranked list with no memory of what earlier
    # days already used. Hotels are deliberately NOT deduplicated this way -
    # staying at the same hotel for the whole trip is correct, not a bug.
    # A day that runs out of fresh candidates (a long trip, or a genuinely
    # small real pool) falls back to the full ranked list rather than
    # serving an emptier day - reuse is the correct degrade, not a crash.
    used_attraction_ids: set[str] = set()
    used_restaurant_ids: set[str] = set()

    for day_num in range(1, ctx.duration_days + 1):
        day_date = ctx.start_date + timedelta(days=day_num - 1)
        day_date_str = day_date.isoformat()
        rain_p = ctx.per_day_rain_probability.get(day_date_str, 0.0)

        day_anchor = anchor if day_num == 1 else hotel_anchor

        fresh_attractions = [a for a in ranked_attractions if a["id"] not in used_attraction_ids]
        fresh_restaurants = [r for r in ranked_restaurants if r["id"] not in used_restaurant_ids]

        constraints = DayConstraints(
            items_target=ctx.pace_items_per_day,
            exclude_outdoor=(rain_p >= RAIN_THRESHOLD),
            outdoor_tags=outdoor_tags,
            need_hotel_checkin=(day_num == 1 and bool(ranked_hotels)),
            need_hotel_checkout=(day_num == ctx.duration_days and bool(ranked_hotels)),
            include_lunch=True,
            include_dinner=True,
            prefer_price_level_max=ctx.max_price_level,
            cost_lookup=all_cost_lookup,
        )
        selections = DaySelections(
            hotels=ranked_hotels,
            restaurants=fresh_restaurants or ranked_restaurants,
            attractions=fresh_attractions or ranked_attractions,
        )
        plan = build_day_plan(day_num, day_date_str, day_anchor, selections, constraints, matrix)

        for it in plan.items:
            if it.type == "attraction" and it.listing_id:
                used_attraction_ids.add(it.listing_id)
            elif it.type == "restaurant" and it.listing_id:
                used_restaurant_ids.add(it.listing_id)

        days.append({
            "day": plan.day, "date": plan.date,
            "items": [
                {"time": it.time, "end_time": it.end_time, "type": it.type, "listing_id": it.listing_id,
                 "name": it.name, "lat": it.lat, "lon": it.lon, "est_cost": it.est_cost,
                 "currency": it.currency, "notes": it.notes}
                for it in plan.items
            ],
            "day_cost": plan.day_cost,
        })
        day_costs_for_budget_check.append({"total": plan.day_cost})

    estimated_cost = round(sum(d["day_cost"] for d in days), 2)
    budget_check = check_budget(day_costs_for_budget_check, ctx.budget, feas.unknown_cost_items)

    budget_notes = None
    if not feas.feasible:
        budget_notes = (
            f"Even the most affordable options come to an estimated {feas.cheapest_total:,.0f} LKR, "
            f"which is {feas.shortfall:,.0f} LKR over the stated budget."
        )
    elif not budget_check.feasible:
        budget_notes = (
            f"Estimated cost is {budget_check.total:,.0f} LKR, "
            f"{budget_check.over_by:,.0f} LKR over the {ctx.budget:,.0f} LKR budget."
        )
    if budget_check.unknown_cost_items:
        note = f"{len(budget_check.unknown_cost_items)} item(s) had no price data and are excluded from the total."
        budget_notes = f"{budget_notes} {note}" if budget_notes else note

    final_response = (
        f"Here's your trip plan for {ctx.destination_name}: {ctx.duration_days} day(s) planned, "
        f"estimated cost {estimated_cost:,.0f} LKR."
    )
    if budget_notes:
        final_response += f"\n\nBudget note: {budget_notes}"

    return FallbackPlanResult(
        itinerary=days, estimated_cost=estimated_cost, currency="LKR",
        budget_notes=budget_notes, final_response=final_response,
    )


# ─────────────────────────── async entry point ───────────────────────────

async def build_plan(
    ctx: PlanningContext,
    candidate_hotels: list[dict],
    candidate_restaurants: list[dict],
    candidate_attractions: list[dict],
    candidate_events: list[dict],
) -> FallbackPlanResult:
    """Fetches what build_plan_core needs, then delegates. Called by
    app/core/orchestrator.py's `_fallback_node` (AGENT_ARCHITECTURE.md §1)."""
    from app.utils.db_pool import get_pool

    outdoor_tags: frozenset[str] = frozenset()
    cost_table: CostReferenceTable = {}

    pool = await get_pool()
    if pool is not None:
        try:
            rows = await pool.fetch("SELECT tag FROM tag_vocabulary WHERE is_outdoor = true")
            outdoor_tags = frozenset(r["tag"] for r in rows)
        except Exception:
            pass   # fallback planner degrades further, not crashes - empty set just means no outdoor filtering
        try:
            rows = await pool.fetch(
                "SELECT district_id, category, price_level, unit, typical_cost, currency FROM cost_reference"
            )
            cost_table = {
                (str(r["district_id"]) if r["district_id"] else None, r["category"], r["price_level"]):
                    {"unit": r["unit"], "typical_cost": r["typical_cost"], "currency": r["currency"]}
                for r in rows
            }
        except Exception:
            pass

    # Travel matrix: build_day_plan/scoring fall back to haversine per-pair
    # automatically when the matrix has no entry, so an empty TravelMatrix
    # here is a correct, if less precise, degradation - not a crash.
    matrix = TravelMatrix()

    return build_plan_core(
        ctx, candidate_hotels, candidate_restaurants, candidate_attractions, candidate_events,
        outdoor_tags, cost_table, matrix,
    )
