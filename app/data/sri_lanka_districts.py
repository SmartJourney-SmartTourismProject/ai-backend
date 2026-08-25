"""
Canonical list of Sri Lanka's 25 districts with their OSM area name and an
approximate centroid (district-capital coordinates, not survey-grade).

Used by every data-refresh job (hotels/restaurants via Overpass, events via
Ticketmaster/Eventbrite, and weather geocoding) so all pipelines cover the
same districts instead of each hardcoding its own partial list.
"""
from __future__ import annotations
from typing import TypedDict


class District(TypedDict):
    name: str          # e.g. "Kandy" - used as the human-facing/destination key
    osm_name: str       # e.g. "Kandy District" - matches OSM admin_level=5 area name
    province: str
    lat: float
    lon: float


DISTRICTS: list[District] = [
    {"name": "Colombo", "osm_name": "Colombo District", "province": "Western", "lat": 6.9271, "lon": 79.8612},
    {"name": "Gampaha", "osm_name": "Gampaha District", "province": "Western", "lat": 7.0917, "lon": 79.9990},
    {"name": "Kalutara", "osm_name": "Kalutara District", "province": "Western", "lat": 6.5854, "lon": 79.9607},
    {"name": "Kandy", "osm_name": "Kandy District", "province": "Central", "lat": 7.2906, "lon": 80.6337},
    {"name": "Matale", "osm_name": "Matale District", "province": "Central", "lat": 7.4675, "lon": 80.6234},
    {"name": "Nuwara Eliya", "osm_name": "Nuwara Eliya District", "province": "Central", "lat": 6.9497, "lon": 80.7891},
    {"name": "Galle", "osm_name": "Galle District", "province": "Southern", "lat": 6.0535, "lon": 80.2210},
    {"name": "Matara", "osm_name": "Matara District", "province": "Southern", "lat": 5.9549, "lon": 80.5550},
    {"name": "Hambantota", "osm_name": "Hambantota District", "province": "Southern", "lat": 6.1246, "lon": 81.1185},
    {"name": "Jaffna", "osm_name": "Jaffna District", "province": "Northern", "lat": 9.6615, "lon": 80.0255},
    {"name": "Kilinochchi", "osm_name": "Kilinochchi District", "province": "Northern", "lat": 9.3803, "lon": 80.3770},
    {"name": "Mannar", "osm_name": "Mannar District", "province": "Northern", "lat": 8.9810, "lon": 79.9044},
    {"name": "Vavuniya", "osm_name": "Vavuniya District", "province": "Northern", "lat": 8.7514, "lon": 80.4971},
    {"name": "Mullaitivu", "osm_name": "Mullaitivu District", "province": "Northern", "lat": 9.2670, "lon": 80.8142},
    {"name": "Trincomalee", "osm_name": "Trincomalee District", "province": "Eastern", "lat": 8.5874, "lon": 81.2152},
    {"name": "Batticaloa", "osm_name": "Batticaloa District", "province": "Eastern", "lat": 7.7170, "lon": 81.7000},
    {"name": "Ampara", "osm_name": "Ampara District", "province": "Eastern", "lat": 7.2975, "lon": 81.6747},
    {"name": "Kurunegala", "osm_name": "Kurunegala District", "province": "North Western", "lat": 7.4863, "lon": 80.3647},
    {"name": "Puttalam", "osm_name": "Puttalam District", "province": "North Western", "lat": 8.0362, "lon": 79.8283},
    {"name": "Anuradhapura", "osm_name": "Anuradhapura District", "province": "North Central", "lat": 8.3114, "lon": 80.4037},
    {"name": "Polonnaruwa", "osm_name": "Polonnaruwa District", "province": "North Central", "lat": 7.9403, "lon": 81.0188},
    {"name": "Badulla", "osm_name": "Badulla District", "province": "Uva", "lat": 6.9934, "lon": 81.0550},
    {"name": "Monaragala", "osm_name": "Monaragala District", "province": "Uva", "lat": 6.8728, "lon": 81.3507},
    {"name": "Ratnapura", "osm_name": "Ratnapura District", "province": "Sabaragamuwa", "lat": 6.6828, "lon": 80.3992},
    {"name": "Kegalle", "osm_name": "Kegalle District", "province": "Sabaragamuwa", "lat": 7.2513, "lon": 80.3464},
]

assert len(DISTRICTS) == 25

DISTRICT_NAMES: list[str] = [d["name"] for d in DISTRICTS]


def get_district(name: str) -> District | None:
    """Case-insensitive lookup by district name (e.g. 'kandy' -> Kandy record)."""
    key = name.strip().lower()
    for d in DISTRICTS:
        if d["name"].lower() == key:
            return d
    return None
