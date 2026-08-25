"""
Monthly hotel/restaurant/attraction refresh.

Pulls base listings from OpenStreetMap Overpass (free, no key) for every
Sri Lanka district, enriches hotels with real nightly prices from the
Booking.com API (via RapidAPI), and upserts everything into Supabase's
`travel_listing` table. Run via app/scheduler.py (monthly) or manually:

    python -m app.data.overpass_ingest
"""
from __future__ import annotations
import time
import math
import logging
from datetime import date, timedelta
from typing import Any, Optional

import requests

from app.config.settings import settings
from app.data.sri_lanka_districts import DISTRICTS
from app.data.supabase_writer import (
    get_category_id_map,
    get_district_id_map,
    has_column,
    to_point_wkt,
    upsert_rows,
)

logger = logging.getLogger(__name__)

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
BOOKING_DESTINATION_URL_TMPL = "https://{host}/api/v1/hotels/searchDestination"
BOOKING_SEARCH_URL_TMPL = "https://{host}/api/v1/hotels/searchHotels"

# Every district is now in scope (see app/data/sri_lanka_districts.py).
# Kept as a module-level name for backwards compatibility with any callers
# that still import TARGET_DISTRICTS directly.
TARGET_DISTRICTS = [d["osm_name"] for d in DISTRICTS]


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculates the distance in kilometers between two points on Earth."""
    R = 6371.0  # Earth radius in km
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2)**2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2)**2)
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c


def build_listing_query(district_name: str) -> str:
    """Builds the Overpass QL query with a 180s timeout and optimized admin_level=5 area search."""
    return f"""
    [out:json][timeout:180];
    area["name"="{district_name}"]["admin_level"="5"]->.searchArea;
    (
      node["tourism"="hotel"](area.searchArea);
      node["amenity"="restaurant"](area.searchArea);
      node["tourism"="attraction"](area.searchArea);
      node["historic"](area.searchArea);
    );
    out body;
    """


def build_district_transit_query(district_name: str) -> str:
    """Builds the Overpass QL query for transit with a 180s timeout."""
    return f"""
    [out:json][timeout:180];
    area["name"="{district_name}"]["admin_level"="5"]->.searchArea;
    (
      node["amenity"="bus_station"](area.searchArea);
      node["railway"="station"](area.searchArea);
      node["public_transport"="station"](area.searchArea);
    );
    out body;
    """


def fetch_overpass_data(query: str, session: requests.Session) -> dict[str, Any]:
    """Executes a query against the Overpass API with retry logic and longer Python timeout."""
    try:
        # Added timeout=200 to tell Python to wait patiently for the server
        response = session.post(OVERPASS_URL, data=query, timeout=200)

        if response.status_code == 429:
            print("  [!] Rate limited. Sleeping for 15 seconds...")
            time.sleep(15)
            response = session.post(OVERPASS_URL, data=query, timeout=200)

        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"  [!] Overpass API error: {e}")
        return {}


def _booking_headers() -> dict[str, str]:
    return {
        "X-RapidAPI-Key": settings.booking_rapidapi_key,
        "X-RapidAPI-Host": settings.booking_rapidapi_host,
    }


def _find_destination_id(district_name: str, session: requests.Session) -> Optional[dict]:
    """
    Resolves a district name (e.g. "Kandy") to a Booking.com dest_id/search_type
    pair via searchDestination. Prefers the Sri Lanka match with the most
    hotels (searchDestination can return same-named places in other countries).
    """
    url = BOOKING_DESTINATION_URL_TMPL.format(host=settings.booking_rapidapi_host)
    try:
        resp = session.get(url, headers=_booking_headers(), params={"query": district_name}, timeout=30)
        resp.raise_for_status()
        candidates = resp.json().get("data", [])
    except requests.exceptions.RequestException as e:
        print(f"  [!] Booking.com destination lookup failed: {e}")
        return None

    sri_lanka_matches = [c for c in candidates if c.get("country") == "Sri Lanka"]
    if not sri_lanka_matches:
        return None

    best = max(sri_lanka_matches, key=lambda c: c.get("hotels", 0))
    return {"dest_id": best["dest_id"], "search_type": best.get("search_type", "city")}


def fetch_booking_prices(district_name: str, session: requests.Session) -> list[dict[str, Any]]:
    """
    Fetches real nightly hotel prices for a district from the Booking.com API
    (RapidAPI, booking-com15 host). One destination lookup + one hotel search
    per district keeps this well inside free-tier limits instead of one call
    per hotel. Returns [] if no key is configured or any call fails - price
    enrichment is best-effort, never blocks the base Overpass sync.
    """
    if not settings.booking_rapidapi_key:
        return []

    destination = _find_destination_id(district_name, session)
    if destination is None:
        print(f"  [!] Booking.com has no Sri Lanka destination match for '{district_name}'.")
        return []

    checkin = date.today() + timedelta(days=30)
    checkout = checkin + timedelta(days=1)

    url = BOOKING_SEARCH_URL_TMPL.format(host=settings.booking_rapidapi_host)
    params = {
        "dest_id": destination["dest_id"],
        "search_type": destination["search_type"],
        "arrival_date": checkin.isoformat(),
        "departure_date": checkout.isoformat(),
        "adults": "2",
        "room_qty": "1",
        "page_number": "1",
        "currency_code": "USD",
    }

    try:
        resp = session.get(url, headers=_booking_headers(), params=params, timeout=30)
        resp.raise_for_status()
        hotels = resp.json().get("data", {}).get("hotels", [])
    except requests.exceptions.RequestException as e:
        print(f"  [!] Booking.com hotel search failed: {e}")
        return []

    results = []
    for hotel in hotels:
        prop = hotel.get("property", {})
        price = prop.get("priceBreakdown", {}).get("grossPrice", {})
        if price.get("value") is None:
            continue
        results.append({
            "name": prop.get("name", ""),
            "lat": prop.get("latitude"),
            "lon": prop.get("longitude"),
            "price_per_night": price["value"],
            "currency": price.get("currency", "USD"),
        })
    return results


def attach_prices(listings: list[dict[str, Any]], booking_hotels: list[dict[str, Any]]) -> None:
    """
    Matches each Overpass hotel listing to the nearest Booking.com result
    within 1km and attaches real price_per_night/currency in place.
    Listings with no match within range keep price_per_night = None.
    """
    for listing in listings:
        if listing["category"] != "hotel":
            continue

        best_dist = 1.0  # km match radius
        best_match: Optional[dict] = None
        for bh in booking_hotels:
            if bh.get("lat") is None or bh.get("lon") is None:
                continue
            dist = haversine(listing["lat"], listing["lon"], bh["lat"], bh["lon"])
            if dist < best_dist:
                best_dist = dist
                best_match = bh

        if best_match:
            listing["price_per_night"] = best_match["price_per_night"]
            listing["currency"] = best_match["currency"]
        else:
            listing["price_per_night"] = None
            listing["currency"] = None


def process_district(district_name: str, session: requests.Session) -> list[dict[str, Any]]:
    """Fetches and formats listings, local transit, and hotel prices for a specific district."""
    print(f"\nFetching listings for {district_name} (this may take 1-2 minutes)...")

    # 1. Fetch Listings
    listing_query = build_listing_query(district_name)
    listing_data = fetch_overpass_data(listing_query, session)
    listings_elements = listing_data.get("elements", [])
    print(f"  -> Found {len(listings_elements)} listings.")

    if not listings_elements:
        return []

    time.sleep(3)  # Polite pause between API calls

    # 2. Fetch Transit Stops
    transit_query = build_district_transit_query(district_name)
    transit_data = fetch_overpass_data(transit_query, session)
    transit_elements = transit_data.get("elements", [])
    print(f"  -> Found {len(transit_elements)} transit stops.")

    processed_listings = []

    for el in listings_elements:
        if "tags" not in el or "name" not in el["tags"]:
            continue

        tags = el["tags"]
        lat = el["lat"]
        lon = el["lon"]

        category = "attraction"
        if tags.get("tourism") == "hotel":
            category = "hotel"
        elif tags.get("amenity") == "restaurant":
            category = "restaurant"

        has_transit = False
        nearest_stop = None
        min_dist = 1.0

        for t_el in transit_elements:
            if "lat" in t_el and "lon" in t_el:
                dist = haversine(lat, lon, t_el["lat"], t_el["lon"])
                if dist < min_dist:
                    min_dist = dist
                    has_transit = True
                    nearest_stop = t_el.get("tags", {}).get("name", "Unnamed Station")

        listing = {
            "name": tags["name"],
            "category": category,
            "district": district_name.replace(" District", ""),
            "lat": lat,
            "lon": lon,
            "source": "overpass",
            "external_ref": str(el["id"]),
            "has_public_transit": has_transit,
            "nearest_transit_stop": nearest_stop,
            "is_verified": False,
        }

        processed_listings.append(listing)

    # 3. Enrich hotel prices via Booking.com (one destination lookup + one search per district)
    district_centroid = next(
        (d for d in DISTRICTS if d["osm_name"] == district_name), None
    )
    if district_centroid:
        time.sleep(2)
        booking_hotels = fetch_booking_prices(district_centroid["name"], session)
        if booking_hotels:
            print(f"  -> Matched against {len(booking_hotels)} Booking.com hotel prices.")
        attach_prices(processed_listings, booking_hotels)

    return processed_listings


def _price_range_bucket(price_per_night: Optional[float]) -> Optional[str]:
    """Buckets a real USD nightly price into the $/$$/$$$/$$$$ scale the schema already uses."""
    if price_per_night is None:
        return None
    if price_per_night < 30:
        return "$"
    if price_per_night < 70:
        return "$$"
    if price_per_night < 150:
        return "$$$"
    return "$$$$"


def build_db_rows(listings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Maps our internal listing shape (flat district/category/lat/lon) onto
    `travel_listing`'s real schema: district_id/category_id foreign keys and
    a PostGIS `location` point. Listings whose district/category can't be
    resolved are dropped (logged) rather than sent with a broken row.
    """
    district_map = get_district_id_map()
    category_map = get_category_id_map()
    include_price_columns = has_column("travel_listing", "price_per_night")

    rows = []
    for listing in listings:
        district_id = district_map.get(listing["district"])
        category_id = category_map.get(listing["category"])
        if district_id is None or category_id is None:
            print(f"  [!] Skipping '{listing['name']}' - unknown district/category, not synced.")
            continue

        row = {
            "district_id": district_id,
            "category_id": category_id,
            "name": listing["name"],
            "location": to_point_wkt(listing["lat"], listing["lon"]),
            "price_range": _price_range_bucket(listing.get("price_per_night")),
            "source": listing["source"],
            "external_ref": listing["external_ref"],
            "has_public_transit": listing["has_public_transit"],
            "nearest_transit_stop": listing["nearest_transit_stop"],
            "is_verified": listing["is_verified"],
        }
        if include_price_columns:
            row["price_per_night"] = listing.get("price_per_night")
            row["currency"] = listing.get("currency")
        rows.append(row)

    if not include_price_columns:
        print(
            "  [!] 'travel_listing' has no price_per_night/currency columns yet - "
            "real Booking.com prices are only stored as the $/$$/$$$ bucket. "
            "Run the migration in the project's setup notes to store exact prices."
        )

    return rows


def run_ingestion() -> None:
    print("--- STARTING MONTHLY HOTEL/RESTAURANT/ATTRACTION INGESTION (all 25 districts) ---")
    session = requests.Session()

    session.headers.update({
        "User-Agent": "SmartJourney-AI-Backend/1.0",
        "Accept": "application/json"
    })

    all_listings = []

    for district in TARGET_DISTRICTS:
        listings = process_district(district, session)
        all_listings.extend(listings)
        print("  -> Sleeping for 10 seconds before next district...")
        time.sleep(10)  # Big pause between districts

    print(f"\n[SUCCESS] Ingestion complete. Total named listings structured for database: {len(all_listings)}")

    db_rows = build_db_rows(all_listings)
    synced = upsert_rows("travel_listing", db_rows, on_conflict="external_ref")
    if synced:
        print(f"[SUCCESS] Upserted {synced} rows into Supabase 'travel_listing'.")
    else:
        print("[WARN] Supabase not configured (or upsert failed) - rows were not persisted.")

    if all_listings:
        print("\nSample Output Data:")
        print(all_listings[0])


if __name__ == "__main__":
    run_ingestion()
