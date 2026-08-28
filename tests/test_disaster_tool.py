# tests/test_disaster_tool.py
# No real network calls - EONET/USGS/GDACS HTTP responses are mocked with respx.

from unittest.mock import AsyncMock

import respx
import httpx

import app.tools.disaster_tool as disaster_tool_module
from app.tools.disaster_tool import get_disaster_info, EONET_URL, USGS_URL, GDACS_URL, _distance_km

EMPTY_EONET = {"events": []}
EMPTY_USGS = {"features": []}
EMPTY_GDACS = "<rss><channel></channel></rss>"


@respx.mock
async def test_no_events_is_safe():
    respx.get(EONET_URL).mock(return_value=httpx.Response(200, json=EMPTY_EONET))
    respx.get(USGS_URL).mock(return_value=httpx.Response(200, json=EMPTY_USGS))
    respx.get(GDACS_URL).mock(return_value=httpx.Response(200, text=EMPTY_GDACS))

    result = await get_disaster_info(7.29, 80.63)
    assert result == {"safe": True, "active_events": []}


@respx.mock
async def test_nearby_earthquake_is_detected():
    respx.get(EONET_URL).mock(return_value=httpx.Response(200, json=EMPTY_EONET))
    respx.get(USGS_URL).mock(return_value=httpx.Response(200, json={
        "features": [{
            "properties": {"mag": 6.5, "place": "10km N of Testville"},
            "geometry": {"coordinates": [80.63, 7.29, 10.0]},
        }]
    }))
    respx.get(GDACS_URL).mock(return_value=httpx.Response(200, text=EMPTY_GDACS))

    result = await get_disaster_info(7.29, 80.63)

    assert result["safe"] is False
    assert len(result["active_events"]) == 1
    assert result["active_events"][0]["severity"] == "orange"
    assert result["active_events"][0]["source"] == "USGS"


@respx.mock
async def test_all_sources_returning_http_errors_still_reports_safe():
    # Each _fetch_* function catches its own HTTP errors internally and
    # returns [] rather than raising - so from get_disaster_info's point of
    # view, "all three APIs are down" and "confirmed zero events nearby"
    # currently look identical. This documents that actual behavior; it
    # does NOT reach the "note": "disaster data unavailable" fallback
    # below, since that only fires when an exception genuinely reaches
    # asyncio.gather (see test_exception_escaping_a_fetcher_triggers_note_fallback).
    respx.get(EONET_URL).mock(return_value=httpx.Response(500))
    respx.get(USGS_URL).mock(return_value=httpx.Response(500))
    respx.get(GDACS_URL).mock(return_value=httpx.Response(500))

    result = await get_disaster_info(7.29, 80.63)

    assert result["safe"] is True
    assert result["active_events"] == []
    assert "note" not in result


async def test_exception_escaping_a_fetcher_triggers_note_fallback():
    # Bypasses the per-source try/except entirely by mocking the fetcher
    # functions themselves, to prove the all-sources-failed fallback path
    # does work when a real exception reaches asyncio.gather.
    original_eonet = disaster_tool_module._fetch_eonet
    original_usgs = disaster_tool_module._fetch_usgs
    original_gdacs = disaster_tool_module._fetch_gdacs
    try:
        disaster_tool_module._fetch_eonet = AsyncMock(side_effect=RuntimeError("boom"))
        disaster_tool_module._fetch_usgs = AsyncMock(side_effect=RuntimeError("boom"))
        disaster_tool_module._fetch_gdacs = AsyncMock(side_effect=RuntimeError("boom"))

        result = await get_disaster_info(7.29, 80.63)
    finally:
        disaster_tool_module._fetch_eonet = original_eonet
        disaster_tool_module._fetch_usgs = original_usgs
        disaster_tool_module._fetch_gdacs = original_gdacs

    assert result == {"safe": True, "active_events": [], "note": "disaster data unavailable"}


@respx.mock
async def test_uses_cache_on_second_call():
    respx.get(EONET_URL).mock(return_value=httpx.Response(200, json=EMPTY_EONET))
    usgs_route = respx.get(USGS_URL).mock(return_value=httpx.Response(200, json=EMPTY_USGS))
    respx.get(GDACS_URL).mock(return_value=httpx.Response(200, text=EMPTY_GDACS))

    await get_disaster_info(1.0, 1.0)
    await get_disaster_info(1.0, 1.0)

    assert usgs_route.call_count == 1


def test_distance_km_kandy_to_colombo_is_roughly_correct():
    km = _distance_km(7.2906, 80.6337, 6.9271, 79.8612)
    assert 90 <= km <= 100
