# tests/test_routing_tool.py
# No real database or network - the asyncpg pool and ORS's HTTP responses
# are both faked. Verifies the three-tier fallback (cache -> ORS -> haversine)
# and, critically, that a single get_travel_matrix() call issues at most ONE
# ORS request regardless of matrix size - the quota-safety requirement from
# docs/master_plan/DATA_PLATFORM.md §7 ("never per-pair").

from unittest.mock import AsyncMock

import httpx
import respx

from app.tools import routing_tool
from app.tools.routing_tool import get_travel_matrix, haversine_km, haversine_minutes, _avg_kmh

COLOMBO = {"lat": 6.9271, "lon": 79.8612}
KANDY = {"lat": 7.2906, "lon": 80.6337}
GALLE = {"lat": 6.0535, "lon": 80.2210}


class _FakePool:
    def __init__(self, rows=None):
        self._rows = rows or []
        self.calls: list[tuple] = []

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        return self._rows

    async def execute(self, sql, *args):
        self.calls.append((sql, args))


def _patch_pool(monkeypatch, rows=None) -> _FakePool:
    pool = _FakePool(rows)
    monkeypatch.setattr(routing_tool, "get_pool", AsyncMock(return_value=pool))
    return pool


async def test_empty_inputs_returns_empty():
    result = await get_travel_matrix([], [])
    assert result == {"minutes": [], "km": [], "provider": "none"}


async def test_full_cache_hit_makes_no_http_call(monkeypatch):
    rows = [{
        "origin_key": "6.9271,79.8612", "dest_key": "7.2906,80.6337",
        "minutes": 128.0, "km": 121.0, "provider": "ors",
    }]
    _patch_pool(monkeypatch, rows)

    with respx.mock:  # no route registered - a call here raises and fails the test
        result = await get_travel_matrix([COLOMBO], [KANDY])

    assert result == {"minutes": [[128.0]], "km": [[121.0]], "provider": "cache"}


async def test_cache_miss_calls_ors_once_for_a_multi_point_matrix(monkeypatch):
    """The quota-safety proof: 2 origins x 2 destinations = 4 pairs, but
    exactly ONE HTTP call should be made."""
    _patch_pool(monkeypatch, rows=[])
    call_count = 0

    def _responder(request):
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, json={
            "durations": [[0.0, 7680.0], [7680.0, 0.0]],
            "distances": [[0.0, 121000.0], [121000.0, 0.0]],
        })

    with respx.mock:
        respx.post(f"{routing_tool.settings.ors_base_url}/v2/matrix/driving-car").mock(side_effect=_responder)
        result = await get_travel_matrix([COLOMBO, KANDY], [COLOMBO, KANDY])

    assert call_count == 1, "must issue exactly one matrix call, never per-pair"
    assert result["provider"] == "ors"
    assert result["minutes"][0][1] == 128.0  # 7680s -> 128min
    assert result["km"][0][1] == 121.0


async def test_ors_failure_falls_back_to_haversine(monkeypatch):
    _patch_pool(monkeypatch, rows=[])

    with respx.mock:
        respx.post(f"{routing_tool.settings.ors_base_url}/v2/matrix/driving-car").mock(
            return_value=httpx.Response(500)
        )
        result = await get_travel_matrix([COLOMBO], [KANDY])

    assert result["provider"] == "haversine"
    assert result["minutes"][0][0] > 0
    assert result["km"][0][0] > 0


async def test_no_ors_key_falls_back_to_haversine_without_a_call(monkeypatch):
    monkeypatch.setattr(routing_tool.settings, "ors_api_key", "")
    _patch_pool(monkeypatch, rows=[])

    with respx.mock:  # no route registered - would raise if a call were attempted
        result = await get_travel_matrix([COLOMBO], [KANDY])

    assert result["provider"] == "haversine"


async def test_no_database_still_produces_a_result(monkeypatch):
    monkeypatch.setattr(routing_tool, "get_pool", AsyncMock(return_value=None))

    with respx.mock:
        respx.post(f"{routing_tool.settings.ors_base_url}/v2/matrix/driving-car").mock(
            return_value=httpx.Response(200, json={
                "durations": [[7680.0]], "distances": [[121000.0]],
            })
        )
        result = await get_travel_matrix([COLOMBO], [KANDY])

    assert result["provider"] == "ors"
    assert result["minutes"] == [[128.0]]


# ---- calibration / pure math ---------------------------------------------

def test_avg_kmh_bands():
    assert _avg_kmh(2) == 18.0
    assert _avg_kmh(15) == 35.0
    assert _avg_kmh(100) == 50.0


def test_haversine_km_known_distance():
    # Colombo -> Kandy straight-line is ~94.5km (verified against the real
    # ORS measurement in docs/master_plan/API_SETUP.md §3.1.1: 121km actual
    # road distance vs this function's ~94.5km straight line).
    d = haversine_km(COLOMBO, KANDY)
    assert 90 < d < 100


def test_haversine_minutes_uses_banded_speed_not_flat_32kmh():
    # Regression guard for the calibration finding itself: the old flat
    # 32km/h constant predicted 239min for this pair; the banded version
    # must predict meaningfully less (~152min, still an overestimate of the
    # real 128min ORS answer, but no longer wildly so).
    minutes = haversine_minutes(COLOMBO, KANDY)
    assert minutes < 200, "banded speed must not reproduce the old 32km/h behavior"
