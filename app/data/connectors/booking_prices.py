"""
Weekly real hotel-price enrichment via Booking.com (RapidAPI) -
docs/master_plan/DATA_PLATFORM.md §5.2. Unlike osm_listings, this connector
does not create rows - it enriches travel_listing hotel rows osm_listings
already created, matching by proximity (nearest Booking.com result within
1km of each OSM-sourced hotel).

One destination lookup + one hotel search per district (not one call per
hotel) - keeps this well inside RapidAPI's free tier.

    python -m app.data.connectors.booking_prices --district "Kandy"
"""
from __future__ import annotations

import argparse
import logging
import math
import time
from datetime import date, timedelta
from typing import Any, Optional

import requests

from app.config.settings import settings
from app.data.connectors.base import District, fetch_all_districts
from app.data.postgres_writer import get_connection

logger = logging.getLogger(__name__)

BOOKING_DESTINATION_URL_TMPL = "https://{host}/api/v1/hotels/searchDestination"
BOOKING_SEARCH_URL_TMPL = "https://{host}/api/v1/hotels/searchHotels"
MATCH_RADIUS_KM = 1.0

NAME = "booking_prices"
CADENCE = "weekly"
REQUIRES_KEY = True
SCOPE = "per_district"


def _headers() -> dict[str, str]:
    return {
        "X-RapidAPI-Key": settings.booking_rapidapi_key,
        "X-RapidAPI-Host": settings.booking_rapidapi_host,
    }


def _haversine_km(lat1, lon1, lat2, lon2) -> float:
    R = 6371.0
    dlat, dlon = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _price_level_bucket(price_per_night_lkr: Optional[float]) -> Optional[int]:
    """Buckets a real LKR nightly price into the 1-4 price_level scale
    app/core/scoring.py's cost() function reads (DETERMINISM_AND_VALIDATION.md §6.2)."""
    if price_per_night_lkr is None:
        return None
    if price_per_night_lkr < 9_000:
        return 1
    if price_per_night_lkr < 22_000:
        return 2
    if price_per_night_lkr < 45_000:
        return 3
    return 4


def _find_destination(district_name: str, session: requests.Session) -> Optional[dict]:
    url = BOOKING_DESTINATION_URL_TMPL.format(host=settings.booking_rapidapi_host)
    try:
        resp = session.get(url, headers=_headers(), params={"query": district_name}, timeout=30)
        resp.raise_for_status()
        candidates = resp.json().get("data", [])
    except requests.exceptions.RequestException as e:
        logger.warning(f"Booking destination lookup failed for '{district_name}': {e}")
        return None
    sri_lanka = [c for c in candidates if c.get("country") == "Sri Lanka"]
    if not sri_lanka:
        return None
    best = max(sri_lanka, key=lambda c: c.get("hotels", 0))
    return {"dest_id": best["dest_id"], "search_type": best.get("search_type", "city")}


class BookingPricesConnector:
    name = NAME
    cadence = CADENCE
    requires_key = REQUIRES_KEY
    scope = SCOPE

    async def fetch(self, district: Optional[District]) -> list[dict[str, Any]]:
        if district is None:
            raise ValueError("booking_prices is a per_district connector")
        if not settings.booking_rapidapi_key:
            return []

        session = requests.Session()
        # District.name is "Kandy District" (OSM's own naming); Booking's
        # destination search wants the bare place name.
        short_name = district.name.replace(" District", "")
        destination = _find_destination(short_name, session)
        if destination is None:
            logger.info(f"No Booking.com Sri Lanka destination match for '{short_name}'")
            return []

        checkin = date.today() + timedelta(days=30)
        checkout = checkin + timedelta(days=1)
        url = BOOKING_SEARCH_URL_TMPL.format(host=settings.booking_rapidapi_host)
        params = {
            "dest_id": destination["dest_id"], "search_type": destination["search_type"],
            "arrival_date": checkin.isoformat(), "departure_date": checkout.isoformat(),
            "adults": "2", "room_qty": "1", "page_number": "1", "currency_code": "USD",
        }
        try:
            resp = session.get(url, headers=_headers(), params=params, timeout=30)
            resp.raise_for_status()
            hotels = resp.json().get("data", {}).get("hotels", [])
        except requests.exceptions.RequestException as e:
            logger.warning(f"Booking hotel search failed for '{short_name}': {e}")
            return []

        results = []
        for hotel in hotels:
            prop = hotel.get("property", {})
            price = prop.get("priceBreakdown", {}).get("grossPrice", {})
            if price.get("value") is None or prop.get("latitude") is None:
                continue
            results.append({
                "name": prop.get("name", ""), "lat": prop["latitude"], "lon": prop["longitude"],
                "price_usd": price["value"], "currency": price.get("currency", "USD"),
                # Free, already in this response - no extra call. This turns
                # out to be a materially better free rating source for
                # hotels than Foursquare, whose rating/price/stats fields
                # are Premium-only even on the free "Pro" tier (verified
                # live 2026-09-02 - see docs/master_plan/API_SETUP.md §4.1).
                "review_score_10": prop.get("reviewScore"),   # 0-10 scale
                "review_count": prop.get("reviewCount"),
                "photo_url": (prop.get("photoUrls") or [None])[0],
            })
        return results

    def normalize(self, raw: list[dict[str, Any]], district: Optional[District]) -> list[dict[str, Any]]:
        """Matches each Booking result to the nearest existing OSM-sourced
        hotel row within MATCH_RADIUS_KM. Returns update rows keyed by
        travel_listing.id, not new rows - this connector enriches, never inserts."""
        if not raw or district is None:
            return []

        conn = get_connection()
        if conn is None:
            return []
        try:
            with conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT l.id, l.latitude, l.longitude FROM travel_listing l "
                    "JOIN category c ON c.id = l.category_id "
                    "WHERE l.district_id = %s AND c.name = 'hotel'",
                    (district.id,),
                )
                existing = cur.fetchall()
        except Exception as e:
            logger.error(f"booking_prices.normalize: lookup failed: {e}")
            return []
        finally:
            conn.close()

        updates = []
        for listing_id, lat, lon in existing:
            best_dist, best_match = MATCH_RADIUS_KM, None
            for bh in raw:
                d = _haversine_km(lat, lon, bh["lat"], bh["lon"])
                if d < best_dist:
                    best_dist, best_match = d, bh
            if best_match:
                price_lkr = best_match["price_usd"] * settings.usd_lkr_rate
                # Booking's 0-10 reviewScore -> this schema's 1-5 scale
                # (rating CHECK constraint is 1.0-5.0). 0 maps to 1.0 (the
                # floor), not 0, since travel_listing.rating has no zero.
                score_10 = best_match.get("review_score_10")
                rating_5 = round(1.0 + (score_10 / 10.0) * 4.0, 1) if score_10 is not None else None
                updates.append({
                    "listing_id": str(listing_id),
                    "price_per_night": round(price_lkr, 2),
                    "currency": "LKR",
                    "price_level": _price_level_bucket(price_lkr),
                    "rating": rating_5,
                    "rating_count": best_match.get("review_count") or 0,
                    "photo_url": best_match.get("photo_url"),
                })
        return updates

    def upsert(self, rows: list[dict[str, Any]]) -> int:
        """UPDATE, not upsert_rows - these rows already exist (created by
        osm_listings); this connector only ever enriches price/rating
        fields on them. rating/rating_count only overwrite when Booking
        actually returned a score - COALESCE keeps whatever a later
        enrichment pass (e.g. an admin correction) already set."""
        if not rows:
            return 0
        conn = get_connection()
        if conn is None:
            return 0
        try:
            with conn, conn.cursor() as cur:
                for r in rows:
                    cur.execute(
                        "UPDATE travel_listing SET price_per_night=%s, currency=%s, "
                        "price_level=%s, rating=COALESCE(%s, rating), "
                        "rating_count=CASE WHEN %s IS NOT NULL THEN %s ELSE rating_count END, "
                        "updated_at=now() WHERE id=%s",
                        (r["price_per_night"], r["currency"], r["price_level"],
                         r["rating"], r["rating"], r["rating_count"], r["listing_id"]),
                    )
                    if r.get("photo_url"):
                        cur.execute(
                            "INSERT INTO listing_image (listing_id, url, attribution) "
                            "VALUES (%s, %s, %s) "
                            "ON CONFLICT ON CONSTRAINT listing_image_listing_url_uq DO NOTHING",
                            (r["listing_id"], r["photo_url"], "Booking.com"),
                        )
            return len(rows)
        except Exception as e:
            logger.error(f"booking_prices upsert failed: {e}")
            return 0
        finally:
            conn.close()


async def run_all(district_filter: str = None) -> None:
    connector = BookingPricesConnector()
    districts = fetch_all_districts()
    if district_filter:
        districts = [d for d in districts if district_filter.lower() in d.name.lower()]
    if not districts:
        print("[FATAL] No districts found.")
        return
    if not settings.booking_rapidapi_key:
        print("[WARN] BOOKING_RAPIDAPI_KEY not set - nothing to enrich.")
        return

    total = 0
    for i, d in enumerate(districts, 1):
        print(f"[{i}/{len(districts)}] {d.name} ...", end=" ", flush=True)
        raw = await connector.fetch(d)
        rows = connector.normalize(raw, d)
        count = connector.upsert(rows)
        total += count
        print(f"{count} hotels priced (of {len(raw)} Booking results)")
        time.sleep(2)

    print(f"\n[SUCCESS] Enriched {total} hotel(s) with real prices.")


if __name__ == "__main__":
    import asyncio

    logging.basicConfig(level=logging.INFO)
    ap = argparse.ArgumentParser()
    ap.add_argument("--district", default=None)
    args = ap.parse_args()
    asyncio.run(run_all(args.district))
