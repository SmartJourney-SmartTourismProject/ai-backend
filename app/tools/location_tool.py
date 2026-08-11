# app/tools/location_tool.py
import httpx


async def resolve_start_location(
    client_gps: dict | None = None,
    client_ip: str | None = None,
) -> dict | None:
    """
    Resolve the traveler's starting location.
    Priority: GPS (from client) -> IP geolocation (ip-api.com) -> None.
    A None return tells the Orchestrator to ask the user directly.
    """
    if client_gps and client_gps.get("lat") is not None and client_gps.get("lon") is not None:
        return {
            "lat": client_gps["lat"],
            "lon": client_gps["lon"],
            "source": "gps",
        }

    # IP geolocation fallback — using ip-api.com instead of ipapi.co.
    # Switched after ipapi.co's unkeyed free tier hit HTTP 429 (rate limited)
    # during testing (~1,000 req/day limit). ip-api.com's free tier needs no
    # signup/key and allows 45 req/min instead.
    #
    # Tradeoffs of this choice, for future reference:
    # - HTTP only on the free tier (no HTTPS) — ipapi.co supports HTTPS even
    #   unkeyed. If deployment target blocks outbound plain HTTP, revisit.
    # - Rate limit is per-minute, not per-day — a traffic burst (e.g. a demo
    #   with several concurrent testers) could still hit 429 within a short
    #   window, even though the overall daily volume is small.
    # - No official free HTTPS upgrade path — ipapi.co offers a free signup
    #   tier with a much higher limit AND HTTPS; ip-api.com only gets HTTPS
    #   on a paid plan.
    # If this becomes a real deployment blocker, switch to ipapi.co with a
    # free API key (see settings.ipapi_key) instead of this unkeyed call.

    if client_ip:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"http://ip-api.com/json/{client_ip}")
                resp.raise_for_status()
                data = resp.json()
                if data.get("status") == "success" and data.get("lat") is not None and data.get("lon") is not None:
                    return {
                        "lat": data["lat"],
                        "lon": data["lon"],
                        "source": "ip",
                    }
        except (httpx.HTTPError, httpx.TimeoutException):
            pass

    return None