"""
The tool catalog (docs/master_plan/AGENT_ARCHITECTURE.md §4) - wraps every
existing tool function as a `StructuredTool` so app/core/react.py's executor
can bind them to an LLM via bind_tools(). One Pydantic args schema per tool;
the underlying functions themselves (app/tools/*.py) are untouched - this
module only adds the LangChain-facing schema/description layer on top.

Grouped into per-agent lists so no agent ever sees more than 6 tools -
Gemini's tool-selection quality degrades with large tool lists
(AGENT_ARCHITECTURE.md §4's own note).

The former "context tools" (resolve_place/resolve_district/
resolve_start_location/get_calendar_free_days/get_weather/
get_disaster_info) are gone from here - context resolution became
deterministic (app/core/context_resolver.py, "C2" in the itinerary-quality/
token-reduction pass) and calls those same underlying app/tools/* functions
directly, with no LangChain tool-schema wrapper needed since there's no LLM
choosing whether/how to call them anymore.

`score_candidates`/`estimate_costs`/`build_day_plan` are id-based ("B1" in
the same pass), not full-dict-based: every db_search_listings call this
request makes is accumulated into an in-memory item store keyed by id (see
build_data_tools()'s item_store / planner_agent.py's item_store built from
state.hotels/etc), so the model passes back a handful of listing_ids
instead of re-emitting entire candidate rows as output tokens just to name
which ones it means. This was the single largest model-generated payload in
the system - up to 15 rows x ~15 fields, once per category, per the
recommendation prompt's own one-call-per-category rule.
"""
from __future__ import annotations

from typing import Optional

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from app.tools.db_tool import search_listings_by_district
from app.core.scoring import ScoringContext, TravelMatrix, rank
from app.core.budget import CostReferenceTable, estimate_item_cost, check_budget as _check_budget_pure
from app.core.itinerary import DayConstraints, DaySelections, build_day_plan as _build_day_plan_pure


def _resolve_ids(ids: list[str], item_store: dict[str, dict]) -> list[dict]:
    """Every id that isn't in item_store (the model naming one it never
    actually observed) is silently dropped rather than raising - matches
    every other tool's own "never raises, returns what it can" convention;
    app/core/output_validator.py's L1 referential check is what actually
    enforces that every listing_id in the final PlannerOutput came from a
    real observation, not this lookup."""
    return [item_store[i] for i in ids if i in item_store]


# ─────────────────────────── data tools (recommendation) ───────────────────

class _DbSearchListingsArgs(BaseModel):
    district_id: str
    category: str = Field(description="One of: hotel, restaurant, attraction.")
    tags: list[str] = Field(default_factory=list)
    must_avoid: list[str] = Field(default_factory=list)
    max_price_level: Optional[int] = None
    near: Optional[dict] = Field(None, description="{'lat':..., 'lon':..., 'radius_km':...} to restrict by proximity.")
    radius_km: Optional[float] = None
    # O1 (AI_BACKEND_OPTIMIZATION_PLAN.md): was 40 - the agent selects at
    # most 3 hotels / 2xdays restaurants / 3xdays attractions regardless,
    # and score_candidates does the real ordering, so 40 full-width rows per
    # category was pure token overhead, not extra selection quality.
    limit: int = 15


class _ScoreCandidatesArgs(BaseModel):
    listing_ids: list[str] = Field(
        description="ids from a db_search_listings observation earlier in this conversation - "
                     "not full candidate objects.")
    interests: list[str] = Field(default_factory=list)
    anchor: dict = Field(default_factory=dict)
    budget_per_day: Optional[float] = None
    category: str = Field(description="One of: hotel, restaurant, attraction, event.")
    must_avoid: list[str] = Field(default_factory=list)


def build_data_tools() -> tuple[list[StructuredTool], dict[str, dict]]:
    """Per-request factory (mirrors build_planning_tools' own closure
    pattern below) - `item_store` accumulates every item any
    db_search_listings call in THIS request has returned, keyed by id, so
    score_candidates can take listing_ids. Returned alongside the tools so
    a caller (RecommendationAgent) can still read the full pool after the
    ReAct loop finishes, same as it always could from the trace."""
    item_store: dict[str, dict] = {}

    async def _db_search_listings(**kwargs) -> dict:
        try:
            result = await search_listings_by_district(**kwargs)
        except Exception as e:
            return {"error": str(e), "items": [], "total": 0, "truncated": False}
        for item in result.get("items") or []:
            if "id" in item:
                item_store[str(item["id"])] = item
        return result

    def _score_candidates(listing_ids: list[str], interests: list[str], anchor: dict,
                           budget_per_day: Optional[float], category: str, must_avoid: list[str]) -> dict:
        candidates = _resolve_ids(listing_ids, item_store)
        ctx = ScoringContext(
            interests=interests, anchor=anchor, matrix=TravelMatrix(),
            budget_per_day={category: budget_per_day}, must_avoid=must_avoid,
        )
        ranked = rank(candidates, ctx, category)
        return {
            "ranked": [
                {
                    "listing_id": r.item["id"], "rank": i + 1, "score": r.score,
                    "breakdown": {"pref": r.breakdown.pref, "prox": r.breakdown.prox,
                                  "rating": r.breakdown.rating, "cost": r.breakdown.cost},
                }
                for i, r in enumerate(ranked)
            ]
        }

    db_search_listings_tool = StructuredTool.from_function(
        coroutine=_db_search_listings, name="db_search_listings", args_schema=_DbSearchListingsArgs,
        description="Search verified hotels/restaurants/attractions in a district from the real database.",
    )
    # db_search_events removed (AI_BACKEND_OPTIMIZATION_PLAN.md C5): local_event
    # has 0 rows, Ticketmaster returns zero events for Sri Lanka - this was a
    # tool the recommendation agent could waste a ReAct step calling for a
    # result that's always empty. search_events_by_district itself is kept
    # (app/tools/db_tool.py) for when admin-entered events land.
    #
    # travel_matrix removed too (B2, same pass): TravelMatrix.from_matrix_result
    # has always had zero real callers - every planner builds an EMPTY
    # TravelMatrix (scoring.py/itinerary.py fall back to haversine per-pair
    # automatically), so the LLM calling this tool spent a whole ReAct step
    # and a real ORS quota call for a result nothing downstream ever reads.
    score_candidates_tool = StructuredTool.from_function(
        func=_score_candidates, name="score_candidates", args_schema=_ScoreCandidatesArgs,
        description="Deterministically rank candidates (by id, from a db_search_listings observation) "
                     "by preference/proximity/rating/cost. The only legal source of an ordering - "
                     "never reorder its output.",
    )
    return [db_search_listings_tool, score_candidates_tool], item_store


# ─────────────────────────── planning tools (planner) ──────────────────────

class _EstimateCostsArgs(BaseModel):
    listing_ids: list[str] = Field(description="ids of items already known from the recommendation "
                                                "(state.hotels/restaurants/attractions/events) - not full objects.")
    category: str


class _BuildDayPlanArgs(BaseModel):
    day: int
    date: str
    anchor: dict
    hotel_ids: list[str] = Field(default_factory=list)
    attraction_ids: list[str] = Field(default_factory=list)
    items_target: int = 3
    exclude_outdoor: bool = False
    need_hotel_checkin: bool = False
    need_hotel_checkout: bool = False
    prefer_price_level_max: Optional[int] = None
    # outdoor_tags and cost_lookup dropped (B1) - both are pure server
    # state the model had no business carrying: outdoor_tags is the whole
    # tag_vocabulary "is this tag outdoor" list (see
    # build_planning_tools' own fetch below), and cost_lookup used to be
    # transcribed by hand from a prior estimate_costs observation - a
    # mis-copied value there silently broke output_validator.py's
    # cost_recomputes check. Both are now resolved deterministically inside
    # _build_day_plan from the same cost_table/outdoor_tags this closure
    # already has, for every item it actually places.
    #
    # restaurant_ids dropped too (itinerary-quality/token-reduction pass,
    # Part 2 extension) - live-found: the model had no geographic signal
    # when deciding which restaurant_ids to assign to which day, so a real
    # run put a Badulla restaurant on an all-Ella day, ~13km from every
    # other stop. Unlike attractions ("which day do I visit this" is a
    # real judgment call), a restaurant is purely meal-time filler with no
    # day-specific meaning - nearest-available-and-not-yet-used is always
    # the right answer, which is exactly what build_day_plan's own
    # nearest_restaurant() already computes deterministically for the
    # fallback planner. Restaurants are now resolved the same way for
    # every path: from the full pool this request observed, never from a
    # per-day list the model had to partition by hand.


class _CheckBudgetArgs(BaseModel):
    day_costs: list[dict] = Field(description="[{'hotel':..,'restaurant':..,'attraction':..}, ...] per day.")
    budget: Optional[float] = None
    unknown_cost_items: list[str] = Field(default_factory=list)


def _check_budget(day_costs: list[dict], budget: Optional[float] = None,
                   unknown_cost_items: Optional[list[str]] = None) -> dict:
    result = _check_budget_pure(day_costs, budget, unknown_cost_items)
    return {
        "feasible": result.feasible, "total": result.total, "over_by": result.over_by,
        "per_category": result.per_category, "unknown_cost_items": result.unknown_cost_items,
        "cheapest_swaps": [
            {"replace": s.replace, "with": s.with_, "saves": s.saves, "score_delta": s.score_delta}
            for s in result.cheapest_swaps
        ],
    }


def build_planning_tools(
    cost_table: CostReferenceTable,
    item_store: Optional[dict[str, dict]] = None,
    outdoor_tags: frozenset[str] = frozenset(),
    district_id: Optional[str] = None,
) -> list[StructuredTool]:
    """`item_store` (id -> full item dict, built by the caller from
    state.hotels/restaurants/attractions/events - see planner_agent.py) and
    `outdoor_tags`/`district_id` are all per-request data closed over here,
    same reason `cost_table` already was: `estimate_costs` and
    `build_day_plan` need real DB-derived data no LLM call should have to
    carry back and forth. item_store defaults to {} (not None) so a caller
    that doesn't pass one - e.g. an existing test - degrades to "nothing
    resolves" rather than crashing on a NoneType lookup."""
    item_store = item_store or {}
    # Shared ACROSS every build_day_plan call this request makes (not reset
    # per call) - same cross-day dedup convention app/core/fallback.py's
    # build_plan_core already uses for attractions/restaurants, now applied
    # here too so day 2 doesn't repeat day 1's restaurant just because it's
    # still the nearest candidate.
    used_restaurant_ids: set[str] = set()

    def _restaurant_pool() -> list[dict]:
        all_restaurants = [item for item in item_store.values() if item.get("category") == "restaurant"]
        fresh = [r for r in all_restaurants if r["id"] not in used_restaurant_ids]
        # Reuse rather than serve a day with no restaurant candidates at
        # all once every real one is exhausted - same "degrade, don't
        # omit" convention as everywhere else in this codebase.
        return fresh or all_restaurants

    def _cost_lookup_for(items: list[dict], category: str) -> dict[str, float]:
        out = {}
        for item in items:
            est = estimate_item_cost(item, category, district_id, cost_table)
            if est.value is not None:
                out[item["id"]] = est.value
        return out

    def _estimate_costs_impl(listing_ids: list[str], category: str) -> dict:
        items = _resolve_ids(listing_ids, item_store)
        per_item = {}
        subtotal = 0.0
        for item in items:
            est = estimate_item_cost(item, category, district_id, cost_table)
            per_item[item["id"]] = {"value": est.value, "currency": est.currency, "basis": est.basis}
            subtotal += est.value or 0.0
        return {"per_item": per_item, "subtotal": round(subtotal, 2), "currency": "LKR"}

    def _build_day_plan(day: int, date: str, anchor: dict, hotel_ids: list[str],
                         attraction_ids: list[str], items_target: int, exclude_outdoor: bool,
                         need_hotel_checkin: bool, need_hotel_checkout: bool,
                         prefer_price_level_max: Optional[int]) -> dict:
        hotels = _resolve_ids(hotel_ids, item_store)
        attractions = _resolve_ids(attraction_ids, item_store)
        restaurants = _restaurant_pool()
        cost_lookup = {
            **_cost_lookup_for(hotels, "hotel"),
            **_cost_lookup_for(restaurants, "restaurant"),
            **_cost_lookup_for(attractions, "attraction"),
        }
        selections = DaySelections(hotels=hotels, restaurants=restaurants, attractions=attractions)
        constraints = DayConstraints(
            items_target=items_target, exclude_outdoor=exclude_outdoor,
            outdoor_tags=outdoor_tags, need_hotel_checkin=need_hotel_checkin,
            need_hotel_checkout=need_hotel_checkout, prefer_price_level_max=prefer_price_level_max,
            cost_lookup=cost_lookup,
        )
        plan = _build_day_plan_pure(day, date, anchor, selections, constraints)
        for it in plan.items:
            if it.type == "restaurant" and it.listing_id:
                used_restaurant_ids.add(it.listing_id)
        return {
            "items": [
                {"time": it.time, "end_time": it.end_time, "type": it.type, "listing_id": it.listing_id,
                 "name": it.name, "lat": it.lat, "lon": it.lon, "est_cost": it.est_cost,
                 "currency": it.currency, "notes": it.notes}
                for it in plan.items
            ],
            "day_cost": plan.day_cost, "total_km": plan.total_km,
            "total_travel_min": plan.total_travel_min, "dropped": plan.dropped,
        }

    estimate_costs_tool = StructuredTool.from_function(
        func=_estimate_costs_impl, name="estimate_costs", args_schema=_EstimateCostsArgs,
        description="Get real per-item costs (by id) from price data or cost_reference.",
    )
    build_day_plan_tool = StructuredTool.from_function(
        func=_build_day_plan, name="build_day_plan", args_schema=_BuildDayPlanArgs,
        description="Build a fully timed, routed day from ranked selections (by id) and constraints. "
                     "Does all routing/timing/arithmetic/cost lookup - never compute these yourself.",
    )
    check_budget_tool = StructuredTool.from_function(
        func=_check_budget, name="check_budget", args_schema=_CheckBudgetArgs,
        description="Check whether the built days fit the budget; suggests cheapest swaps if not.",
    )
    return [estimate_costs_tool, build_day_plan_tool, check_budget_tool]
