from typing import List, Optional

# Placeholder data — swap these function bodies for Supabase calls later.
# Keep the function signatures the same so nothing else in the codebase changes.

_MOCK_LISTINGS = {
    "kandy": {
        "hotel": [
            {"name": "Hilltop Kandy Residency", "description": "Lake-view hotel near the Temple of the Tooth", "price_range": "$$"},
            {"name": "Riverside Kandy Inn", "description": "Budget-friendly, riverside", "price_range": "$"},
        ],
        "restaurant": [
            {"name": "The Empire Cafe", "description": "Local Sri Lankan cuisine, popular with tourists", "price_range": "$"},
        ],
        "attraction": [
            {"name": "Temple of the Sacred Tooth Relic", "description": "Iconic Buddhist temple", "price_range": "$"},
            {"name": "Royal Botanical Gardens", "description": "Large botanical garden, good for families", "price_range": "$"},
        ],
    }
}


async def get_hotels(destination: str, interests: Optional[List[str]] = None) -> List[dict]:
    return _MOCK_LISTINGS.get(destination.lower(), {}).get("hotel", [])


async def get_restaurants(destination: str, interests: Optional[List[str]] = None) -> List[dict]:
    return _MOCK_LISTINGS.get(destination.lower(), {}).get("restaurant", [])


async def get_attractions(destination: str, interests: Optional[List[str]] = None) -> List[dict]:
    return _MOCK_LISTINGS.get(destination.lower(), {}).get("attraction", [])


async def get_events(destination: str, start_date, end_date) -> List[dict]:
    return []  # no mock events yet — fine, Recommendation Agent should handle empty lists gracefully