"""
Turns the traveller's destination text into one or more real places.

geo_tool.resolve_place() answers "where is <name>?" for ONE place. Travellers
routinely name several ("Galle and Matara", "Kandy, Ella") or a region
("down south"), and the whole string went to resolve_place as if it were one
name: "galle and matara" matched nothing, so the request failed with
"could not resolve destination" (live-found 2026-10-01), and "down south" was
found by Nominatim in Washington State, so the trip was refused as being
outside Sri Lanka.

Regions are mapped to their districts here, by name, so they never reach
Nominatim. Lists are split and each part resolved on its own.
"""
from __future__ import annotations

import logging
import re
from typing import Awaitable, Callable, Optional

from app.tools import geo_tool
from app.tools.geo_tool import PlaceResolution

logger = logging.getLogger(__name__)

PlaceResolver = Callable[[str], Awaitable[Optional[PlaceResolution]]]
DistrictResolver = Callable[[float, float], Awaitable[Optional[dict]]]

# Informal region names -> the districts a traveller means by them. Resolved
# through the district table (resolve_place's trigram step matches a district
# name exactly), never Nominatim.
REGION_ALIASES: dict[str, list[str]] = {
    "down south": ["Galle", "Matara", "Hambantota"],
    "south coast": ["Galle", "Matara", "Hambantota"],
    "southern coast": ["Galle", "Matara", "Hambantota"],
    "southern province": ["Galle", "Matara", "Hambantota"],
    "the south": ["Galle", "Matara", "Hambantota"],
    "south": ["Galle", "Matara", "Hambantota"],
    "hill country": ["Kandy", "Nuwara Eliya", "Badulla"],
    "the hill country": ["Kandy", "Nuwara Eliya", "Badulla"],
    "hills": ["Kandy", "Nuwara Eliya", "Badulla"],
    "cultural triangle": ["Anuradhapura", "Polonnaruwa", "Matale"],
    "the cultural triangle": ["Anuradhapura", "Polonnaruwa", "Matale"],
    "east coast": ["Trincomalee", "Batticaloa", "Ampara"],
    "the east coast": ["Trincomalee", "Batticaloa", "Ampara"],
    "west coast": ["Colombo", "Gampaha", "Kalutara"],
    "the west coast": ["Colombo", "Gampaha", "Kalutara"],
}

# "Galle and Matara", "Kandy, Ella", "Galle & Matara", "Colombo to Galle",
# "Kandy then Ella", "Galle/Matara".
_SEPARATOR = re.compile(r"\s*(?:,|&|\+|/|\band\b|\bthen\b|\bto\b|\bplus\b)\s*", re.IGNORECASE)

# Words around a place name that aren't part of it ("around Galle",
# "the Matara area", "Galle district").
_FILLER = re.compile(
    r"^(?:around|near|in|at|the|visit|visiting)\s+|\s+(?:area|region|district|town|city)$",
    re.IGNORECASE,
)


def _normalise(text: str) -> str:
    text = re.sub(r"[()]", " ", text)
    text = re.sub(r"\s+", " ", text).strip().lower()
    previous = None
    while previous != text:
        previous = text
        text = _FILLER.sub("", text).strip()
    return text


class _Resolvers:
    def __init__(self, place: Optional[PlaceResolver], district: Optional[DistrictResolver]):
        # Looked up at call time, not import time, so a caller's (or a
        # test's) replacement of geo_tool's functions is honoured.
        self.place = place or geo_tool.resolve_place
        self.district = district or geo_tool.resolve_district

    async def one(self, name: str) -> Optional[PlaceResolution]:
        """resolve_place, plus a district lookup for a Nominatim hit that
        came back without one (the step context_resolver has always done)."""
        place = await self.place(name)
        if place is None or place.get("confidence") == "out_of_country":
            return place
        if place.get("district_id") is None:
            district = await self.district(place["lat"], place["lon"])
            if district is None:
                return None
            place = {**place, "district_id": district["district_id"]}
        return place

    async def alias(self, key: str) -> list[PlaceResolution]:
        places = []
        for district_name in REGION_ALIASES[key]:
            place = await self.one(district_name)
            if place is not None and place.get("confidence") != "out_of_country":
                places.append(place)
        return places


def _dedupe(places: list[PlaceResolution]) -> list[PlaceResolution]:
    seen: set[str] = set()
    out = []
    for place in places:
        key = place.get("district_id") or place["name"]
        if key in seen:
            continue
        seen.add(key)
        out.append(place)
    return out


async def resolve_destinations(
    text: Optional[str],
    *,
    resolve_place: Optional[PlaceResolver] = None,
    resolve_district: Optional[DistrictResolver] = None,
) -> list[PlaceResolution]:
    """Every place the destination text names, in the order named.

    Returns in-country places when there are any. When everything named is
    abroad, returns those out_of_country results (so the caller can say "we
    only cover Sri Lanka"); when nothing resolves at all, returns [].
    """
    if not text or not text.strip():
        return []
    resolvers = _Resolvers(resolve_place, resolve_district)
    key = _normalise(text)

    if key in REGION_ALIASES:
        return await resolvers.alias(key)

    parts = [p for p in (_normalise(p) for p in _SEPARATOR.split(key)) if p]
    if len(parts) > 1:
        found: list[PlaceResolution] = []
        abroad: list[PlaceResolution] = []
        for part in parts:
            if part in REGION_ALIASES:
                found.extend(await resolvers.alias(part))
                continue
            try:
                place = await resolvers.one(part)
            except Exception as e:
                logger.warning(f"resolve_destinations: '{part}' failed: {e}")
                continue
            if place is None:
                continue
            (abroad if place.get("confidence") == "out_of_country" else found).append(place)
        if found:
            return _dedupe(found)
        if abroad:
            return abroad[:1]
        # No part resolved - try the text as one name before giving up (a
        # real place whose name happens to contain "and"/"to").

    place = await resolvers.one(key)
    return [place] if place is not None else []


def display_name(places: list[dict]) -> str:
    """'Galle & Matara' - district suffixes dropped, they add nothing when
    every name is a district."""
    names = [re.sub(r"\s+District$", "", p["name"], flags=re.IGNORECASE) for p in places]
    return " & ".join(names)
