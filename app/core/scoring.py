"""
The deterministic scoring engine - concern #10, in full
(docs/master_plan/DETERMINISM_AND_VALIDATION.md §6, PROJECT_MASTER_PLAN.md
Phase 4).

Pure functions. No I/O. No LLM. Every ranking decision in the system goes
through rank() below - an agent may veto a scored item for a stated reason,
but it may never re-order what this module produces. That's what makes the
system's choices reproducible instead of a model's judgment call.

Items are plain dicts, matching what app/tools/db_tool.py's
_row_to_listing_dict/_row_to_event_dict already return - id, name, tags,
price_level, price_per_night, lat, lon, rating, rating_count, is_verified,
is_active. No attribute-style wrapper class; dict access throughout, same
shape the rest of the codebase already uses.

Distances come from a pre-computed TravelMatrix, not a live call - routing_tool.
get_travel_matrix() is async I/O and must be awaited exactly once, upfront,
by the caller (app/core/fallback.py in Phase 4, a ReAct tool wrapper in
Phase 6) before scoring runs. Keeping scoring.py itself synchronous and I/O-free
is what makes it unit-testable with fixed fixtures and bit-reproducible.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

# ─────────────────────────── travel matrix (pre-computed) ───────────────────

@dataclass
class TravelMatrix:
    """A pre-fetched lookup from (origin_key, dest_key) -> minutes. Built by
    the caller from routing_tool.get_travel_matrix()'s result before calling
    rank() - scoring.py never awaits anything itself. A cache miss returns
    None, and prox() below falls through to the haversine estimate, exactly
    matching DETERMINISM_AND_VALIDATION.md §6.2's documented fallback order."""

    _lookup: dict[tuple[str, str], float] = field(default_factory=dict)

    @staticmethod
    def _key(point: dict) -> str:
        return f"{round(point['lat'], 4)},{round(point['lon'], 4)}"

    def set(self, origin: dict, dest: dict, minutes: float) -> None:
        self._lookup[(self._key(origin), self._key(dest))] = minutes

    def minutes(self, origin: dict, dest: dict) -> Optional[float]:
        return self._lookup.get((self._key(origin), self._key(dest)))

    @classmethod
    def from_matrix_result(cls, origins: list[dict], destinations: list[dict], result: dict) -> "TravelMatrix":
        """Builds a TravelMatrix from routing_tool.get_travel_matrix()'s
        {"minutes": [[...]], "km": [[...]]} return shape."""
        tm = cls()
        for i, o in enumerate(origins):
            for j, d in enumerate(destinations):
                tm.set(o, d, result["minutes"][i][j])
        return tm


def haversine_km(a: dict, b: dict) -> float:
    R = 6371.0
    lat1, lon1, lat2, lon2 = a["lat"], a["lon"], b["lat"], b["lon"]
    dlat, dlon = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    x = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(x), math.sqrt(1 - x))


def _avg_kmh(straight_km: float) -> float:
    """Distance-banded, because one constant is wrong at both ends.

    Measured against real ORS 2026-09-02, Colombo->Kandy: actual 121 km /
    128 min (~57 km/h). A flat 32 km/h predicted 239 min - nearly 2x over,
    which would push prox() toward 0 on every inter-city leg and make the
    scorer over-prefer whatever is nearest, exactly the bias this factor
    exists to avoid. The 1.35 road-distance factor checked out fine
    (127.6 km predicted vs 121 km actual) and is kept as-is.

    Provisional bands - recalibrate against more real ORS samples if
    scoring behavior suggests they're off (docs/master_plan/API_SETUP.md §3.1.1).
    Kept identical to app/tools/routing_tool.py's copy so both fallback
    paths agree; scoring.py cannot import routing_tool (that module does
    I/O) so the constants are duplicated deliberately, not shared."""
    if straight_km < 5:
        return 18.0   # urban, congested
    if straight_km < 30:
        return 35.0   # suburban / secondary roads
    return 50.0        # inter-city A-roads


def haversine_minutes(a: dict, b: dict) -> float:
    straight = haversine_km(a, b)
    return (straight * 1.35) / _avg_kmh(straight) * 60.0


def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


# ─────────────────────────── sub-scores ───────────────────────────────────

def pref(item: dict, interests: list[str]) -> float:
    """Tag overlap between the traveller's interests and the listing's
    canonical tags. Dividing by min(len(interests), 3) means a traveller
    with 8 interests isn't punished for items that can only match 2 of
    them - matching any 3 is a perfect preference fit."""
    if not interests:
        return 0.5   # neutral: let other factors decide
    item_tags = set(item.get("tags") or [])
    overlap = len(item_tags & set(interests))
    return min(overlap / min(len(interests), 3), 1.0)


D_MAX_MIN = 90.0


def prox(item: dict, anchor: dict, matrix: TravelMatrix) -> float:
    minutes = matrix.minutes(anchor, item)
    if minutes is None:
        minutes = haversine_minutes(anchor, item)
    return clamp(1.0 - minutes / D_MAX_MIN)


PRIOR_MEAN, PRIOR_WEIGHT = 0.5, 5.0


def rate(item: dict) -> float:
    """Bayesian-shrunk rating, so a single 5-star review can't beat a
    4.4-star with 200. rating is populated for hotels only (Booking.com's
    reviewScore, verified live - docs/master_plan/API_SETUP.md §4.1);
    restaurants/attractions correctly fall to the neutral-ish prior below,
    same degradation path either way."""
    if item.get("rating") is None:
        return 0.45   # slightly below neutral: unknown != good
    r = clamp((item["rating"] - 1.0) / 4.0)   # 1-5 stars -> 0-1
    n = item.get("rating_count") or 0
    return (r * n + PRIOR_MEAN * PRIOR_WEIGHT) / (n + PRIOR_WEIGHT)


LEVEL_BASE = {1: 0.90, 2: 1.00, 3: 0.65, 4: 0.35}   # level 2 (medium) scores highest, not level 1


def cost(item: dict, budget_per_day_for_category: Optional[float], estimated_item_cost: Optional[float]) -> float:
    """Prefers medium/low price bands even when the budget could stretch
    further (project concern #10) - rock-bottom options are usually worse
    experiences, so the preferred band is medium, with low close behind and
    expensive penalized, independently of whether the traveller could
    afford it. `estimated_item_cost` is computed by app/core/budget.py's
    precedence chain (exact price -> cost_reference -> national fallback)
    and passed in - scoring.py has no DB access of its own."""
    base = LEVEL_BASE.get(item.get("price_level"), 0.60)   # unknown level -> mild neutral
    if budget_per_day_for_category is None or estimated_item_cost is None:
        return base
    ratio = estimated_item_cost / budget_per_day_for_category if budget_per_day_for_category > 0 else 999
    fit = 1.0 if ratio <= 1.0 else 0.6 if ratio <= 1.5 else 0.2
    return 0.6 * base + 0.4 * fit


@dataclass
class Breakdown:
    pref: float
    prox: float
    rating: float
    cost: float


@dataclass
class Ranked:
    item: dict
    score: float
    breakdown: Breakdown
    rank: int = 0


# ─────────────────────────── weights ───────────────────────────────────────

WEIGHTS: dict[str, dict[str, float]] = {
    "attraction": {"pref": 0.45, "prox": 0.25, "rate": 0.20, "cost": 0.10},
    "restaurant": {"pref": 0.30, "prox": 0.25, "rate": 0.20, "cost": 0.25},
    "hotel":      {"pref": 0.25, "prox": 0.25, "rate": 0.20, "cost": 0.30},
    "event":      {"pref": 0.50, "prox": 0.25, "rate": 0.10, "cost": 0.15},
}


# ─────────────────────────── hard filters ───────────────────────────────────

def tag_hit(item: dict, must_avoid: list[str]) -> bool:
    if not must_avoid:
        return False
    return bool(set(item.get("tags") or []) & set(must_avoid))


def in_red_disaster_zone(item: dict, disaster: Optional[dict], km: float = 50.0) -> bool:
    if not disaster:
        return False
    for event in disaster.get("active_events", []):
        if event.get("severity") != "red":
            continue
        event_point = event.get("location")
        if event_point and haversine_km(item, event_point) <= km:
            return True
    return False


def open_during(item: dict, date_window: Optional[dict]) -> bool:
    """Only excludes when opening_hours is known AND clearly says closed -
    OSM's opening_hours syntax isn't parsed into structure yet (see
    app/data/connectors/osm_listings.py's raw-text note), so absence of
    data must never be treated as "closed". Conservative by design: this
    is a real gap, not a false negative waiting to happen."""
    return True   # placeholder until opening_hours is actually parsed into structure


def hard_filter(items: list[dict], ctx: "ScoringContext") -> list[dict]:
    """Scoring ranks; it does not exclude. These are the only exclusions."""
    return [
        i for i in items
        if i.get("is_verified", True) and i.get("is_active", True)
        and not tag_hit(i, ctx.must_avoid)
        and not in_red_disaster_zone(i, ctx.disaster)
        and (i.get("price_level") is None or ctx.max_price_level is None or i["price_level"] <= ctx.max_price_level)
        and open_during(i, ctx.date_window)
    ]


@dataclass
class ScoringContext:
    interests: list[str] = field(default_factory=list)
    anchor: dict = field(default_factory=dict)             # {"lat": ..., "lon": ...}
    matrix: TravelMatrix = field(default_factory=TravelMatrix)
    budget_per_day: dict[str, Optional[float]] = field(default_factory=dict)   # category -> LKR/day
    cost_estimates: dict[str, Optional[float]] = field(default_factory=dict)   # item id -> LKR
    must_avoid: list[str] = field(default_factory=list)
    disaster: Optional[dict] = None
    max_price_level: Optional[int] = None
    date_window: Optional[dict] = None


# ─────────────────────────── ranking ───────────────────────────────────────

def rank(items: list[dict], ctx: ScoringContext, category: str) -> list[Ranked]:
    w = WEIGHTS[category]
    scored: list[Ranked] = []
    for i in hard_filter(items, ctx):
        b = Breakdown(
            pref=pref(i, ctx.interests),
            prox=prox(i, ctx.anchor, ctx.matrix),
            rating=rate(i),
            cost=cost(i, ctx.budget_per_day.get(category), ctx.cost_estimates.get(i["id"])),
        )
        # round(..., 6) before sorting - float noise in the 15th decimal
        # place otherwise reorders equal items between runs for no reason.
        s = round(w["pref"] * b.pref + w["prox"] * b.prox + w["rate"] * b.rating + w["cost"] * b.cost, 6)
        scored.append(Ranked(item=i, score=s, breakdown=b))

    # item.id as the final tie-break -> total ordering, deterministic even
    # when score and rating_count are both equal.
    scored.sort(key=lambda r: (-r.score, -(r.item.get("rating_count") or 0), r.item["id"]))
    for n, r in enumerate(scored, start=1):
        r.rank = n
    return scored
