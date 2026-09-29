# tests/test_clustering.py
# app/core/clustering.py's partition_by_geography() - pure, deterministic,
# no I/O. Fixed fixtures throughout, same bit-reproducibility expectation
# as app/core/itinerary.py's own tests.

from app.core.clustering import partition_by_geography

# Two well-separated geographic groups plus a couple of stragglers, ranked
# 1..6 in this order (index 0 = rank 1, the highest-ranked/most-preferred).
NEAR_A = {"id": "a1", "lat": 7.2906, "lon": 80.6337}     # Kandy town
NEAR_B = {"id": "a2", "lat": 7.2936, "lon": 80.6413}     # ~1km from NEAR_A
FAR_A = {"id": "a3", "lat": 7.9403, "lon": 81.0188}      # Rangala-ish, ~80km away
FAR_B = {"id": "a4", "lat": 7.9450, "lon": 81.0200}      # ~1km from FAR_A
LONE_C = {"id": "a5", "lat": 6.0535, "lon": 80.2210}     # Galle-ish, far from everything
LONE_D = {"id": "a6", "lat": 9.6615, "lon": 80.0255}     # Jaffna-ish, far from everything

RANKED = [NEAR_A, FAR_A, NEAR_B, FAR_B, LONE_C, LONE_D]   # deliberately interleaved by rank


def test_clusters_are_geographically_coherent_not_just_top_n_by_rank():
    # The old bug this fixes: taking the top-N by rank per day would put
    # NEAR_A (rank 1) and FAR_A (rank 2) on the SAME day, 80km apart.
    clusters = partition_by_geography(RANKED, days=2, per_day=2)

    assert len(clusters) == 2
    day1_ids = {i["id"] for i in clusters[0]}
    day2_ids = {i["id"] for i in clusters[1]}
    # NEAR_A's cluster-mate must be its real geographic neighbour, not
    # whatever ranked 2nd.
    assert day1_ids == {"a1", "a2"} or day2_ids == {"a1", "a2"}


def test_clusters_are_disjoint():
    clusters = partition_by_geography(RANKED, days=3, per_day=2)
    seen: set[str] = set()
    for cluster in clusters:
        ids = {i["id"] for i in cluster}
        assert not (ids & seen)   # no id appears in more than one cluster
        seen |= ids


def test_day_one_gets_the_best_ranked_seed():
    clusters = partition_by_geography(RANKED, days=3, per_day=2)
    assert clusters[0][0]["id"] == "a1"   # the top-ranked item seeds day 1


def test_fewer_candidates_than_days_leaves_trailing_clusters_short():
    clusters = partition_by_geography([NEAR_A, NEAR_B], days=3, per_day=2)
    assert len(clusters) == 3
    assert {i["id"] for i in clusters[0]} == {"a1", "a2"}
    assert clusters[1] == []
    assert clusters[2] == []


def test_single_candidate():
    clusters = partition_by_geography([NEAR_A], days=2, per_day=3)
    assert clusters[0] == [NEAR_A]
    assert clusters[1] == []


def test_empty_pool_produces_every_day_empty():
    clusters = partition_by_geography([], days=3, per_day=3)
    assert clusters == [[], [], []]


def test_deterministic_across_runs():
    results = [partition_by_geography(RANKED, days=3, per_day=2) for _ in range(5)]
    first = results[0]
    for r in results[1:]:
        assert r == first
