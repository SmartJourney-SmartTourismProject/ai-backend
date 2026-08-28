# app/tools/geocode_tool.py
#Take a place name and convert it into geographical coordinates 
#(latitude and longitude).
import httpx

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"


async def geocode_destination(destination: str) -> dict | None:
    """
    Resolves a place name to {"lat": float, "lon": float} using Nominatim
    (OpenStreetMap) - free, keyless, no settings.py entry needed.
    Biased toward Sri Lanka since that's this app's scope. Returns None on
    any failure (bad name, network issue, no match) - never raises, so the
    Orchestrator can degrade gracefully same as every other tool.
    """
    try:
        async with httpx.AsyncClient(
            timeout=5.0, headers={"User-Agent": "SmartTourismAI/1.0"}
        ) as client:
            resp = await client.get(NOMINATIM_URL, params={
                "q": f"{destination}, Sri Lanka",
                "format": "json",
                "limit": 1,
            })
            resp.raise_for_status()
            data = resp.json()
            if data:
                return {"lat": float(data[0]["lat"]), "lon": float(data[0]["lon"])}
    except Exception:
        pass
    return None
