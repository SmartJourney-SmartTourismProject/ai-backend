from typing import List, Optional

# Placeholder data — swap these function bodies for Supabase calls later.
# Keep the function signatures the same so nothing else in the codebase changes.

_MOCK_LISTINGS = {
    "kandy": {
        "hotel": [
            {
                "id": "h1-kandy-hilltop", "name": "Hilltop Kandy Residency",
                "description": "Lake-view hotel near the Temple of the Tooth",
                "price_range": "$$", "lat": 7.2936, "lon": 80.6414,
                "rating": 4.3, "photo_url": "", "opening_hours": "24 hours",
                "has_public_transit": True, "nearest_transit_stop": "Kandy Railway Station",
                "pickme_available": True,
            },
            {
                "id": "h2-kandy-riverside", "name": "Riverside Kandy Inn",
                "description": "Budget-friendly, riverside",
                "price_range": "$", "lat": 7.2906, "lon": 80.6337,
                "rating": 3.9, "photo_url": "", "opening_hours": "24 hours",
                "has_public_transit": False, "nearest_transit_stop": None,
                "pickme_available": True,
            },
        ],
        "restaurant": [
            {
                "id": "r1-kandy-empire", "name": "The Empire Cafe",
                "description": "Local Sri Lankan cuisine, popular with tourists",
                "price_range": "$", "lat": 7.2921, "lon": 80.6350,
                "rating": 4.1, "photo_url": "", "opening_hours": "08:00-21:00",
                "has_public_transit": True, "nearest_transit_stop": "Kandy Bus Stand",
                "pickme_available": True,
            },
        ],
        "attraction": [
            {
                "id": "a1-kandy-temple", "name": "Temple of the Sacred Tooth Relic",
                "description": "Iconic Buddhist temple",
                "price_range": "$", "lat": 7.2936, "lon": 80.6413,
                "rating": 4.7, "photo_url": "", "opening_hours": "05:30-20:00",
                "has_public_transit": True, "nearest_transit_stop": "Kandy Bus Stand",
                "pickme_available": True,
            },
            {
                "id": "a2-kandy-gardens", "name": "Royal Botanical Gardens",
                "description": "Large botanical garden, good for families",
                "price_range": "$", "lat": 7.2716, "lon": 80.5977,
                "rating": 4.6, "photo_url": "", "opening_hours": "07:30-17:00",
                "has_public_transit": False, "nearest_transit_stop": None,
                "pickme_available": True,
            },
        ],
    }
}

_MOCK_EVENTS = {
    "kandy": [
        {
            "id": "e1-kandy-perahera", "name": "Kandy Esala Perahera",
            "description": "Grand annual Buddhist festival with processions",
            "start_datetime": "2026-08-20T19:00:00", "end_datetime": "2026-08-20T22:00:00",
            "venue_name": "Temple of the Sacred Tooth Relic", "price_info": {"free": True},
        },
    ]
}

_MOCK_USER_PROFILES = {
    "demo-user-1": {
        "interests": ["culture", "food"],
        "travel_style": "budget",
        "budget": 300.0,
        "home_location": {"lat": 6.9271, "lon": 79.8612},  # Colombo
    }
}


async def get_hotels(destination: str, interests: Optional[List[str]] = None) -> List[dict]:
    return _MOCK_LISTINGS.get(destination.lower(), {}).get("hotel", [])


async def get_restaurants(destination: str, interests: Optional[List[str]] = None) -> List[dict]:
    return _MOCK_LISTINGS.get(destination.lower(), {}).get("restaurant", [])


async def get_attractions(destination: str, interests: Optional[List[str]] = None) -> List[dict]:
    return _MOCK_LISTINGS.get(destination.lower(), {}).get("attraction", [])


async def get_events(destination: str, start_date: str, end_date: str) -> List[dict]:
    return _MOCK_EVENTS.get(destination.lower(), [])


async def get_transit_info(listing_id: str) -> dict:
    for category in _MOCK_LISTINGS.values():
        for listings in category.values():
            for listing in listings:
                if listing["id"] == listing_id:
                    return {
                        "has_public_transit": listing["has_public_transit"],
                        "nearest_transit_stop": listing["nearest_transit_stop"],
                    }
    return {"has_public_transit": False, "nearest_transit_stop": None}


async def check_pickme_coverage(lat: float, lon: float) -> bool:
    return True  # mock: assume covered everywhere until real geofencing lands


async def get_user_profile(user_id: str) -> dict:
    return _MOCK_USER_PROFILES.get(
        user_id,
        {"interests": [], "travel_style": None, "budget": None, "home_location": None},
    )