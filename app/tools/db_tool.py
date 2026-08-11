from __future__ import annotations

from typing import Any, Dict, List, Optional


async def get_hotels(destination: str, interests: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    # Hardcoded sample hotels for the mock
    return [
        {
            "id": "hotel-1",
            "name": "Hilltop Hotel",
            "description": "Comfortable hotel with scenic views",
            "price_range": "$$",
            "lat": 7.2906,
            "lon": 80.6337,
            "rating": 4.2,
            "photo_url": "https://example.com/hilltop.jpg",
            "opening_hours": "24/7",
            "has_public_transit": True,
            "nearest_transit_stop": "Central Bus Station",
            "pickme_available": True,
        }
    ]


async def get_restaurants(destination: str, interests: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    return [
        {
            "id": "rest-1",
            "name": "Spice Garden",
            "description": "Local cuisine",
            "price_range": "$",
            "lat": 7.2910,
            "lon": 80.6340,
            "rating": 4.5,
            "photo_url": "https://example.com/spice.jpg",
            "opening_hours": "10:00-22:00",
            "has_public_transit": True,
            "nearest_transit_stop": "Market Stop",
            "pickme_available": True,
        }
    ]


async def get_attractions(destination: str, interests: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    return [
        {
            "id": "attr-1",
            "name": "Old Temple",
            "description": "Historic site",
            "price_range": "$",
            "lat": 7.2920,
            "lon": 80.6350,
            "rating": 4.7,
            "photo_url": "https://example.com/temple.jpg",
            "opening_hours": "06:00-18:00",
            "has_public_transit": False,
            "nearest_transit_stop": "Temple Gate",
            "pickme_available": False,
        }
    ]


async def get_events(destination: str, start_date: str, end_date: str) -> List[Dict[str, Any]]:
    return [
        {
            "id": "event-1",
            "name": "Cultural Show",
            "description": "Local dance and music",
            "start_datetime": start_date + "T19:00:00",
            "end_datetime": start_date + "T21:00:00",
            "venue_name": "Town Hall",
            "price_info": {"currency": "LKR", "min": 500, "max": 1500},
        }
    ]


async def get_transit_info(listing_id: str) -> Dict[str, Any]:
    return {"listing_id": listing_id, "next_bus_minutes": 12, "routes": ["A1", "B2"]}


async def check_pickme_coverage(lat: float, lon: float) -> bool:
    # Mock: return True for central coordinates
    return True if (7.0 <= lat <= 8.0 and 80.0 <= lon <= 81.0) else False


async def get_user_profile(user_id: str) -> Dict[str, Any]:
    return {
        "user_id": user_id,
        "interests": ["nature", "history"],
        "travel_style": "relaxed",
        "budget": 500.0,
        "home_location": {"lat": 7.2906, "lon": 80.6337},
    }
