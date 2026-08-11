import time
import math
import requests
from typing import Any

OVERPASS_URL = "https://overpass-api.de/api/interpreter"

# The 3 launch districts agreed upon for the platform
TARGET_DISTRICTS = ["Kandy District", "Colombo District", "Galle District"]

def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculates the distance in kilometers between two points on Earth."""
    R = 6371.0 # Earth radius in km
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

def process_district(district_name: str, session: requests.Session) -> list[dict[str, Any]]:
    """Fetches and formats listings and local transit for a specific district."""
    print(f"\nFetching listings for {district_name} (this may take 1-2 minutes)...")
    
    # 1. Fetch Listings
    listing_query = build_listing_query(district_name)
    listing_data = fetch_overpass_data(listing_query, session)
    listings_elements = listing_data.get("elements", [])
    print(f"  -> Found {len(listings_elements)} listings.")

    if not listings_elements:
        return []

    time.sleep(3) # Polite pause between API calls
    
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
            "is_verified": False 
        }
        
        processed_listings.append(listing)
            
    return processed_listings

def run_ingestion() -> None:
    print("--- STARTING OVERPASS DATA INGESTION ---")
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
        time.sleep(10) # Big pause between districts
        
    print(f"\n[SUCCESS] Ingestion complete. Total named listings structured for database: {len(all_listings)}")
    
    if all_listings:
        print("\nSample Output Data:")
        print(all_listings[0])

if __name__ == "__main__":
    run_ingestion()