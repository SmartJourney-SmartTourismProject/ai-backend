"""
The budget engine (docs/master_plan/DETERMINISM_AND_VALIDATION.md §7,
PROJECT_MASTER_PLAN.md Phase 4). Pure functions, no I/O - `cost_reference`
data is pre-fetched into a plain dict by the caller (CostReferenceTable),
same pattern as app/core/scoring.py's TravelMatrix.

Never silently assumes zero for a missing price. A plan that looks
affordable because three items had no price data is worse than one that
says so explicitly - unknown-cost items are tracked separately and
surfaced in budget_notes, not folded into the total as 0.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

CostReferenceTable = dict[tuple[Optional[str], str, int], dict]   # (district_id, category, price_level) -> {unit, typical_cost, currency}

# Base allocation, adjusted by travel_style. One dict, tunable in one place.
DEFAULT_SPLIT = {"stay": 0.40, "food": 0.25, "activity": 0.20, "transport": 0.15}
SPLIT_BY_STYLE = {
    "budget":  {"stay": 0.32, "food": 0.26, "activity": 0.26, "transport": 0.16},
    "luxury":  {"stay": 0.50, "food": 0.25, "activity": 0.15, "transport": 0.10},
}

_CATEGORY_TO_SPLIT_KEY = {"hotel": "stay", "restaurant": "food", "attraction": "activity", "event": "activity"}


def budget_split(travel_style: Optional[str]) -> dict[str, float]:
    return dict(SPLIT_BY_STYLE.get(travel_style or "", DEFAULT_SPLIT))


def budget_per_day(budget: Optional[float], duration_days: int, travel_style: Optional[str],
                   travelers: int = 1, per_person: bool = False) -> dict[str, Optional[float]]:
    """{"hotel": ..., "restaurant": ..., "attraction": ..., "event": ..., "transport": ...} in LKR/day.
    All None if budget is None (unconstrained trip)."""
    split = budget_split(travel_style)
    if budget is None or duration_days <= 0:
        return {k: None for k in ("hotel", "restaurant", "attraction", "event", "transport")}

    divisor = duration_days * (travelers if per_person else 1)
    per_day = {split_key: budget * frac / divisor for split_key, frac in split.items()}
    return {
        "hotel": per_day["stay"],
        "restaurant": per_day["food"],
        "attraction": per_day["activity"],
        "event": per_day["activity"],
        "transport": per_day["transport"],
    }


@dataclass
class CostEstimate:
    value: Optional[float]
    currency: str
    basis: str   # "exact" | "reference" | "national" | "unknown"


def estimate_item_cost(item: dict, category: str, district_id: Optional[str],
                       cost_table: CostReferenceTable) -> CostEstimate:
    """Precedence (docs/master_plan/DETERMINISM_AND_VALIDATION.md §7):
      1. price_per_night (hotels, from Booking)      -> exact
      2. price_min (events)                           -> exact
      3. cost_reference[district][category][level]    -> reference
      4. cost_reference[None][category][level]         -> national
      5. nothing available                             -> unknown, value=None

    `category` is required, not read off the item - no dict this codebase
    passes around (app/tools/db_tool.py's _row_to_listing_dict output,
    real or test fixture) carries its own category key; the caller always
    knows it already, from which db_tool function it called
    (get_hotels/get_restaurants/...) or which list it's iterating. A prior
    version tried `item.get("category")` and silently priced everything as
    "unknown" - every real item's est_cost stayed 0.0, and budget
    feasibility checks never caught even a wildly impossible budget,
    because "nothing has a cost" reads as "everything is free"."""
    if item.get("price_per_night") is not None:
        return CostEstimate(float(item["price_per_night"]), item.get("currency", "LKR"), "exact")
    if item.get("price_min") is not None:
        return CostEstimate(float(item["price_min"]), item.get("currency", "LKR"), "exact")

    level = item.get("price_level")
    if level is not None:
        row = cost_table.get((district_id, category, level))
        if row is not None:
            return CostEstimate(float(row["typical_cost"]), row.get("currency", "LKR"), "reference")
        row = cost_table.get((None, category, level))
        if row is not None:
            return CostEstimate(float(row["typical_cost"]), row.get("currency", "LKR"), "national")

    return CostEstimate(None, "LKR", "unknown")


@dataclass
class Feasibility:
    feasible: bool
    cheapest_total: float
    shortfall: float
    unknown_cost_items: list[str] = field(default_factory=list)


def _cheapest_of(items: list[dict], category: str, district_id: Optional[str],
                 cost_table: CostReferenceTable) -> tuple[float, list[str]]:
    """Cheapest single item's cost, plus the ids of any items with no cost
    data at all (never assumed to be free)."""
    unknown = []
    costs = []
    for i in items:
        est = estimate_item_cost(i, category, district_id, cost_table)
        if est.value is None:
            unknown.append(i["id"])
        else:
            costs.append(est.value)
    return (min(costs) if costs else 0.0), unknown


def feasibility(
    hotels: list[dict], restaurants: list[dict], attractions: list[dict],
    duration_days: int, budget: Optional[float], district_id: Optional[str],
    cost_table: CostReferenceTable,
) -> Feasibility:
    """Checked BEFORE planning, not after - the minimum realistic cost is
    known up front, so budget_notes can say so before the planner wastes
    effort building something that was never going to fit."""
    hotel_cost, u1 = _cheapest_of(hotels, "hotel", district_id, cost_table)
    meal_cost, u2 = _cheapest_of(restaurants, "restaurant", district_id, cost_table)
    # Attractions can legitimately be free (price_level=1, typical_cost=0 in
    # cost_reference's seed data) - no special-casing needed, the estimate
    # chain already returns 0.0 for those.

    cheapest_total = (hotel_cost * duration_days) + (meal_cost * 2 * duration_days)
    unknown = u1 + u2

    return Feasibility(
        feasible=(budget is None or cheapest_total <= budget),
        cheapest_total=round(cheapest_total, 2),
        shortfall=round(max(0.0, cheapest_total - (budget or 0.0)), 2),
        unknown_cost_items=unknown,
    )


@dataclass
class SwapSuggestion:
    replace: str
    with_: str
    saves: float
    score_delta: float


@dataclass
class BudgetCheck:
    feasible: bool
    total: float
    over_by: float
    per_category: dict[str, float]
    unknown_cost_items: list[str] = field(default_factory=list)
    cheapest_swaps: list[SwapSuggestion] = field(default_factory=list)


def check_budget(
    day_costs: list[dict],   # [{"hotel": cost, "restaurant": cost, "attraction": cost, ...}, ...] per day
    budget: Optional[float],
    unknown_cost_items: Optional[list[str]] = None,
) -> BudgetCheck:
    """Sums per-category costs across all days. Swap suggestions are the
    caller's job (app/core/itinerary.py has the ranked alternatives; this
    module only knows totals) - cheapest_swaps stays empty here and is
    filled in by whoever calls this with real alternatives available."""
    per_category: dict[str, float] = {}
    for day in day_costs:
        for cat, val in day.items():
            per_category[cat] = per_category.get(cat, 0.0) + val
    total = round(sum(per_category.values()), 2)

    return BudgetCheck(
        feasible=(budget is None or total <= budget),
        total=total,
        over_by=round(max(0.0, total - (budget or 0.0)), 2),
        per_category={k: round(v, 2) for k, v in per_category.items()},
        unknown_cost_items=unknown_cost_items or [],
    )
