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

# The price band to assume when a listing carries none of its own, per
# category. This is NOT a single number, and getting that wrong was a real
# defect: assuming the mid band everywhere charged 1,500 LKR to enter every
# attraction, and 336 of 339 verified attractions have no price_level. Beaches,
# viewpoints, the Galle Fort ramparts and most temples are free to visit in Sri
# Lanka, so the budget was inventing charges for them and the tracker's totals
# could not be trusted.
#
#   attraction -> band 1, which cost_reference prices at 0.00: free is the
#                 correct default here, and a budget must not invent a fee.
#                 It understates genuinely ticketed sites (Sigiriya, the
#                 national museums), which _assumed_cost_note surfaces rather
#                 than hiding.
#   restaurant -> band 2: a meal is never free, so assuming so would be worse
#                 than assuming a typical one.
#   hotel      -> band 2: same reasoning; 665 of them carry a real Booking
#                 price anyway and never reach this fallback.
_ASSUMED_PRICE_LEVEL_BY_CATEGORY = {
    "attraction": 1,
    "restaurant": 2,
    "hotel": 2,
    "event": 2,
}
_DEFAULT_ASSUMED_PRICE_LEVEL = 2

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
    basis: str   # "exact" | "reference" | "national" | "assumed" | "unknown"


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

    # Nothing knows this item's price band. OSM, which supplies almost every
    # listing, records price_level for well under 1% of restaurants and
    # attractions - so without this step a real itinerary prices only its
    # hotel and reports every meal and entry fee as "no price data". That
    # makes the budget total meaningless, the budget-feasibility check
    # unable to fail, and "make it cheaper" a no-op, because the only
    # priced line is the one the user cannot drop.
    #
    # Assuming the mid band is an estimate, not a measurement, and it is
    # labelled "assumed" so callers can still tell it apart from a real
    # price. It is deliberately NOT written back to travel_listing:
    # price_level stays NULL because we genuinely do not know it, and
    # inventing catalogue data to make a total look tidy is the failure
    # mode the grounding checks exist to prevent.
    assumed_level = _ASSUMED_PRICE_LEVEL_BY_CATEGORY.get(category, _DEFAULT_ASSUMED_PRICE_LEVEL)
    row = cost_table.get((district_id, category, assumed_level)) or         cost_table.get((None, category, assumed_level))
    if row is not None:
        return CostEstimate(float(row["typical_cost"]), row.get("currency", "LKR"), "assumed")

    return CostEstimate(None, "LKR", "unknown")


def cost_lookup_for(items: list[dict], category: str, district_id: Optional[str],
                    cost_table: CostReferenceTable) -> dict[str, float]:
    """id -> real recomputed cost, for every item that resolves to a real
    value. De-duplicated (itinerary-quality/token-reduction pass, Part 6) -
    this was a byte-identical private copy in both app/core/fallback.py and
    app/core/followup_replan.py, with no comment justifying the duplication
    (unlike this module's `_fetch_cost_table`-style helpers, which ARE
    deliberately duplicated per caller and say so)."""
    out: dict[str, float] = {}
    for item in items:
        est = estimate_item_cost(item, category, district_id, cost_table)
        if est.value is not None:
            out[item["id"]] = est.value
    return out


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

    # Hotel cost is charged once, for the WHOLE stay, at check-in -
    # app/core/itinerary.py's build_day_plan bills nightly_rate * hotel_nights
    # on day 1 and emits check-out as a free closing bookend, never a second
    # charge. Multiplying by duration_days here used to overstate the true
    # floor for any trip that wasn't exactly 2 days - live-found 2026-09-06:
    # a 3-day Kandy trip reported "even the cheapest options come to 118,517
    # LKR" while the actual delivered plan cost only 79,011 LKR, a floor that
    # was literally higher than reality. That was first "fixed" by charging
    # hotel_cost a flat 2x regardless of duration - which matched the
    # 2-day case in front of it at the time, but the real quantity was never
    # "2 emits", it's nights stayed: a flat 2x instead UNDERSTATES the floor
    # for any trip longer than 2 days (live-found 2026-09-26: a 1->5 day
    # follow-up left this floor at 2 nights' worth of hotel cost while the
    # actual plan billed 4). hotel_nights = duration_days - 1 is the same
    # quantity build_day_plan itself uses (see planner_shared.py's
    # fill_missing_days and orchestrator.py's _fallback_node).
    hotel_nights = max(duration_days - 1, 0) if hotels else 0
    cheapest_total = (hotel_cost * hotel_nights) + (meal_cost * 2 * duration_days)
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


def compose_budget_notes(feas: Feasibility, budget_check: BudgetCheck, budget: Optional[float]) -> Optional[str]:
    """Shared budget_notes narration (itinerary-quality/token-reduction pass,
    de-duplication). Was inlined only in fallback.py's fresh-plan path;
    app/core/followup_replan.py's targeted-rebuild path never computed
    budget_notes at all, so a rebuilt itinerary (e.g. "make it 2 days
    instead") kept showing the FIRST plan's stale note even after the real
    cost changed - live-found 2026-09-24: a 2-day rebuild costing 39,506 LKR
    still displayed "19,011 LKR over budget", a number computed for the
    original 3-day, 79,011 LKR plan."""
    budget_notes = None
    if not feas.feasible:
        budget_notes = (
            f"Even the most affordable options come to an estimated {feas.cheapest_total:,.0f} LKR, "
            f"which is {feas.shortfall:,.0f} LKR over the stated budget."
        )
    elif not budget_check.feasible:
        budget_notes = (
            f"Estimated cost is {budget_check.total:,.0f} LKR, "
            f"{budget_check.over_by:,.0f} LKR over the {(budget or 0.0):,.0f} LKR budget."
        )
    if budget_check.unknown_cost_items:
        note = f"{len(budget_check.unknown_cost_items)} item(s) had no price data and are excluded from the total."
        budget_notes = f"{budget_notes} {note}" if budget_notes else note
    return budget_notes


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
