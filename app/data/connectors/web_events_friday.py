"""
Weekly events scrape from friday.lk -> local_event, landing as PENDING in
the admin moderation queue. Vetting record (robots.txt, terms, structure):
docs/master_plan/SCRAPE_SOURCES.md.

Reads only schema.org JSON-LD, never the page's CSS classes (generated MUI
names that change every build):
  /events            -> links to each city page (/events/<city-slug>)
  /events/<city>     -> ItemList of upcoming event URLs (/events/<uuid>)
  /events/<uuid>     -> one Event: startDate/endDate, location.address, offers

    python -m app.data.connectors.web_events_friday
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from bs4 import BeautifulSoup

from app.data.connectors.base import District
from app.data.postgres_writer import to_point_wkt, upsert_rows
from app.data.scraping import parse_lkr, polite_get
from app.tools.geo_tool import resolve_place

logger = logging.getLogger(__name__)

NAME = "web_events_friday"
CADENCE = "weekly"
REQUIRES_KEY = False
SCOPE = "global"
SOURCE = "friday_lk"

BASE = "https://www.friday.lk"
# Hard ceiling per run - a site change that suddenly lists thousands of
# pages must not turn a weekly job into an all-day crawl.
MAX_EVENT_PAGES = 200
LOOKAHEAD_DAYS = 180
# friday.lk has been seen emitting "60008000.00" for a 6,000-8,000 range.
# No single ticket in Sri Lanka costs more than this; above it the number is
# garbage and is stored as unknown rather than put into anyone's budget.
MAX_PLAUSIBLE_TICKET_LKR = 100_000
DESCRIPTION_MAX = 300

_CITY_LINK = re.compile(r"^(?:https://www\.friday\.lk)?/events/([a-z][a-z0-9-]*)/?$")
_EVENT_LINK = re.compile(r"^(?:https://www\.friday\.lk)?/events/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/?$")

# Event text -> the tag vocabulary (tag_vocabulary table; music / festival /
# nightlife added by migration 0012). First match per tag, order irrelevant.
_TAG_KEYWORDS = {
    "music": ("music", "concert", "live band", "dj ", "gig", "orchestra", "jazz", "acoustic"),
    "nightlife": ("party", "club", "rave", "nightlife", "techno", "electronic"),
    "festival": ("festival", "fest ", "carnival", "perahera", "fair"),
    "culture": ("culture", "cultural", "dance", "theatre", "theater", "drama", "art ", "exhibition", "heritage"),
    "food": ("food", "culinary", "dining", "market", "brunch"),
    "family": ("family", "kids", "children"),
    "beach": ("beach",),
    "nature": ("nature", "wildlife", "garden"),
    "hike": ("hike", "hiking", "trek"),
}


def _json_ld(html: str) -> list[dict]:
    out = []
    for script in BeautifulSoup(html, "html.parser").find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except json.JSONDecodeError:
            continue
        out.extend(data if isinstance(data, list) else [data])
    return out


# Same URL shape as a city page but not one ("/events/submit" is also
# robots-disallowed, so polite_get would refuse it anyway).
_NOT_CITIES = {"submit"}


def parse_city_slugs(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    slugs = []
    for a in soup.find_all("a", href=True):
        m = _CITY_LINK.match(a["href"])
        if m and m.group(1) not in slugs and m.group(1) not in _NOT_CITIES:
            slugs.append(m.group(1))
    return slugs


def parse_event_urls(html: str) -> list[str]:
    """Event URLs from a city page's ItemList JSON-LD."""
    urls = []
    for block in _json_ld(html):
        if block.get("@type") != "ItemList":
            continue
        for item in block.get("itemListElement") or []:
            url = item.get("url") or ""
            if _EVENT_LINK.match(url) and url not in urls:
                urls.append(url)
    return urls


def parse_event(html: str, url: str) -> Optional[dict[str, Any]]:
    """The page's schema.org Event, flattened. None if the page has none."""
    for block in _json_ld(html):
        if block.get("@type") != "Event":
            continue
        location = block.get("location") or {}
        address = location.get("address") or {}
        offers = block.get("offers") or {}
        if isinstance(offers, list):
            offers = offers[0] if offers else {}
        return {
            "uuid": _EVENT_LINK.match(url).group(1),
            "url": url,
            "name": (block.get("name") or "").strip(),
            "description": (block.get("description") or "").strip(),
            "start": block.get("startDate"),
            "end": block.get("endDate"),
            "status": block.get("eventStatus") or "",
            "venue": (location.get("name") or "").strip(),
            "locality": (address.get("addressLocality") or "").strip(),
            "country": (address.get("addressCountry") or "").strip(),
            "price": offers.get("price"),
            "currency": (offers.get("priceCurrency") or "LKR").upper(),
            "keywords": block.get("keywords") or "",
        }
    return None


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _price_lkr(raw: dict[str, Any]) -> Optional[float]:
    amount = raw.get("price")
    # offers.price is a bare number; the currency lives in priceCurrency.
    price = parse_lkr(f"USD {amount}" if raw.get("currency") == "USD" else amount)
    if price is None:
        return None
    if price > MAX_PLAUSIBLE_TICKET_LKR:
        logger.info(f"friday.lk: implausible price {raw.get('price')!r} for {raw.get('name')!r} - stored as unknown")
        return None
    return price


def tags_for(raw: dict[str, Any]) -> list[str]:
    text = f" {raw.get('name', '')} {raw.get('description', '')} {raw.get('keywords', '')} ".lower()
    return [tag for tag, words in _TAG_KEYWORDS.items() if any(w in text for w in words)]


def _crawl() -> list[dict[str, Any]]:
    """Synchronous (polite_get sleeps) - always called via asyncio.to_thread."""
    index = polite_get(f"{BASE}/events")
    if index is None:
        return []
    event_urls: list[str] = []
    for slug in parse_city_slugs(index):
        city_html = polite_get(f"{BASE}/events/{slug}")
        if city_html:
            event_urls.extend(u for u in parse_event_urls(city_html) if u not in event_urls)
    if len(event_urls) > MAX_EVENT_PAGES:
        logger.warning(f"friday.lk: {len(event_urls)} events listed, capping at {MAX_EVENT_PAGES}")
        event_urls = event_urls[:MAX_EVENT_PAGES]

    raw = []
    for url in event_urls:
        html = polite_get(url)
        event = parse_event(html, url) if html else None
        if event:
            raw.append(event)
    return raw


class FridayEventsConnector:
    name = NAME
    cadence = CADENCE
    requires_key = REQUIRES_KEY
    scope = SCOPE

    async def fetch(self, district: Optional[District]) -> list[dict[str, Any]]:
        raw = await asyncio.to_thread(_crawl)
        # Locality -> district here, not in normalize: resolve_place is async
        # and cached in geo_resolution, so each town costs one lookup ever.
        places: dict[str, Any] = {}
        for event in raw:
            locality = event["locality"]
            if locality and locality not in places:
                try:
                    places[locality] = await resolve_place(locality)
                except Exception as e:
                    logger.warning(f"friday.lk: resolve_place({locality!r}) failed: {e}")
                    places[locality] = None
            event["place"] = places.get(locality)
        return raw

    def normalize(self, raw: list[dict[str, Any]], district: Optional[District]) -> list[dict[str, Any]]:
        now = datetime.now(timezone.utc)
        horizon = now + timedelta(days=LOOKAHEAD_DAYS)
        rows = []
        for r in raw:
            start, end = _parse_iso(r.get("start")), _parse_iso(r.get("end"))
            place = r.get("place") or {}
            if not r.get("name") or start is None:
                continue
            if "Cancelled" in r.get("status", "") or "Postponed" in r.get("status", ""):
                continue
            if (end or start) < now or start > horizon:
                continue
            if r.get("country") not in ("LK", "Sri Lanka") or not place.get("district_id"):
                continue    # out of scope, or a town we can't place in a district
            price = _price_lkr(r)
            description = r.get("description") or None
            if description and len(description) > DESCRIPTION_MAX:
                description = description[:DESCRIPTION_MAX].rsplit(" ", 1)[0] + "…"
            rows.append({
                "district_id": place["district_id"],
                "name": r["name"],
                "description": description,
                "start_datetime": start,
                "end_datetime": end,
                "venue_name": r.get("venue") or None,
                # Town-level point: the JSON-LD carries a street address but
                # no coordinates, and geocoding every venue would multiply
                # Nominatim load for little gain at itinerary scale.
                "lat": place["lat"],
                "lon": place["lon"],
                "tags": tags_for(r),
                "price": price,
                "source_url": r["url"],
                "external_ref": r["uuid"],
            })
        return rows

    def upsert(self, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0
        db_rows = [{
            "district_id": r["district_id"],
            "name": r["name"],
            "description": r["description"],
            "start_datetime": r["start_datetime"],
            "end_datetime": r["end_datetime"],
            "venue_name": r["venue_name"],
            "location": to_point_wkt(r["lat"], r["lon"]),
            "tags": r["tags"],
            "price_min": r["price"],
            "price_max": r["price"],
            "source": SOURCE,
            "source_url": r["source_url"],
            "external_ref": r["external_ref"],
            # Pending: an admin approves before any traveller sees it.
            "is_verified": False,
        } for r in rows]
        return upsert_rows(
            "local_event", db_rows, on_conflict=("source", "external_ref"), geo_columns={"location"},
            insert_only={"is_verified"},
        )


async def run() -> None:
    connector = FridayEventsConnector()
    raw = await connector.fetch(None)
    rows = connector.normalize(raw, None)
    count = connector.upsert(rows)
    print(f"[SUCCESS] {count} event(s) upserted as pending ({len(raw)} fetched, {len(raw) - len(rows)} dropped).")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())
