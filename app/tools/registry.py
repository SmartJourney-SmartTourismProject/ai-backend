"""
The tool catalog (docs/master_plan/AGENT_ARCHITECTURE.md §4) - wraps every
existing tool function as a `StructuredTool` so app/core/react.py's executor
can bind them to an LLM via bind_tools(). One Pydantic args schema per tool;
the underlying functions themselves (app/tools/*.py) are untouched - this
module only adds the LangChain-facing schema/description layer on top.

13 tools total, grouped into three per-agent lists so no agent ever sees
more than 6 - Gemini's tool-selection quality degrades with large tool
lists (AGENT_ARCHITECTURE.md §4's own note).
"""
from __future__ import annotations

from typing import Optional

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from app.tools.geo_tool import resolve_district, resolve_place
from app.tools.location_tool import resolve_start_location
from app.tools.calendar_tool import get_free_days
from app.tools.weather_tool import get_weather
from app.tools.disaster_tool import get_disaster_info
from app.tools.db_tool import search_listings_by_district, search_events_by_district
from app.tools.routing_tool import get_travel_matrix, Point
from app.core.scoring import ScoringContext, TravelMatrix, rank
from app.core.budget import CostReferenceTable, estimate_item_cost, check_budget as _check_budget_pure
from app.core.itinerary import DayConstraints, DaySelections, build_day_plan as _build_day_plan_pure


# ─────────────────────────── context tools (orchestrator) ──────────────────

class _ResolvePlaceArgs(BaseModel):
    name: str = Field(description="A place name as the traveler wrote it, e.g. 'Ella' or 'New York'.")


async def _resolve_place(name: str) -> dict:
    result = await resolve_place(name)
    return result or {"error": f"could not resolve '{name}' to any place"}


class _ResolveDistrictArgs(BaseModel):
    lat: float
    lon: float


async def _resolve_district(lat: float, lon: float) -> dict:
    result = await resolve_district(lat, lon)
    return result or {"error": f"no district found near ({lat}, {lon})"}


class _ResolveStartLocationArgs(BaseModel):
    client_gps: Optional[dict] = Field(None, description="{'lat':..., 'lon':...} if the client already sent GPS.")
    client_ip: Optional[str] = None


async def _resolve_start_location(client_gps: Optional[dict] = None, client_ip: Optional[str] = None) -> dict:
    result = await resolve_start_location(client_gps, client_ip)
    return result or {"error": "could not resolve a starting location from gps or ip"}


class _GetCalendarFreeDaysArgs(BaseModel):
    user_id: str
    window_days: int = Field(30, description="How many days ahead to search for free windows.")


async def _get_calendar_free_days(user_id: str, window_days: int = 30) -> dict:
    ranges = await get_free_days(user_id, window_days)
    return {"free_ranges": ranges}


class _GetWeatherArgs(BaseModel):
    lat: float
    lon: float
    dates: list[str] = Field(description="ISO date strings, e.g. ['2026-10-01','2026-10-02'].")


async def _get_weather(lat: float, lon: float, dates: list[str]) -> dict:
    result = await get_weather(lat, lon, dates)
    return result or {"error": "weather unavailable"}


class _GetDisasterInfoArgs(BaseModel):
    lat: float
    lon: float
    radius_km: int = 300


async def _get_disaster_info(lat: float, lon: float, radius_km: int = 300) -> dict:
    return await get_disaster_info(lat, lon, radius_km)


CONTEXT_TOOLS: list[StructuredTool] = [
    StructuredTool.from_function(
        coroutine=_resolve_place, name="resolve_place", args_schema=_ResolvePlaceArgs,
        description="Resolve a place name to coordinates + district_id for a Sri Lankan place, "
                     "or confidence='out_of_country' if it isn't one.",
    ),
    StructuredTool.from_function(
        coroutine=_resolve_district, name="resolve_district", args_schema=_ResolveDistrictArgs,
        description="Resolve a lat/lon point to its district_id, name, and province.",
    ),
    StructuredTool.from_function(
        coroutine=_resolve_start_location, name="resolve_start_location", args_schema=_ResolveStartLocationArgs,
        description="Resolve the traveler's own starting point from GPS or IP.",
    ),
    StructuredTool.from_function(
        coroutine=_get_calendar_free_days, name="get_calendar_free_days", args_schema=_GetCalendarFreeDaysArgs,
        description="Get the traveler's free date ranges from their connected calendar, if any.",
    ),
    StructuredTool.from_function(
        coroutine=_get_weather, name="get_weather", args_schema=_GetWeatherArgs,
        description="Get forecast (temp, condition, rain probability) for specific dates at a point.",
    ),
    StructuredTool.from_function(
        coroutine=_get_disaster_info, name="get_disaster_info", args_schema=_GetDisasterInfoArgs,
        description="Get active hazards (flood, landslide, storm, etc.) near a point.",
    ),
]


# ─────────────────────────── data tools (recommendation) ───────────────────

class _DbSearchListingsArgs(BaseModel):
    district_id: str
    category: str = Field(description="One of: hotel, restaurant, attraction.")
    tags: list[str] = Field(default_factory=list)
    must_avoid: list[str] = Field(default_factory=list)
    max_price_level: Optional[int] = None
    near: Optional[dict] = Field(None, description="{'lat':..., 'lon':..., 'radius_km':...} to restrict by proximity.")
    radius_km: Optional[float] = None
    limit: int = 40


async def _db_search_listings(**kwargs) -> dict:
    try:
        return await search_listings_by_district(**kwargs)
    except Exception as e:
        return {"error": str(e), "items": [], "total": 0, "truncated": False}


class _DbSearchEventsArgs(BaseModel):
    district_id: str
    date_from: str
    date_to: str
    tags: list[str] = Field(default_factory=list)
    limit: int = 20


async def _db_search_events(**kwargs) -> dict:
    try:
        return await search_events_by_district(**kwargs)
    except Exception as e:
        return {"error": str(e), "items": [], "total": 0, "truncated": False}


class _TravelMatrixArgs(BaseModel):
    origins: list[Point]
    destinations: list[Point]


async def _travel_matrix(origins: list[Point], destinations: list[Point]) -> dict:
    return await get_travel_matrix(origins, destinations)


class _ScoreCandidatesArgs(BaseModel):
    candidates: list[dict]
    interests: list[str] = Field(default_factory=list)
    anchor: dict = Field(default_factory=dict)
    budget_per_day: Optional[float] = None
    category: str = Field(description="One of: hotel, restaurant, attraction, event.")
    must_avoid: list[str] = Field(default_factory=list)


def _score_candidates(candidates: list[dict], interests: list[str], anchor: dict,
                       budget_per_day: Optional[float], category: str, must_avoid: list[str]) -> dict:
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


DATA_TOOLS: list[StructuredTool] = [
    StructuredTool.from_function(
        coroutine=_db_search_listings, name="db_search_listings", args_schema=_DbSearchListingsArgs,
        description="Search verified hotels/restaurants/attractions in a district from the real database.",
    ),
    StructuredTool.from_function(
        coroutine=_db_search_events, name="db_search_events", args_schema=_DbSearchEventsArgs,
        description="Search verified local events in a district overlapping a date window.",
    ),
    StructuredTool.from_function(
        coroutine=_travel_matrix, name="travel_matrix", args_schema=_TravelMatrixArgs,
        description="Get real road travel minutes/km between every origin x destination pair, in one call.",
    ),
    StructuredTool.from_function(
        func=_score_candidates, name="score_candidates", args_schema=_ScoreCandidatesArgs,
        description="Deterministically rank candidates by preference/proximity/rating/cost. "
                     "The only legal source of an ordering - never reorder its output.",
    ),
]


# ─────────────────────────── planning tools (planner) ──────────────────────

class _EstimateCostsArgs(BaseModel):
    items: list[dict]
    category: str
    district_id: Optional[str] = None


class _BuildDayPlanArgs(BaseModel):
    day: int
    date: str
    anchor: dict
    hotels: list[dict] = Field(default_factory=list)
    restaurants: list[dict] = Field(default_factory=list)
    attractions: list[dict] = Field(default_factory=list)
    items_target: int = 3
    exclude_outdoor: bool = False
    outdoor_tags: list[str] = Field(default_factory=list)
    need_hotel_checkin: bool = False
    need_hotel_checkout: bool = False
    prefer_price_level_max: Optional[int] = None
    cost_lookup: dict[str, float] = Field(default_factory=dict)


def _build_day_plan(day: int, date: str, anchor: dict, hotels: list[dict], restaurants: list[dict],
                     attractions: list[dict], items_target: int, exclude_outdoor: bool,
                     outdoor_tags: list[str], need_hotel_checkin: bool, need_hotel_checkout: bool,
                     prefer_price_level_max: Optional[int], cost_lookup: dict[str, float]) -> dict:
    selections = DaySelections(hotels=hotels, restaurants=restaurants, attractions=attractions)
    constraints = DayConstraints(
        items_target=items_target, exclude_outdoor=exclude_outdoor,
        outdoor_tags=frozenset(outdoor_tags), need_hotel_checkin=need_hotel_checkin,
        need_hotel_checkout=need_hotel_checkout, prefer_price_level_max=prefer_price_level_max,
        cost_lookup=cost_lookup,
    )
    plan = _build_day_plan_pure(day, date, anchor, selections, constraints)
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


def build_planning_tools(cost_table: CostReferenceTable) -> list[StructuredTool]:
    """`estimate_costs` needs a pre-fetched cost_reference table (real DB
    data, fetched once per request by the planner agent node - see
    app/agents/planner_agent.py) - built here rather than as a bare module
    function so the closure captures that table without a global."""

    def _estimate_costs_impl(items: list[dict], category: str, district_id: Optional[str] = None) -> dict:
        per_item = {}
        subtotal = 0.0
        for item in items:
            est = estimate_item_cost(item, category, district_id, cost_table)
            per_item[item["id"]] = {"value": est.value, "currency": est.currency, "basis": est.basis}
            subtotal += est.value or 0.0
        return {"per_item": per_item, "subtotal": round(subtotal, 2), "currency": "LKR"}

    estimate_costs_tool = StructuredTool.from_function(
        func=_estimate_costs_impl, name="estimate_costs", args_schema=_EstimateCostsArgs,
        description="Get real per-item costs for a list of candidates, from price data or cost_reference.",
    )
    build_day_plan_tool = StructuredTool.from_function(
        func=_build_day_plan, name="build_day_plan", args_schema=_BuildDayPlanArgs,
        description="Build a fully timed, routed day from ranked selections and constraints. "
                     "Does all routing/timing/arithmetic - never compute these yourself.",
    )
    check_budget_tool = StructuredTool.from_function(
        func=_check_budget, name="check_budget", args_schema=_CheckBudgetArgs,
        description="Check whether the built days fit the budget; suggests cheapest swaps if not.",
    )
    travel_matrix_tool = StructuredTool.from_function(
        coroutine=_travel_matrix, name="travel_matrix", args_schema=_TravelMatrixArgs,
        description="Get real road travel minutes/km between every origin x destination pair, in one call.",
    )
    return [estimate_costs_tool, build_day_plan_tool, check_budget_tool, travel_matrix_tool]
