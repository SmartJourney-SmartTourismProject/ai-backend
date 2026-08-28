# tests/test_location_tool.py
# No real network calls - ip-api.com's HTTP responses are mocked with respx.

import respx
import httpx

from app.tools.location_tool import resolve_start_location


async def test_gps_provided_short_circuits_no_network():
    result = await resolve_start_location(client_gps={"lat": 7.29, "lon": 80.63}, client_ip="8.8.8.8")
    assert result == {"lat": 7.29, "lon": 80.63, "source": "gps"}


@respx.mock
async def test_ip_fallback_success():
    respx.get("http://ip-api.com/json/8.8.8.8").mock(
        return_value=httpx.Response(200, json={"status": "success", "lat": 37.4, "lon": -122.0})
    )
    result = await resolve_start_location(client_gps=None, client_ip="8.8.8.8")
    assert result == {"lat": 37.4, "lon": -122.0, "source": "ip"}


@respx.mock
async def test_gps_with_null_values_falls_back_to_ip():
    respx.get("http://ip-api.com/json/8.8.8.8").mock(
        return_value=httpx.Response(200, json={"status": "success", "lat": 1.0, "lon": 2.0})
    )
    result = await resolve_start_location(client_gps={"lat": None, "lon": None}, client_ip="8.8.8.8")
    assert result == {"lat": 1.0, "lon": 2.0, "source": "ip"}


async def test_nothing_provided_returns_none():
    result = await resolve_start_location(client_gps=None, client_ip=None)
    assert result is None


@respx.mock
async def test_ip_lookup_http_failure_returns_none_not_raises():
    respx.get("http://ip-api.com/json/0.0.0.0").mock(return_value=httpx.Response(500))
    result = await resolve_start_location(client_gps=None, client_ip="0.0.0.0")
    assert result is None


@respx.mock
async def test_ip_lookup_status_fail_returns_none():
    respx.get("http://ip-api.com/json/1.2.3.4").mock(
        return_value=httpx.Response(200, json={"status": "fail", "message": "invalid query"})
    )
    result = await resolve_start_location(client_gps=None, client_ip="1.2.3.4")
    assert result is None
