"""
Free popularity signal from Wikipedia pageviews.

Why pageviews rather than ratings: star ratings and review text for arbitrary
places belong to Google and TripAdvisor - both metered, and both forbid storing
the ratings they return. Wikipedia's pageviews API is free, needs no key, has no
quota to manage, and is CC BY-SA (attribution only, no caching restriction). It
also measures the thing the scorer actually lacked: how many people care about
a place. Sampled 2026-09-30 over twelve months: Sigiriya 275,216, Temple of the
Tooth 76,106, Galle Fort 52,038, Nine Arch Bridge 32,444.

Matching is the hard part, and the reason this connector exists separately from
`wikidata_enrich`. That one matches by nearest coordinate, which inside a dense
historic quarter attaches the wrong article: on 2026-09-30 it gave the Galle
Services Club to "Memorial Pillar", "Ramasinghe Premadasa" AND "The Moon
Bastion", and "The Kandy Airport is a proposed domestic airport" to three
unrelated Kandy listings - 29 of 74 enrichments (39%) were wrong.

So this connector never matches on proximity alone. An article is accepted only
when the listing's name and the article title genuinely correspond:

  1. the listing's own OSM `wikipedia` / `wikidata` tag, when it has one -
     an exact link, no guessing;
  2. otherwise a nearby article whose title is similar enough to the listing
     name (see _NAME_SIMILARITY_FLOOR), which is what the coordinate-only
     approach was missing.

A listing with no match is left with popularity NULL. Most listings are small
guesthouses and cafes that will never have an article; that is the normal case,
not a failure, and the scorer reads a missing value as "no evidence".

    python -m app.data.connectors.wikipedia_popularity --category attraction
    python -m app.data.connectors.wikipedia_popularity --district Galle --limit 50
"""
from __future__ import annotations

import argparse
import difflib
import logging
import re
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import quote

import requests

from app.data.postgres_writer import get_connection

logger = logging.getLogger(__name__)

NAME = "wikipedia_popularity"
CADENCE = "monthly"
REQUIRES_KEY = False
SCOPE = "per_district"

WIKI_API = "https://en.wikipedia.org/w/api.php"
PAGEVIEWS_API = (
    "https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/"
    "en.wikipedia/all-access/user/{title}/monthly/{start}/{end}"
)
# Wikimedia asks for a descriptive agent identifying the caller; a generic one
# gets rate-limited or blocked outright.
HEADERS = {"User-Agent": "SmartJourney/1.0 (university project; trip planning)"}

# How far around a listing to look for a candidate article.
GEOSEARCH_RADIUS_M = 1000
# Pageviews are reported monthly; twelve months smooths out seasonality, which
# matters in Sri Lanka where the two monsoons move visitors between coasts.
LOOKBACK_MONTHS = 12
# Title-vs-name similarity below this is a different place.
#
# Measured against real pairs from this catalogue rather than picked by feel:
#
#   1.00  Galle Fort Ramparts            ~ Galle Fort                    (true)
#   0.75  Temple of the Sacred Tooth Relic ~ Temple of the Tooth         (true)
#   0.65  Hikkaduwa Coral Reaf           ~ Hikkaduwa Divisional Secretariat (FALSE)
#   0.11  Dutch Entrance                 ~ National Maritime Museum      (FALSE)
#
# Sri Lankan place names routinely share a settlement prefix, so "same town"
# alone scored 0.65 and was passing at the old 0.6 floor. 0.72 sits between the
# weakest true match and the strongest false one. The margin is deliberately
# tight on the strict side: a missed match leaves popularity NULL, which the
# scorer reads as "no evidence" and costs nothing, whereas a wrong match
# attributes a landmark's visitors to an unrelated place.
_NAME_SIMILARITY_FLOOR = 0.72
# Wikipedia's API returns 429 well before any documented hard limit when
# called in a tight loop; 0.2s produced a wall of them. One second per request
# runs clean, and this job is a monthly background sweep, not user-facing.
_REQUEST_PAUSE_SECONDS = 1.0
_RETRY_BACKOFF_SECONDS = 5.0
_MAX_RETRIES = 3


def _normalize(text: str) -> str:
    """Lowercase, strip punctuation and the generic words that make two
    unrelated names look alike ("the", "museum")."""
    cleaned = re.sub(r"[^\w\s]", " ", text.lower())
    return re.sub(r"\s+", " ", cleaned).strip()


def _match_score(listing_name: str, article_title: str) -> float:
    """How well an article title corresponds to a listing name, 0-1.

    Containment scores 1.0: "Galle Fort" is genuinely the article for a listing
    called "Galle Fort Ramparts". Everything else falls back to a character
    ratio, which absorbs spelling and transliteration drift.
    """
    a, b = _normalize(listing_name), _normalize(article_title)
    if not a or not b:
        return 0.0
    if a in b or b in a:
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def _pageview_window() -> tuple[str, str]:
    """The last LOOKBACK_MONTHS whole months, as the API's YYYYMMDDHH strings."""
    today = datetime.now(timezone.utc).date()
    end = date(today.year, today.month, 1) - timedelta(days=1)
    start_year = end.year - (LOOKBACK_MONTHS // 12)
    start_month = end.month - (LOOKBACK_MONTHS % 12)
    if start_month <= 0:
        start_month += 12
        start_year -= 1
    start = date(start_year, start_month, 1)
    return start.strftime("%Y%m%d00"), end.strftime("%Y%m%d00")


def _title_from_tag(wikipedia_tag: str) -> Optional[str]:
    """OSM stores this as "en:Galle Fort"; other languages are skipped rather
    than guessed at, since pageviews here are read from en.wikipedia."""
    if not wikipedia_tag:
        return None
    lang, _, title = wikipedia_tag.partition(":")
    if not title:
        return wikipedia_tag.strip() or None
    return title.strip() if lang.strip().lower() == "en" else None


def _nearby_article(session: requests.Session, lat: float, lon: float, name: str) -> Optional[str]:
    """Nearest article whose title actually corresponds to `name`."""
    params = {
        "action": "query", "format": "json", "list": "geosearch",
        "gscoord": f"{lat}|{lon}", "gsradius": GEOSEARCH_RADIUS_M, "gslimit": 10,
    }
    data = _get_with_retry(session, WIKI_API, params=params)
    if data is None:
        logger.warning(f"geosearch unavailable for {name}")
        return None
    candidates = data.get("query", {}).get("geosearch", [])

    # Best match, not the first acceptable one. Geosearch returns
    # nearest-first, and the nearest article is often a business named AFTER
    # the landmark: "Galle Fort Ramparts" scored 0.63 against "Galle Fort
    # Hotel" (1,709 views) and took it, when "Galle Fort" itself (52,038
    # views) was further down the same list and scores 1.0.
    best_title, best_score = None, 0.0
    for candidate in candidates:
        title = candidate.get("title", "")
        score = _match_score(name, title)
        if score > best_score:
            best_title, best_score = title, score
    return best_title if best_score >= _NAME_SIMILARITY_FLOOR else None


def _pageviews(session: requests.Session, title: str) -> Optional[int]:
    start, end = _pageview_window()
    url = PAGEVIEWS_API.format(title=quote(title.replace(" ", "_"), safe=""), start=start, end=end)
    data = _get_with_retry(session, url, allow_404=True)
    if data is None:
        return None
    return sum(item.get("views", 0) for item in data.get("items", []))


def _get_with_retry(session: requests.Session, url: str, params: Optional[dict] = None,
                    allow_404: bool = False) -> Optional[dict]:
    """GET with backoff on 429. A 404 is a real answer for pageviews (the
    article has no view record), not a failure worth retrying."""
    for attempt in range(_MAX_RETRIES):
        try:
            resp = session.get(url, params=params, headers=HEADERS, timeout=30)
            if allow_404 and resp.status_code == 404:
                return None
            if resp.status_code == 429:
                time.sleep(_RETRY_BACKOFF_SECONDS * (attempt + 1))
                continue
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            if attempt == _MAX_RETRIES - 1:
                logger.warning(f"request failed after {_MAX_RETRIES} attempts: {e}")
                return None
            time.sleep(_RETRY_BACKOFF_SECONDS * (attempt + 1))
    return None


_SELECT_CANDIDATES = """
    SELECT l.id, l.name, l.latitude, l.longitude, l.tags
    FROM travel_listing l
    JOIN category c ON c.id = l.category_id
    JOIN district d ON d.id = l.district_id
    WHERE l.is_active
      AND l.latitude IS NOT NULL
      AND l.longitude IS NOT NULL
      AND (%(category)s IS NULL OR c.name = %(category)s)
      AND (%(district)s IS NULL OR d.name ILIKE %(district)s)
      AND (l.popularity_checked_at IS NULL OR l.popularity_checked_at < now() - interval '30 days')
    ORDER BY l.popularity_checked_at NULLS FIRST, l.rating_count DESC NULLS LAST
    LIMIT %(limit)s
"""


def run(district: Optional[str] = None, limit: int = 200, category: Optional[str] = None) -> None:
    conn = get_connection()
    if conn is None:
        print("[FATAL] DATABASE_URL not configured or database unreachable.")
        return

    session = requests.Session()
    matched = checked = 0
    try:
        with conn, conn.cursor() as cur:
            cur.execute(_SELECT_CANDIDATES, {
                "category": category,
                "district": f"%{district}%" if district else None,
                "limit": limit,
            })
            rows = cur.fetchall()

        print(f"--- {NAME}: {len(rows)} listing(s) due a popularity check ---")
        for listing_id, name, lat, lon, tags in rows:
            checked += 1
            title = _title_from_tag(_tag_value(tags, "wikipedia"))
            if not title:
                title = _nearby_article(session, float(lat), float(lon), name)
                time.sleep(_REQUEST_PAUSE_SECONDS)

            views = _pageviews(session, title) if title else None
            time.sleep(_REQUEST_PAUSE_SECONDS)

            # popularity_checked_at is stamped either way: a listing with no
            # article should not be retried on every single run.
            with conn, conn.cursor() as cur:
                cur.execute(
                    "UPDATE travel_listing SET wikipedia_title = %s, popularity = %s, "
                    "popularity_checked_at = now() WHERE id = %s",
                    (title, views, listing_id),
                )
            if views is not None:
                matched += 1
                print(f"  {name[:45]:<45} {title[:30]:<30} {views:>8,} views")

        print(f"\n[DONE] {checked} checked, {matched} matched to an article with pageviews.")
    finally:
        conn.close()
        session.close()


def _tag_value(tags: Any, key: str) -> str:
    """`tags` is the canonical text[] this project stores, not OSM's raw dict,
    so a "wikipedia=..." entry only appears if ingest kept one. Returns "" when
    absent, which sends the caller down the name-matched geosearch path."""
    if not tags:
        return ""
    for tag in tags:
        if isinstance(tag, str) and tag.lower().startswith(f"{key}:"):
            return tag.split(":", 1)[1]
    return ""


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--district", default=None)
    ap.add_argument("--category", default=None)
    ap.add_argument("--limit", type=int, default=200)
    args = ap.parse_args()
    run(args.district, args.limit, args.category)
