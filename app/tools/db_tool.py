

import logging
import os
from typing import List, Optional

# Try to import supabase, but make it optional for testing
try:
    from supabase import acreate_client, AsyncClient
    _SUPABASE_AVAILABLE = True
except ImportError:
    _SUPABASE_AVAILABLE = False
    acreate_client = None
    AsyncClient = None

logger = logging.getLogger(__name__)

# --- Configuration -------------------------------------------------------
# Fail gracefully if config is missing (use mock data for testing)
_SUPABASE_URL = os.environ.get("SUPABASE_URL")
_SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

_client: Optional[AsyncClient] = None


async def _get_client() -> Optional[AsyncClient]:
    """Lazily create and cache a single AsyncClient for the process."""
    global _client
    if not _SUPABASE_AVAILABLE or not _SUPABASE_URL or not _SUPABASE_KEY:
        return None
    if _client is None:
        _client = await acreate_client(_SUPABASE_URL, _SUPABASE_KEY)
    return _client


# --- Mock Data for Testing -----------------------------------------------

_MOCK_HOTELS = {
    "ella": [
        {"id": "h1", "name": "Mountain View Hotel", "category": "hotel", "destination": "Ella", "price_range": "$$", "rating": 4.5, "interests": ["nature", "views", "hiking"], "description": "Beautiful hotel with mountain views"},
        {"id": "h2", "name": "Ella Flower Garden Resort", "category": "hotel", "destination": "Ella", "price_range": "$$$", "rating": 4.7, "interests": ["luxury", "nature", "relaxation"], "description": "Luxury resort with flower gardens"},
        {"id": "h3", "name": "Backpacker's Hostel Ella", "category": "hotel", "destination": "Ella", "price_range": "$", "rating": 4.0, "interests": ["budget", "social", "hiking"], "description": "Affordable hostel for backpackers"},
    ],
    "kandy": [
        {"id": "h4", "name": "Kandy Lake View Hotel", "category": "hotel", "destination": "Kandy", "price_range": "$$", "rating": 4.3, "interests": ["culture", "views", "history"], "description": "Hotel overlooking Kandy Lake"},
        {"id": "h5", "name": "Theva Residency", "category": "hotel", "destination": "Kandy", "price_range": "$$$", "rating": 4.6, "interests": ["luxury", "culture", "spa"], "description": "Luxury hotel near Temple of Tooth"},
    ],
    "colombo": [
        {"id": "h6", "name": "Galle Face Hotel", "category": "hotel", "destination": "Colombo", "price_range": "$$$$", "rating": 4.8, "interests": ["luxury", "history", "beach"], "description": "Iconic colonial luxury hotel"},
        {"id": "h7", "name": "CityRest Fort", "category": "hotel", "destination": "Colombo", "price_range": "$$", "rating": 4.2, "interests": ["budget", "city", "business"], "description": "Modern hotel in Fort district"},
    ],
    "galle": [
        {"id": "h8", "name": "Jetwing Lighthouse", "category": "hotel", "destination": "Galle", "price_range": "$$$$", "rating": 4.7, "interests": ["luxury", "beach", "history"], "description": "Luxury resort designed by Geoffrey Bawa"},
        {"id": "h9", "name": "Fort Bazaar", "category": "hotel", "destination": "Galle", "price_range": "$$$", "rating": 4.5, "interests": ["boutique", "history", "culture"], "description": "Boutique hotel in Galle Fort"},
    ],
}

_MOCK_RESTAURANTS = {
    "ella": [
        {"id": "r1", "name": "Cafe Chill", "category": "restaurant", "destination": "Ella", "price_range": "$", "rating": 4.4, "interests": ["local cuisine", "vegetarian", "budget"], "description": "Popular spot for Sri Lankan food"},
        {"id": "r2", "name": "360 Ella", "category": "restaurant", "destination": "Ella", "price_range": "$$", "rating": 4.6, "interests": ["views", "fine dining", "sunset"], "description": "Restaurant with 360-degree views"},
        {"id": "r3", "name": "Dream Cafe", "category": "restaurant", "destination": "Ella", "price_range": "$", "rating": 4.3, "interests": ["western", "breakfast", "coffee"], "description": "Cozy cafe with good coffee"},
    ],
    "kandy": [
        {"id": "r4", "name": "The Empire Cafe", "category": "restaurant", "destination": "Kandy", "price_range": "$$", "rating": 4.4, "interests": ["colonial", "tea", "history"], "description": "Historic cafe in colonial building"},
        {"id": "r5", "name": "Slightly Chilled Lounge Bar", "category": "restaurant", "destination": "Kandy", "price_range": "$$$", "rating": 4.5, "interests": ["fine dining", "cocktails", "views"], "description": "Upscale dining with lake views"},
    ],
    "colombo": [
        {"id": "r6", "name": "Ministry of Crab", "category": "restaurant", "destination": "Colombo", "price_range": "$$$$", "rating": 4.8, "interests": ["seafood", "fine dining", "celebrity chef"], "description": "World-renowned crab restaurant"},
        {"id": "r7", "name": "Upali's by Nawaloka", "category": "restaurant", "destination": "Colombo", "price_range": "$$$", "rating": 4.6, "interests": ["traditional", "sri lankan", "family"], "description": "Authentic Sri Lankan cuisine"},
    ],
    "galle": [
        {"id": "r8", "name": "The Tuna & The Crab", "category": "restaurant", "destination": "Galle", "price_range": "$$$", "rating": 4.7, "interests": ["seafood", "fine dining", "beach"], "description": "Seafood restaurant in Galle Fort"},
        {"id": "r9", "name": "Crepe-ology", "category": "restaurant", "destination": "Galle", "price_range": "$$", "rating": 4.4, "interests": ["crepes", "breakfast", "dessert"], "description": "Popular crepe restaurant"},
    ],
}

_MOCK_ATTRACTIONS = {
    "ella": [
        {"id": "a1", "name": "Little Adam's Peak", "category": "attraction", "destination": "Ella", "price_range": "Free", "rating": 4.7, "interests": ["hiking", "views", "easy"], "description": "Easy hike with stunning panoramic views"},
        {"id": "a2", "name": "Nine Arches Bridge", "category": "attraction", "destination": "Ella", "price_range": "Free", "rating": 4.8, "interests": ["photography", "history", "train"], "description": "Iconic colonial railway bridge"},
        {"id": "a3", "name": "Ella Rock", "category": "attraction", "destination": "Ella", "price_range": "Free", "rating": 4.6, "interests": ["hiking", "challenging", "sunrise"], "description": "Challenging hike with rewarding views"},
        {"id": "a4", "name": "Ravana Falls", "category": "attraction", "destination": "Ella", "price_range": "Free", "rating": 4.5, "interests": ["waterfall", "swimming", "nature"], "description": "Beautiful waterfall with swimming spots"},
    ],
    "kandy": [
        {"id": "a5", "name": "Temple of the Sacred Tooth Relic", "category": "attraction", "destination": "Kandy", "price_range": "$", "rating": 4.8, "interests": ["culture", "history", "religion", "unesco"], "description": "UNESCO World Heritage Buddhist temple"},
        {"id": "a6", "name": "Royal Botanical Gardens Peradeniya", "category": "attraction", "destination": "Kandy", "price_range": "$", "rating": 4.7, "interests": ["nature", "botany", "walking"], "description": "Extensive botanical gardens"},
        {"id": "a7", "name": "Kandy Lake", "category": "attraction", "destination": "Kandy", "price_range": "Free", "rating": 4.4, "interests": ["walking", "views", "relaxation"], "description": "Scenic lake in city center"},
    ],
    "colombo": [
        {"id": "a8", "name": "Galle Face Green", "category": "attraction", "destination": "Colombo", "price_range": "Free", "rating": 4.5, "interests": ["beach", "sunset", "street food", "walking"], "description": "Urban park along the coast"},
        {"id": "a9", "name": "Gangaramaya Temple", "category": "attraction", "destination": "Colombo", "price_range": "$", "rating": 4.6, "interests": ["culture", "religion", "architecture"], "description": "Important Buddhist temple complex"},
        {"id": "a10", "name": "National Museum Colombo", "category": "attraction", "destination": "Colombo", "price_range": "$", "rating": 4.3, "interests": ["history", "museum", "culture"], "description": "Largest museum in Sri Lanka"},
    ],
    "galle": [
        {"id": "a11", "name": "Galle Fort", "category": "attraction", "destination": "Galle", "price_range": "Free", "rating": 4.8, "interests": ["history", "unesco", "architecture", "walking"], "description": "UNESCO World Heritage Dutch fort"},
        {"id": "a12", "name": "Jungle Beach", "category": "attraction", "destination": "Galle", "price_range": "Free", "rating": 4.5, "interests": ["beach", "swimming", "nature", "hidden gem"], "description": "Secluded beach near Galle Fort"},
        {"id": "a13", "name": "Sea Turtle Hatchery", "category": "attraction", "destination": "Galle", "price_range": "$", "rating": 4.4, "interests": ["wildlife", "conservation", "family"], "description": "Turtle conservation center"},
    ],
}

_MOCK_EVENTS = {
    "ella": [
        {"id": "e1", "title": "Ella Sunday Market", "destination": "Ella", "category": "market", "start_datetime": "2026-08-24T08:00:00", "end_datetime": "2026-08-24T14:00:00", "description": "Weekly market with local crafts and food"},
        {"id": "e2", "title": "Ella Music Festival", "destination": "Ella", "category": "festival", "start_datetime": "2026-09-15T18:00:00", "end_datetime": "2026-09-15T23:00:00", "description": "Annual music festival in the hills"},
    ],
    "kandy": [
        {"id": "e3", "title": "Kandy Esala Perahera", "destination": "Kandy", "category": "festival", "start_datetime": "2026-08-20T19:00:00", "end_datetime": "2026-08-30T23:00:00", "description": "Grand Buddhist festival with processions"},
        {"id": "e4", "title": "Kandy Food Festival", "destination": "Kandy", "category": "food", "start_datetime": "2026-09-10T10:00:00", "end_datetime": "2026-09-12T22:00:00", "description": "Food festival showcasing local cuisine"},
    ],
    "colombo": [
        {"id": "e5", "title": "Colombo International Book Fair", "destination": "Colombo", "category": "exhibition", "start_datetime": "2026-09-18T09:00:00", "end_datetime": "2026-09-28T20:00:00", "description": "Annual book fair at BMICH"},
        {"id": "e6", "title": "Colombo Jazz Festival", "destination": "Colombo", "category": "music", "start_datetime": "2026-10-05T18:00:00", "end_datetime": "2026-10-05T23:00:00", "description": "International jazz festival"},
    ],
    "galle": [
        {"id": "e7", "title": "Galle Literary Festival", "destination": "Galle", "category": "literary", "start_datetime": "2026-01-15T09:00:00", "end_datetime": "2026-01-19T20:00:00", "description": "Annual literary festival in Galle Fort"},
        {"id": "e8", "title": "Galle Art Walk", "destination": "Galle", "category": "art", "start_datetime": "2026-08-30T16:00:00", "end_datetime": "2026-08-30T22:00:00", "description": "Evening art walk through Galle Fort"},
    ],
}


def _get_mock_data(data_dict: dict, destination: str, interests: Optional[List[str]] = None) -> List[dict]:
    """Get mock data for a destination, optionally filtered by interests."""
    dest_key = destination.lower().strip()
    items = data_dict.get(dest_key, [])
    
    if interests:
        # Filter by interests overlap
        interest_set = set(i.lower() for i in interests)
        filtered = []
        for item in items:
            item_interests = set(i.lower() for i in item.get("interests", []))
            if interest_set & item_interests:  # Intersection not empty
                filtered.append(item)
        return filtered
    
    return items


# --- Listings (hotels / restaurants / attractions) ------------------------

async def _get_listings(
    destination: str,
    category: str,
    interests: Optional[List[str]] = None,
) -> List[dict]:
    """
    Shared implementation for hotels/restaurants/attractions.
    Tries Supabase first, falls back to mock data.
    """
    # Try Supabase if available
    if _SUPABASE_AVAILABLE and _SUPABASE_URL and _SUPABASE_KEY:
        try:
            client = await _get_client()
            if client:
                query = (
                    client.table("listings")
                    .select("*")
                    .ilike("destination", destination)
                    .eq("category", category)
                )
                if interests:
                    query = query.overlaps("interests", interests)
                response = await query.execute()
                if response.data:
                    return response.data
        except Exception as e:
            logger.warning(f"Supabase query failed, using mock data: {e}")
    
    # Fallback to mock data
    mock_data = {
        "hotel": _MOCK_HOTELS,
        "restaurant": _MOCK_RESTAURANTS,
        "attraction": _MOCK_ATTRACTIONS,
    }
    return _get_mock_data(mock_data.get(category, {}), destination, interests)


async def get_hotels(destination: str, interests: Optional[List[str]] = None) -> List[dict]:
    return await _get_listings(destination, "hotel", interests)


async def get_restaurants(destination: str, interests: Optional[List[str]] = None) -> List[dict]:
    return await _get_listings(destination, "restaurant", interests)


async def get_attractions(destination: str, interests: Optional[List[str]] = None) -> List[dict]:
    return await _get_listings(destination, "attraction", interests)


# --- Events ----------------------------------------------------------------

async def get_events(destination: str, start_date: str, end_date: str) -> List[dict]:
    """
    Returns events in destination whose window overlaps [start_date, end_date].
    Falls back to mock data if Supabase unavailable.
    """
    if _SUPABASE_AVAILABLE and _SUPABASE_URL and _SUPABASE_KEY:
        try:
            client = await _get_client()
            if client:
                response = (
                    await client.table("events")
                    .select("*")
                    .ilike("destination", destination)
                    .lte("start_datetime", end_date)
                    .gte("end_datetime", start_date)
                    .execute()
                )
                if response.data:
                    return response.data
        except Exception as e:
            logger.warning(f"Supabase events query failed, using mock data: {e}")
    
    # Fallback to mock data
    dest_key = destination.lower().strip()
    events = _MOCK_EVENTS.get(dest_key, [])
    # Filter by date overlap (simplified)
    return events


# --- Transit info ------------------------------------------------------------

async def get_transit_info(listing_id: str) -> dict:
    default = {"has_public_transit": False, "nearest_transit_stop": None}
    return default


# --- PickMe / rideshare coverage ---------------------------------------------

async def check_pickme_coverage(lat: float, lon: float) -> bool:
    return True


# --- User profile --------------------------------------------------------

async def get_user_profile(user_id: str) -> dict:
    default = {"interests": [], "travel_style": None, "budget": None, "home_location": None}
    return default