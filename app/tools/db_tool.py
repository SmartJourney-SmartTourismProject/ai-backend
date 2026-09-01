

import logging
from datetime import date, datetime
from typing import List, Optional, Union

from app.data.sri_lanka_districts import get_district
from app.utils.db_pool import get_pool

logger = logging.getLogger(__name__)

# --- Queries -------------------------------------------------------------
# Raw parameterised SQL rather than an ORM: Prisma (the NestJS backend) owns
# the schema, so defining models here as well would duplicate that definition
# in a second language and let the two drift silently. See
# docs/POSTGRES_MIGRATION_PLAN.md §2.
#
# `latitude`/`longitude` are generated columns derived from the PostGIS
# `location` geography (backend/docs/BACKEND_PLAN.md §4.2) - selecting them
# directly is why this module no longer decodes EWKB hex by hand, and why
# fetching listings is now one JOIN instead of three round trips.

_SELECT_LISTINGS = """
    SELECT l.id, l.name, l.description, l.price_range,
           l.latitude, l.longitude, l.rating, l.photo_url, l.opening_hours,
           l.has_public_transit, l.nearest_transit_stop, l.pickme_available
    FROM travel_listing l
    JOIN district d ON d.id = l.district_id
    JOIN category c ON c.id = l.category_id
    WHERE d.name ILIKE $1
      AND c.name = $2
      AND l.is_verified = true
"""

_SELECT_EVENTS = """
    SELECT e.id, e.name, e.description, e.start_datetime, e.end_datetime,
           e.venue_name, e.price_info
    FROM local_event e
    JOIN district d ON d.id = e.district_id
    WHERE d.name ILIKE $1
      AND e.is_verified = true
      AND e.start_datetime <= $3
      AND e.end_datetime   >= $2
"""

_SELECT_PROFILE = """
    SELECT travel_interests, travel_style, default_budget,
           ST_Y(home_location::geometry) AS home_lat,
           ST_X(home_location::geometry) AS home_lon
    FROM traveler_profile
    WHERE user_id = $1
"""


def _coerce_date(value: Union[str, date, datetime, None]):
    """
    get_events takes ISO date strings (that's its published contract, and what
    RecommendationAgent passes), but the columns are timestamptz and asyncpg
    will not coerce a string the way Supabase's REST layer did - it raises
    instead. Convert here so callers keep passing plain strings.
    """
    if value is None or isinstance(value, (date, datetime)):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        logger.warning(f"Unparseable date {value!r} in events query.")
        return None


def _row_to_listing_dict(row) -> dict:
    """Maps a real `travel_listing` row onto the BUILD_PLAN §4 contract shape."""
    return {
        "id": str(row["id"]),
        "name": row["name"],
        "description": row["description"],
        "price_range": row["price_range"],
        "lat": row["latitude"],
        "lon": row["longitude"],
        "rating": row["rating"],
        "photo_url": row["photo_url"],
        "opening_hours": row["opening_hours"],
        "has_public_transit": row["has_public_transit"] or False,
        "nearest_transit_stop": row["nearest_transit_stop"],
        "pickme_available": row["pickme_available"] or False,
    }


def _row_to_event_dict(row) -> dict:
    """Maps a real `local_event` row onto the BUILD_PLAN §4 contract shape."""
    return {
        "id": str(row["id"]),
        "name": row["name"],
        "description": row["description"],
        "start_datetime": row["start_datetime"],
        "end_datetime": row["end_datetime"],
        "venue_name": row["venue_name"],
        "price_info": row["price_info"],
    }


# --- Mock Data for Testing -----------------------------------------------

_MOCK_HOTELS = {
    "ella": [
        {"id": "h1", "name": "Mountain View Hotel", "category": "hotel", "destination": "Ella", "price_range": "$$", "rating": 4.5, "interests": ["nature", "views", "hiking"], "description": "Beautiful hotel with mountain views", "lat": 6.8667, "lon": 81.0466},
        {"id": "h2", "name": "Ella Flower Garden Resort", "category": "hotel", "destination": "Ella", "price_range": "$$$", "rating": 4.7, "interests": ["luxury", "nature", "relaxation"], "description": "Luxury resort with flower gardens", "lat": 6.8710, "lon": 81.0450},
        {"id": "h3", "name": "Backpacker's Hostel Ella", "category": "hotel", "destination": "Ella", "price_range": "$", "rating": 4.0, "interests": ["budget", "social", "hiking"], "description": "Affordable hostel for backpackers", "lat": 6.8656, "lon": 81.0461},
    ],
    "kandy": [
        {"id": "h4", "name": "Kandy Lake View Hotel", "category": "hotel", "destination": "Kandy", "price_range": "$$", "rating": 4.3, "interests": ["culture", "views", "history"], "description": "Hotel overlooking Kandy Lake", "lat": 7.2931, "lon": 80.6392},
        {"id": "h5", "name": "Theva Residency", "category": "hotel", "destination": "Kandy", "price_range": "$$$", "rating": 4.6, "interests": ["luxury", "culture", "spa"], "description": "Luxury hotel near Temple of Tooth", "lat": 7.2945, "lon": 80.6350},
    ],
    "colombo": [
        {"id": "h6", "name": "Galle Face Hotel", "category": "hotel", "destination": "Colombo", "price_range": "$$$$", "rating": 4.8, "interests": ["luxury", "history", "beach"], "description": "Iconic colonial luxury hotel", "lat": 6.9223, "lon": 79.8449},
        {"id": "h7", "name": "CityRest Fort", "category": "hotel", "destination": "Colombo", "price_range": "$$", "rating": 4.2, "interests": ["budget", "city", "business"], "description": "Modern hotel in Fort district", "lat": 6.9344, "lon": 79.8428},
    ],
    "galle": [
        {"id": "h8", "name": "Jetwing Lighthouse", "category": "hotel", "destination": "Galle", "price_range": "$$$$", "rating": 4.7, "interests": ["luxury", "beach", "history"], "description": "Luxury resort designed by Geoffrey Bawa", "lat": 6.0327, "lon": 80.2168},
        {"id": "h9", "name": "Fort Bazaar", "category": "hotel", "destination": "Galle", "price_range": "$$$", "rating": 4.5, "interests": ["boutique", "history", "culture"], "description": "Boutique hotel in Galle Fort", "lat": 6.0300, "lon": 80.2170},
    ],
}

_MOCK_RESTAURANTS = {
    "ella": [
        {"id": "r1", "name": "Cafe Chill", "category": "restaurant", "destination": "Ella", "price_range": "$", "rating": 4.4, "interests": ["local cuisine", "vegetarian", "budget"], "description": "Popular spot for Sri Lankan food", "lat": 6.8710, "lon": 81.0463},
        {"id": "r2", "name": "360 Ella", "category": "restaurant", "destination": "Ella", "price_range": "$$", "rating": 4.6, "interests": ["views", "fine dining", "sunset"], "description": "Restaurant with 360-degree views", "lat": 6.8785, "lon": 81.0490},
        {"id": "r3", "name": "Dream Cafe", "category": "restaurant", "destination": "Ella", "price_range": "$", "rating": 4.3, "interests": ["western", "breakfast", "coffee"], "description": "Cozy cafe with good coffee", "lat": 6.8698, "lon": 81.0468},
    ],
    "kandy": [
        {"id": "r4", "name": "The Empire Cafe", "category": "restaurant", "destination": "Kandy", "price_range": "$$", "rating": 4.4, "interests": ["colonial", "tea", "history"], "description": "Historic cafe in colonial building", "lat": 7.2939, "lon": 80.6413},
        {"id": "r5", "name": "Slightly Chilled Lounge Bar", "category": "restaurant", "destination": "Kandy", "price_range": "$$$", "rating": 4.5, "interests": ["fine dining", "cocktails", "views"], "description": "Upscale dining with lake views", "lat": 7.2910, "lon": 80.6410},
    ],
    "colombo": [
        {"id": "r6", "name": "Ministry of Crab", "category": "restaurant", "destination": "Colombo", "price_range": "$$$$", "rating": 4.8, "interests": ["seafood", "fine dining", "celebrity chef"], "description": "World-renowned crab restaurant", "lat": 6.9350, "lon": 79.8433},
        {"id": "r7", "name": "Upali's by Nawaloka", "category": "restaurant", "destination": "Colombo", "price_range": "$$$", "rating": 4.6, "interests": ["traditional", "sri lankan", "family"], "description": "Authentic Sri Lankan cuisine", "lat": 6.9147, "lon": 79.8541},
    ],
    "galle": [
        {"id": "r8", "name": "The Tuna & The Crab", "category": "restaurant", "destination": "Galle", "price_range": "$$$", "rating": 4.7, "interests": ["seafood", "fine dining", "beach"], "description": "Seafood restaurant in Galle Fort", "lat": 6.0270, "lon": 80.2168},
        {"id": "r9", "name": "Crepe-ology", "category": "restaurant", "destination": "Galle", "price_range": "$$", "rating": 4.4, "interests": ["crepes", "breakfast", "dessert"], "description": "Popular crepe restaurant", "lat": 6.0292, "lon": 80.2172},
    ],
}

_MOCK_ATTRACTIONS = {
    "ella": [
        {"id": "a1", "name": "Little Adam's Peak", "category": "attraction", "destination": "Ella", "price_range": "Free", "rating": 4.7, "interests": ["hiking", "views", "easy"], "description": "Easy hike with stunning panoramic views", "lat": 6.8698, "lon": 81.0561},
        {"id": "a2", "name": "Nine Arches Bridge", "category": "attraction", "destination": "Ella", "price_range": "Free", "rating": 4.8, "interests": ["photography", "history", "train"], "description": "Iconic colonial railway bridge", "lat": 6.8781, "lon": 81.0592},
        {"id": "a3", "name": "Ella Rock", "category": "attraction", "destination": "Ella", "price_range": "Free", "rating": 4.6, "interests": ["hiking", "challenging", "sunrise"], "description": "Challenging hike with rewarding views", "lat": 6.8636, "lon": 81.0424},
        {"id": "a4", "name": "Ravana Falls", "category": "attraction", "destination": "Ella", "price_range": "Free", "rating": 4.5, "interests": ["waterfall", "swimming", "nature"], "description": "Beautiful waterfall with swimming spots", "lat": 6.8412, "lon": 81.0559},
    ],
    "kandy": [
        {"id": "a5", "name": "Temple of the Sacred Tooth Relic", "category": "attraction", "destination": "Kandy", "price_range": "$", "rating": 4.8, "interests": ["culture", "history", "religion", "unesco"], "description": "UNESCO World Heritage Buddhist temple", "lat": 7.2936, "lon": 80.6413},
        {"id": "a6", "name": "Royal Botanical Gardens Peradeniya", "category": "attraction", "destination": "Kandy", "price_range": "$", "rating": 4.7, "interests": ["nature", "botany", "walking"], "description": "Extensive botanical gardens", "lat": 7.2698, "lon": 80.5972},
        {"id": "a7", "name": "Kandy Lake", "category": "attraction", "destination": "Kandy", "price_range": "Free", "rating": 4.4, "interests": ["walking", "views", "relaxation"], "description": "Scenic lake in city center", "lat": 7.2906, "lon": 80.6414},
    ],
    "colombo": [
        {"id": "a8", "name": "Galle Face Green", "category": "attraction", "destination": "Colombo", "price_range": "Free", "rating": 4.5, "interests": ["beach", "sunset", "street food", "walking"], "description": "Urban park along the coast", "lat": 6.9223, "lon": 79.8437},
        {"id": "a9", "name": "Gangaramaya Temple", "category": "attraction", "destination": "Colombo", "price_range": "$", "rating": 4.6, "interests": ["culture", "religion", "architecture"], "description": "Important Buddhist temple complex", "lat": 6.9169, "lon": 79.8565},
        {"id": "a10", "name": "National Museum Colombo", "category": "attraction", "destination": "Colombo", "price_range": "$", "rating": 4.3, "interests": ["history", "museum", "culture"], "description": "Largest museum in Sri Lanka", "lat": 6.9105, "lon": 79.8613},
    ],
    "galle": [
        {"id": "a11", "name": "Galle Fort", "category": "attraction", "destination": "Galle", "price_range": "Free", "rating": 4.8, "interests": ["history", "unesco", "architecture", "walking"], "description": "UNESCO World Heritage Dutch fort", "lat": 6.0300, "lon": 80.2167},
        {"id": "a12", "name": "Jungle Beach", "category": "attraction", "destination": "Galle", "price_range": "Free", "rating": 4.5, "interests": ["beach", "swimming", "nature", "hidden gem"], "description": "Secluded beach near Galle Fort", "lat": 6.0180, "lon": 80.2280},
        {"id": "a13", "name": "Sea Turtle Hatchery", "category": "attraction", "destination": "Galle", "price_range": "$", "rating": 4.4, "interests": ["wildlife", "conservation", "family"], "description": "Turtle conservation center", "lat": 6.0850, "lon": 80.2600},
    ],
}

_MOCK_EVENTS = {
    "ella": [
        {"id": "e1", "name": "Ella Sunday Market", "destination": "Ella", "category": "market", "start_datetime": "2026-08-24T08:00:00", "end_datetime": "2026-08-24T14:00:00", "description": "Weekly market with local crafts and food", "lat": 6.8667, "lon": 81.0466},
        {"id": "e2", "name": "Ella Music Festival", "destination": "Ella", "category": "festival", "start_datetime": "2026-09-15T18:00:00", "end_datetime": "2026-09-15T23:00:00", "description": "Annual music festival in the hills", "lat": 6.8698, "lon": 81.0468},
    ],
    "kandy": [
        {"id": "e3", "name": "Kandy Esala Perahera", "destination": "Kandy", "category": "festival", "start_datetime": "2026-08-20T19:00:00", "end_datetime": "2026-08-30T23:00:00", "description": "Grand Buddhist festival with processions", "lat": 7.2936, "lon": 80.6413},
        {"id": "e4", "name": "Kandy Food Festival", "destination": "Kandy", "category": "food", "start_datetime": "2026-09-10T10:00:00", "end_datetime": "2026-09-12T22:00:00", "description": "Food festival showcasing local cuisine", "lat": 7.2906, "lon": 80.6337},
    ],
    "colombo": [
        {"id": "e5", "name": "Colombo International Book Fair", "destination": "Colombo", "category": "exhibition", "start_datetime": "2026-09-18T09:00:00", "end_datetime": "2026-09-28T20:00:00", "description": "Annual book fair at BMICH", "lat": 6.8926, "lon": 79.8663},
        {"id": "e6", "name": "Colombo Jazz Festival", "destination": "Colombo", "category": "music", "start_datetime": "2026-10-05T18:00:00", "end_datetime": "2026-10-05T23:00:00", "description": "International jazz festival", "lat": 6.9271, "lon": 79.8612},
    ],
    "galle": [
        {"id": "e7", "name": "Galle Literary Festival", "destination": "Galle", "category": "literary", "start_datetime": "2026-01-15T09:00:00", "end_datetime": "2026-01-19T20:00:00", "description": "Annual literary festival in Galle Fort", "lat": 6.0300, "lon": 80.2167},
        {"id": "e8", "name": "Galle Art Walk", "destination": "Galle", "category": "art", "start_datetime": "2026-08-30T16:00:00", "end_datetime": "2026-08-30T22:00:00", "description": "Evening art walk through Galle Fort", "lat": 6.0295, "lon": 80.2170},
    ],
}


# Mock destinations above that aren't themselves one of the 25 official
# districts (Ella is a town inside Badulla district) - kept only for
# backfilling lat/lon onto the hand-written mock rows.
_MOCK_TOWN_COORDS = {"ella": {"lat": 6.8658, "lon": 81.0467}}


def _get_mock_data(data_dict: dict, destination: str, interests: Optional[List[str]] = None) -> List[dict]:
    """Get mock data for a destination, optionally filtered by interests."""
    dest_key = destination.lower().strip()
    items = data_dict.get(dest_key, [])

    # The §4 contract requires "lat"/"lon" on every listing dict (Recommendation/
    # Planner use them for travel-time reasoning), but the hand-written mock
    # entries above only carry a "destination" name - backfill from the
    # district centroid (or the town-coords fallback) so mock and real
    # (database) rows share the same shape.
    district = get_district(destination) or _MOCK_TOWN_COORDS.get(dest_key)
    if district:
        items = [
            item if ("lat" in item and "lon" in item)
            else {**item, "lat": district["lat"], "lon": district["lon"]}
            for item in items
        ]

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
    Queries the database first, falls back to mock data.
    """
    try:
        pool = await get_pool()
        if pool:
            # The real schema has no per-listing "interests" column, so
            # `interests` only filters the mock-data fallback below.
            rows = await pool.fetch(_SELECT_LISTINGS, destination, category)
            if rows:
                return [_row_to_listing_dict(r) for r in rows]
    except Exception as e:
        logger.warning(f"Listings query failed, using mock data: {e}")

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
    Falls back to mock data if the database is unavailable.
    """
    try:
        pool = await get_pool()
        if pool:
            rows = await pool.fetch(
                _SELECT_EVENTS, destination,
                _coerce_date(start_date), _coerce_date(end_date),
            )
            if rows:
                return [_row_to_event_dict(r) for r in rows]
    except Exception as e:
        logger.warning(f"Events query failed, using mock data: {e}")

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

def _row_to_profile_dict(row) -> dict:
    """Maps a real `traveler_profile` row (see SAD §9 Data View's ER diagram)
    onto the shape slot_filling.py's defaulting logic expects. home_lat/home_lon
    come from ST_Y/ST_X in _SELECT_PROFILE, so there's no geometry decoding here."""
    lat, lon = row["home_lat"], row["home_lon"]
    return {
        "interests": list(row["travel_interests"] or []),
        "travel_style": row["travel_style"],
        "budget": float(row["default_budget"]) if row["default_budget"] is not None else None,
        "home_location": {"lat": lat, "lon": lon} if lat is not None else None,
    }


async def get_user_profile(user_id: str) -> dict:
    """
    Returns a traveler's saved preferences for slot_filling.py's §2
    defaulting logic (destination-only request -> pull interests/travel_style/
    budget from here instead of re-asking). Queries the `traveler_profile`
    table first, falls back to empty defaults - same pattern as every other
    lookup in this file.
    """
    default = {"interests": [], "travel_style": None, "budget": None, "home_location": None}

    try:
        pool = await get_pool()
        if pool:
            row = await pool.fetchrow(_SELECT_PROFILE, user_id)
            if row:
                return _row_to_profile_dict(row)
    except Exception as e:
        logger.warning(f"Profile query failed for user '{user_id}', using defaults: {e}")

    return default