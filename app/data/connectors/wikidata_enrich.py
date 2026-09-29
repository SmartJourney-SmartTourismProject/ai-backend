"""
Monthly free enrichment: descriptions, a real photo, and a popularity prior
for listings that osm_listings created but couldn't describe (OSM carries
almost no prose or images) - docs/master_plan/DATA_PLATFORM.md §5.2/§4.1.

Uses Wikipedia's own API rather than raw Wikidata SPARQL: `list=geosearch`
finds the nearest Wikipedia article to a listing's coordinates (more robust
than matching by name, which fails on transliteration/punctuation
differences), then one combined query returns its intro extract,
`langlinkscount` (how many language editions cover it - a free, unmetered
stand-in for "is this place notable", per API_SETUP.md §4.1's suggested
substitute for Foursquare's now-metered ratings), and a real photo URL.

Free, unmetered, no key. Politeness: this still makes one geosearch + one
detail call per listing, so it's capped to `--limit` per run (default 200)
rather than sweeping every listing at once.

    python -m app.data.connectors.wikidata_enrich --district "Kandy" --limit 50
"""
from __future__ import annotations

import argparse
import logging
import time
from typing import Any, Optional

import requests

from app.data.connectors.base import District, fetch_all_districts
from app.data.postgres_writer import get_connection

logger = logging.getLogger(__name__)

WIKIPEDIA_API = "https://en.wikipedia.org/w/api.php"
# Wikimedia's API etiquette (https://meta.wikimedia.org/wiki/User-Agent_policy)
# requires a descriptive UA with contact info and 429s a generic one - hit
# live 2026-09-02 (first live call 429'd; a UA with a contact address did
# not). Not optional despite there being no API key to configure.
UA = {"User-Agent": "SmartTourismAI/1.0 (university project; contact: smarttourism.project@example.com)"}
GEOSEARCH_RADIUS_M = 150   # tight radius - only claim a match that's genuinely this place
MIN_LANGLINKS_FOR_PHOTO = 0
_MAX_RETRIES = 2

NAME = "wikidata_enrich"
CADENCE = "monthly"
REQUIRES_KEY = False
SCOPE = "per_district"


def _get_with_retry(params: dict) -> Optional[requests.Response]:
    """Wikipedia's shared API occasionally 429s under bulk use even with a
    compliant UA - one short backoff-and-retry absorbs a transient throttle
    without a whole listing silently losing its enrichment."""
    for attempt in range(_MAX_RETRIES + 1):
        try:
            resp = requests.get(WIKIPEDIA_API, params=params, headers=UA, timeout=15)
            if resp.status_code == 429:
                if attempt < _MAX_RETRIES:
                    time.sleep(2 * (attempt + 1))
                    continue
                return None
            resp.raise_for_status()
            return resp
        except requests.exceptions.RequestException as e:
            logger.warning(f"Wikipedia API call failed: {e}")
            return None
    return None


def _find_wikipedia_match(lat: float, lon: float) -> Optional[dict]:
    resp = _get_with_retry({
        "action": "query", "list": "geosearch",
        "gscoord": f"{lat}|{lon}", "gsradius": GEOSEARCH_RADIUS_M, "gslimit": 1,
        "format": "json",
    })
    if resp is None:
        return None
    hits = resp.json().get("query", {}).get("geosearch", [])
    return hits[0] if hits else None


PHOTO_THUMBNAIL_WIDTH_PX = 800  # display size is a 160px card - a full
# multi-megapixel "original" (some run 6000px+ wide) is pure waste, and
# was also the direct cause of a frontend bug: Next.js's image optimizer
# fetches server-side with no browser User-Agent, which Wikimedia's bot-UA
# policy 429s - a thumbnail URL sidesteps needing the optimizer at all.


def _fetch_details(pageid: int) -> Optional[dict]:
    resp = _get_with_retry({
        "action": "query", "pageids": pageid,
        "prop": "extracts|langlinkscount|pageimages",
        "exintro": 1, "explaintext": 1, "exsentences": 3,
        "piprop": "thumbnail", "pithumbsize": PHOTO_THUMBNAIL_WIDTH_PX,
        "format": "json",
    })
    if resp is None:
        return None
    page = resp.json().get("query", {}).get("pages", {}).get(str(pageid))
    if not page:
        return None
    return {
        "extract": page.get("extract"),
        "langlinkscount": page.get("langlinkscount", 0),
        "photo_url": (page.get("thumbnail") or {}).get("source"),
        "title": page.get("title"),
    }


class WikidataEnrichConnector:
    name = NAME
    cadence = CADENCE
    requires_key = REQUIRES_KEY
    scope = SCOPE

    def __init__(self, limit: int = 200, category: Optional[str] = None) -> None:
        self.limit = limit
        # Wikipedia's geosearch only matches places notable enough to have an
        # article, which in practice means attractions - a 150m search around
        # a hotel or restaurant almost never returns that business itself.
        # Restricting by category keeps a sweep from spending ~2 API calls
        # each on the ~5,800 listings that can't match (verified live
        # 2026-09-04: 0 of the existing photo_url values came from here).
        self.category = category

    async def fetch(self, district: Optional[District]) -> list[dict[str, Any]]:
        """Fetches candidate listing rows (no description yet) - the actual
        Wikipedia lookups happen in normalize(), since they're per-row and
        this keeps fetch()'s contract (raw external data) loose enough for
        a two-step external call without a second connector method."""
        if district is None:
            raise ValueError("wikidata_enrich is a per_district connector")

        conn = get_connection()
        if conn is None:
            return []
        try:
            with conn, conn.cursor() as cur:
                sql = (
                    "SELECT tl.id, tl.latitude, tl.longitude FROM travel_listing tl "
                    "WHERE tl.district_id = %s AND tl.description IS NULL"
                )
                params: list[Any] = [district.id]
                if self.category:
                    sql += (
                        " AND tl.category_id = "
                        "(SELECT id FROM category WHERE name = %s)"
                    )
                    params.append(self.category)
                sql += " ORDER BY tl.rating_count DESC NULLS LAST LIMIT %s"
                params.append(self.limit)

                cur.execute(sql, params)
                return [{"id": str(r[0]), "lat": r[1], "lon": r[2]} for r in cur.fetchall()]
        except Exception as e:
            logger.error(f"wikidata_enrich.fetch failed: {e}")
            return []
        finally:
            conn.close()

    def normalize(self, raw: list[dict[str, Any]], district: Optional[District]) -> list[dict[str, Any]]:
        rows = []
        for item in raw:
            match = _find_wikipedia_match(item["lat"], item["lon"])
            time.sleep(0.3)  # polite pacing - Wikipedia has no hard rate limit but this is still bulk use
            if match is None:
                continue
            details = _fetch_details(match["pageid"])
            time.sleep(0.3)
            if details is None or not details.get("extract"):
                continue
            rows.append({
                "listing_id": item["id"],
                "description": details["extract"],
                "photo_url": details["photo_url"],
                "popularity_prior": details["langlinkscount"],
            })
        return rows

    def upsert(self, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0
        conn = get_connection()
        if conn is None:
            return 0
        try:
            with conn, conn.cursor() as cur:
                for r in rows:
                    cur.execute(
                        "UPDATE travel_listing SET description = %s, "
                        "photo_url = COALESCE(photo_url, %s), updated_at = now() "
                        "WHERE id = %s AND description IS NULL",
                        (r["description"], r["photo_url"], r["listing_id"]),
                    )
                    if r["photo_url"]:
                        cur.execute(
                            "INSERT INTO listing_image (listing_id, url, attribution) "
                            "VALUES (%s, %s, %s) "
                            "ON CONFLICT ON CONSTRAINT listing_image_listing_url_uq DO NOTHING",
                            (r["listing_id"], r["photo_url"], "Wikimedia Commons"),
                        )
            return len(rows)
        except Exception as e:
            logger.error(f"wikidata_enrich upsert failed: {e}")
            return 0
        finally:
            conn.close()


async def run_all(district_filter: str = None, limit: int = 200,
                  category: str = None) -> None:
    connector = WikidataEnrichConnector(limit=limit, category=category)
    districts = fetch_all_districts()
    if district_filter:
        districts = [d for d in districts if district_filter.lower() in d.name.lower()]
    if not districts:
        print("[FATAL] No districts found.")
        return

    total = 0
    for i, d in enumerate(districts, 1):
        print(f"[{i}/{len(districts)}] {d.name} ...", end=" ", flush=True)
        raw = await connector.fetch(d)
        rows = connector.normalize(raw, d)
        count = connector.upsert(rows)
        total += count
        print(f"{count} enriched (of {len(raw)} candidates)")

    print(f"\n[SUCCESS] Enriched {total} listing(s) with description/photo/popularity.")


if __name__ == "__main__":
    import asyncio

    logging.basicConfig(level=logging.INFO)
    ap = argparse.ArgumentParser()
    ap.add_argument("--district", default=None)
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--category", default=None,
                    help="Restrict to one category (e.g. 'attraction') - see "
                         "WikidataEnrichConnector.__init__ for why this matters.")
    args = ap.parse_args()
    asyncio.run(run_all(args.district, args.limit, args.category))
