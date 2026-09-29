# tests/test_scoring.py
# Pure unit tests - no I/O, no mocking needed. This module IS the
# determinism guarantee (docs/master_plan/DETERMINISM_AND_VALIDATION.md §6),
# so these tests assert exact values, not just "something reasonable".

import pytest

from app.core.scoring import (
    ScoringContext, TravelMatrix, Breakdown,
    pref, prox, rate, cost, hard_filter, rank, tag_hit, in_red_disaster_zone,
    haversine_km, haversine_minutes, clamp, WEIGHTS,
)

KANDY = {"id": "hotel-1", "name": "Test Hotel", "lat": 7.2906, "lon": 80.6337}


# ---- pref -------------------------------------------------------------

def test_pref_no_interests_returns_neutral():
    assert pref({"tags": ["culture"]}, []) == 0.5


def test_pref_full_overlap_of_three_is_perfect():
    item = {"tags": ["culture", "history", "food"]}
    assert pref(item, ["culture", "history", "food"]) == 1.0


def test_pref_partial_overlap():
    item = {"tags": ["culture"]}
    assert pref(item, ["culture", "history", "food"]) == pytest.approx(1 / 3)


def test_pref_more_than_three_interests_still_maxes_at_three_matches():
    item = {"tags": ["a", "b", "c"]}
    interests = ["a", "b", "c", "d", "e", "f", "g", "h"]
    assert pref(item, interests) == 1.0   # matching any 3 of 8 is a perfect fit


def test_pref_zero_overlap():
    item = {"tags": ["beach"]}
    assert pref(item, ["culture"]) == 0.0


def test_pref_missing_tags_key_treated_as_empty():
    assert pref({}, ["culture"]) == 0.0


# ---- prox ---------------------------------------------------------------

def test_prox_same_point_is_maximal():
    matrix = TravelMatrix()
    assert prox(KANDY, KANDY, matrix) == 1.0


def test_prox_uses_matrix_when_available():
    matrix = TravelMatrix()
    dest = {"lat": 7.30, "lon": 80.64}
    matrix.set(KANDY, dest, 45.0)   # 45 min -> 1 - 45/90 = 0.5
    assert prox(dest, KANDY, matrix) == 0.5


def test_prox_falls_back_to_haversine_on_cache_miss():
    matrix = TravelMatrix()   # empty - no cached entry
    far = {"lat": 6.0535, "lon": 80.2210}   # Galle, genuinely far from Kandy
    result = prox(far, KANDY, matrix)
    assert 0.0 <= result < 0.5   # far enough that proximity score should be low


def test_prox_clamped_to_zero_beyond_d_max():
    matrix = TravelMatrix()
    matrix.set(KANDY, {"lat": 0, "lon": 0}, 500.0)   # way beyond D_MAX_MIN=90
    assert prox({"lat": 0, "lon": 0}, KANDY, matrix) == 0.0


# ---- rate -----------------------------------------------------------------

def test_rate_unknown_rating_returns_prior():
    assert rate({"rating": None}) == 0.45


def test_rate_missing_rating_key_returns_prior():
    assert rate({}) == 0.45


def test_rate_high_rating_high_count_approaches_the_raw_score():
    # 4.8 stars, 500 reviews - shrinkage should barely move it from the raw ~0.95
    item = {"rating": 4.8, "rating_count": 500}
    r = rate(item)
    raw = (4.8 - 1.0) / 4.0
    assert abs(r - raw) < 0.02


def test_rate_single_five_star_review_does_not_beat_established_4_4():
    # The whole point of Bayesian shrinkage (per the module's own framing).
    single_five_star = rate({"rating": 5.0, "rating_count": 1})
    established_4_4 = rate({"rating": 4.4, "rating_count": 200})
    assert single_five_star < established_4_4


def test_rate_zero_reviews_equals_the_prior_mean():
    # n=0 -> (r*0 + 0.5*5) / (0+5) = 0.5, regardless of the stated rating
    assert rate({"rating": 3.0, "rating_count": 0}) == pytest.approx(0.5)


# ---- cost -------------------------------------------------------------------

def test_cost_level_2_beats_level_1_with_no_budget_constraint():
    # Deliberate: medium beats cheapest, per project concern #10.
    level1 = cost({"price_level": 1}, None, None)
    level2 = cost({"price_level": 2}, None, None)
    assert level2 > level1


def test_cost_unknown_level_is_mild_neutral():
    assert cost({}, None, None) == 0.60


def test_cost_within_budget_scores_higher_than_over_budget():
    within = cost({"price_level": 2}, 10000.0, 8000.0)
    over = cost({"price_level": 2}, 10000.0, 30000.0)
    assert within > over


def test_cost_no_estimate_falls_back_to_level_base_only():
    assert cost({"price_level": 3}, 10000.0, None) == 0.65


# ---- hard filters ---------------------------------------------------------

def test_tag_hit_true_on_overlap():
    assert tag_hit({"tags": ["hike"]}, ["hike"]) is True


def test_tag_hit_false_on_no_overlap_or_empty_must_avoid():
    assert tag_hit({"tags": ["hike"]}, []) is False
    assert tag_hit({"tags": ["culture"]}, ["hike"]) is False


def test_in_red_disaster_zone_true_within_radius():
    item = {"lat": 7.29, "lon": 80.63}
    disaster = {"active_events": [{"severity": "red", "location": {"lat": 7.30, "lon": 80.64}}]}
    assert in_red_disaster_zone(item, disaster, km=50) is True


def test_in_red_disaster_zone_false_for_non_red_severity():
    item = {"lat": 7.29, "lon": 80.63}
    disaster = {"active_events": [{"severity": "orange", "location": {"lat": 7.29, "lon": 80.63}}]}
    assert in_red_disaster_zone(item, disaster, km=50) is False


def test_in_red_disaster_zone_false_beyond_radius():
    item = {"lat": 7.29, "lon": 80.63}
    disaster = {"active_events": [{"severity": "red", "location": {"lat": 9.66, "lon": 80.02}}]}  # Jaffna, far away
    assert in_red_disaster_zone(item, disaster, km=50) is False


def test_in_red_disaster_zone_false_when_no_disaster_data():
    assert in_red_disaster_zone({"lat": 7.29, "lon": 80.63}, None) is False


def test_hard_filter_excludes_unverified_and_inactive():
    ctx = ScoringContext()
    items = [
        {"id": "1", "is_verified": True, "is_active": True},
        {"id": "2", "is_verified": False, "is_active": True},
        {"id": "3", "is_verified": True, "is_active": False},
    ]
    result = hard_filter(items, ctx)
    assert [i["id"] for i in result] == ["1"]


def test_hard_filter_excludes_must_avoid_tags():
    ctx = ScoringContext(must_avoid=["hike"])
    items = [{"id": "1", "tags": ["hike"]}, {"id": "2", "tags": ["culture"]}]
    result = hard_filter(items, ctx)
    assert [i["id"] for i in result] == ["2"]


def test_hard_filter_excludes_over_max_price_level():
    ctx = ScoringContext(max_price_level=2)
    items = [{"id": "1", "price_level": 4}, {"id": "2", "price_level": 1}, {"id": "3", "price_level": None}]
    result = hard_filter(items, ctx)
    assert {i["id"] for i in result} == {"2", "3"}   # unknown price_level is not excluded


# ---- rank -------------------------------------------------------------------

def test_rank_produces_sequential_ranks_starting_at_one():
    ctx = ScoringContext(anchor={"lat": 0, "lon": 0})
    items = [{"id": str(i), "tags": [], "lat": 0, "lon": 0} for i in range(3)]
    result = rank(items, ctx, "attraction")
    assert [r.rank for r in result] == [1, 2, 3]


def test_rank_sorts_by_score_descending():
    ctx = ScoringContext(interests=["culture"], anchor={"lat": 0, "lon": 0})
    items = [
        {"id": "low", "tags": [], "lat": 0, "lon": 0, "rating": 3.0, "rating_count": 100},
        {"id": "high", "tags": ["culture"], "lat": 0, "lon": 0, "rating": 4.8, "rating_count": 100},
    ]
    result = rank(items, ctx, "attraction")
    assert [r.item["id"] for r in result] == ["high", "low"]


def test_rank_tie_break_is_deterministic_by_item_id():
    # Identical items except id - score and rating_count both tie, so id
    # must be the deciding factor, and it must sort ascending.
    ctx = ScoringContext(anchor={"lat": 0, "lon": 0})
    items = [
        {"id": "zebra", "tags": [], "lat": 0, "lon": 0},
        {"id": "apple", "tags": [], "lat": 0, "lon": 0},
    ]
    result = rank(items, ctx, "attraction")
    assert [r.item["id"] for r in result] == ["apple", "zebra"]


def test_rank_is_independent_of_input_order():
    """The determinism test from DETERMINISM_AND_VALIDATION.md §8 - an
    accidental unstable sort is the most common way a 'deterministic'
    ranker quietly stops being one."""
    ctx = ScoringContext(interests=["culture", "food"], anchor={"lat": 7.29, "lon": 80.63})
    items = [
        {"id": "a", "tags": ["culture"], "lat": 7.29, "lon": 80.63, "rating": 4.5, "rating_count": 50, "price_level": 2},
        {"id": "b", "tags": ["food"], "lat": 7.30, "lon": 80.64, "rating": 3.9, "rating_count": 10, "price_level": 1},
        {"id": "c", "tags": [], "lat": 6.0, "lon": 80.2, "rating": None, "price_level": 3},
    ]
    forward = [r.item["id"] for r in rank(items, ctx, "attraction")]
    backward = [r.item["id"] for r in rank(list(reversed(items)), ctx, "attraction")]
    assert forward == backward


def test_rank_applies_hard_filter_first():
    ctx = ScoringContext(must_avoid=["hike"], anchor={"lat": 0, "lon": 0})
    items = [
        {"id": "excluded", "tags": ["hike"], "lat": 0, "lon": 0},
        {"id": "kept", "tags": [], "lat": 0, "lon": 0},
    ]
    result = rank(items, ctx, "attraction")
    assert [r.item["id"] for r in result] == ["kept"]


def test_rank_breakdown_is_returned_per_item():
    ctx = ScoringContext(interests=["culture"], anchor={"lat": 0, "lon": 0})
    items = [{"id": "1", "tags": ["culture"], "lat": 0, "lon": 0, "rating": 4.5, "rating_count": 50}]
    result = rank(items, ctx, "attraction")
    assert isinstance(result[0].breakdown, Breakdown)
    assert result[0].breakdown.pref == 1.0


def test_weights_sum_to_one_per_category():
    for category, w in WEIGHTS.items():
        assert sum(w.values()) == pytest.approx(1.0), f"{category} weights don't sum to 1.0"


# ---- calibration / pure math (shared with routing_tool, duplicated by design) --

def test_haversine_km_known_distance():
    d = haversine_km({"lat": 6.9271, "lon": 79.8612}, {"lat": 7.2906, "lon": 80.6337})
    assert 90 < d < 100


def test_clamp_bounds():
    assert clamp(-1.0) == 0.0
    assert clamp(2.0) == 1.0
    assert clamp(0.5) == 0.5
