"""
Geographic day-clustering for the deterministic planners
(app/core/fallback.py, app/core/followup_replan.py) - the direct fix for
Defect 1a in the itinerary-quality/token-reduction pass: attractions used
to be assigned to days by GLOBAL SCORE ONLY (the top `items_target` still-
unused-by-earlier-days items, in rank order), with no geography at all, so
a far-flung attraction could land on day 1 simply because it ranked 2nd.
Two attractions 60 km apart could easily end up scheduled on the same day.

Pure, deterministic, no I/O - same convention as app/core/itinerary.py.
"""
from __future__ import annotations

from app.core.scoring import haversine_km


#  A pool sized EXACTLY days*per_day forces every later day to take
# whatever day 1 (and day 2, etc.) didn't want - live-found running this
# against real Ella data: with only 6 candidates total for a 2-day trip,
# day 1 correctly claimed the 3 tightly-clustered central items, but day 2
# was then stuck with the 3 leftovers regardless of whether THEY were
# mutually close (they weren't - two of them were 12.5km apart, a real
# backtrack). Widening the pool gives every day beyond the first a genuine
# choice of geographically coherent neighbours to draw from, at the cost of
# occasionally using a lower-ranked item - a deliberate trade favouring
# geographic coherence, which is exactly what this module exists for.
_POOL_MULTIPLIER = 3


def partition_by_geography(ranked: list[dict], days: int, per_day: int) -> list[list[dict]]:
    """Greedy seed-and-grow partition of `ranked` (already score-ordered)
    into `days` geographically coherent clusters of up to `per_day` items
    each.

    Algorithm, O(days * len(pool)):
      1. pool = the top `days * per_day * _POOL_MULTIPLIER` items, in RANK
         order (see _POOL_MULTIPLIER's own comment for why it's wider than
         the exact number of slots).
      2. For each day, in order: seed = the highest-ranked item not yet
         used by an earlier day (so day 1 still gets the best-rated
         cluster - the score ordering is preserved, only which OTHER
         items join it changes); then take the `per_day - 1` nearest
         (by haversine_km) still-unused items to that seed, tie-broken by
         (distance, pool index) so this is fully deterministic - no set
         iteration order, no randomness.

    Disjoint by construction (an item, once used, is never picked again).
    Degenerate cases the tests exercise: fewer candidates than
    days*per_day -> later days may end up with fewer than per_day items,
    or an empty list; an empty `ranked` -> every day gets []."""
    pool = ranked[: days * per_day * _POOL_MULTIPLIER]
    used: set[int] = set()   # indices into `pool`
    clusters: list[list[dict]] = []

    for _ in range(days):
        remaining = [i for i in range(len(pool)) if i not in used]
        if not remaining:
            clusters.append([])
            continue

        seed_idx = remaining[0]   # highest-ranked item not yet used
        used.add(seed_idx)
        seed = pool[seed_idx]

        others = [i for i in remaining if i != seed_idx]
        others.sort(key=lambda i: (haversine_km(seed, pool[i]), i))
        chosen = [seed_idx, *others[: per_day - 1]]
        used.update(chosen)

        clusters.append([pool[i] for i in sorted(chosen)])

    return clusters
