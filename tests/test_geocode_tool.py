# tests/test_geocode_tool.py
# No real network calls - Nominatim's HTTP responses are mocked with respx.

import respx
import httpx

from app.tools.geocode_tool import geocode_destination, NOMINATIM_URL


@respx.mock
async def test_geocode_success():
    respx.get(NOMINATIM_URL).mock(
        return_value=httpx.Response(200, json=[{"lat": "7.2931", "lon": "80.6350"}])
    )
    result = await geocode_destination("Kandy")
    assert result == {"lat": 7.2931, "lon": 80.6350}


@respx.mock
async def test_geocode_no_results_returns_none():
    respx.get(NOMINATIM_URL).mock(return_value=httpx.Response(200, json=[]))
    result = await geocode_destination("Xyzzyplonkqqq123")
    assert result is None


@respx.mock
async def test_geocode_api_failure_returns_none():
    respx.get(NOMINATIM_URL).mock(return_value=httpx.Response(500))
    result = await geocode_destination("Kandy")
    assert result is None
