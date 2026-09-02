"""
Lazy, budget-capped contact-info enrichment via Foursquare Places -
docs/master_plan/DATA_PLATFORM.md §5.2, API_SETUP.md §4.1.

⚠️ Free-tier-only by design: `rating`/`price`/`stats` return HTTP 429 with
"Purchasing credits is required" even on a subscribed free-tier key
(verified live 2026-09-02) - they are Premium-gated, not part of the free
"Pro" allowance regardless of remaining monthly quota. Given this project's
"completely free" constraint, this connector requests ONLY confirmed-free
fields (name, category, tel, website) and never touches rating/price. The
real free rating source for hotels is booking_prices.py (Booking's
reviewScore, already in that connector's existing payload); for
restaurants/attractions it's wikidata_enrich's langlinkscount.

Lazy + budget-capped because the free allowance is 500 Pro calls/MONTH
(cut from a much larger figure in June 2026) - enriches only the top N
candidates per district (ranked by rating_count, so listings a user is
actually likely to be shown get priority), skips anything enriched in the
last 90 days, and hard-stops at FOURSQUARE_MONTHLY_BUDGET.

    python -m app.data.connectors.foursquare_enrich --district "Kandy" --limit 20
"""
from __future__ import annotations

import argparse
import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

import requests

from app.config.settings import settings
from app.data.connectors.base import District, fetch_all_districts
from app.data.postgres_writer import get_connection

logger = logging.getLogger(__name__)

FOURSQUARE_URL = "https://places-api.foursquare.com/places/search"
API_VERSION = "2025-06-17"
SEARCH_RADIUS_M = 100
ENRICH_COOLDOWN_DAYS = 90
PER_DISTRICT_LIMIT = 20

NAME = "foursquare_enrich"
CADENCE = "weekly"
REQUIRES_KEY = True
SCOPE = "per_district"


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.foursquare_api_key}",
        "X-Places-Api-Version": API_VERSION,
        "accept": "application/json",
    }


def _monthly_call_count(conn) -> int:
    """Calls this month so far, tracked via data_source_run.rows_fetched
    (one call = one row_fetched, since fetch() below issues exactly one
    Foursquare request per candidate)."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(SUM(rows_fetched), 0) FROM data_source_run "
                "WHERE source = %s AND started_at >= date_trunc('month', now())",
                (NAME,),
            )
            return cur.fetchone()[0]
    except Exception as e:
        logger.warning(f"foursquare_enrich: monthly call count lookup failed: {e}")
        return 0


class FoursquareEnrichConnector:
    name = NAME
    cadence = CADENCE
    requires_key = REQUIRES_KEY
    scope = SCOPE

    def __init__(self, limit: int = PER_DISTRICT_LIMIT) -> None:
        self.limit = limit
        self.calls_made = 0   # exposed so pipeline.py's rows_fetched tracking reflects real API usage

    async def fetch(self, district: Optional[District]) -> list[dict[str, Any]]:
        if district is None:
            raise ValueError("foursquare_enrich is a per_district connector")
        if not settings.foursquare_api_key:
            return []

        conn = get_connection()
        if conn is None:
            return []
        try:
            remaining_budget = settings.foursquare_monthly_budget - _monthly_call_count(conn)
            if remaining_budget <= 0:
                logger.warning("foursquare_enrich: monthly budget exhausted - skipping this run.")
                return []

            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, name, latitude, longitude FROM travel_listing "
                    "WHERE district_id = %s AND (foursquare_checked_at IS NULL "
                    "OR foursquare_checked_at < now() - interval '%s days') "
                    "ORDER BY rating_count DESC NULLS LAST LIMIT %s",
                    (district.id, ENRICH_COOLDOWN_DAYS, min(self.limit, remaining_budget)),
                )
                candidates = [
                    {"id": str(r[0]), "name": r[1], "lat": r[2], "lon": r[3]}
                    for r in cur.fetchall()
                ]
        except Exception as e:
            logger.error(f"foursquare_enrich.fetch failed: {e}")
            return []
        finally:
            conn.close()

        results = []
        for c in candidates:
            try:
                resp = requests.get(FOURSQUARE_URL, params={
                    "ll": f"{c['lat']},{c['lon']}", "radius": SEARCH_RADIUS_M, "limit": 1,
                    "fields": "fsq_place_id,name,tel,website",
                }, headers=_headers(), timeout=15)
                self.calls_made += 1
                if resp.status_code == 429:
                    logger.warning("foursquare_enrich: 429 mid-run (budget/rate) - stopping early.")
                    break
                resp.raise_for_status()
                hits = resp.json().get("results", [])
                results.append({"listing_id": c["id"], "match": hits[0] if hits else None})
            except requests.exceptions.RequestException as e:
                logger.warning(f"Foursquare lookup failed for listing {c['id']}: {e}")
                results.append({"listing_id": c["id"], "match": None})
            time.sleep(0.2)
        return results

    def normalize(self, raw: list[dict[str, Any]], district: Optional[District]) -> list[dict[str, Any]]:
        # Every candidate is checked (even a non-match sets foursquare_checked_at,
        # so it isn't retried every run) - both cases are meaningful rows.
        return raw

    def upsert(self, rows: list[dict[str, Any]]) -> int:
        """Marks every checked candidate as checked (match or not - either
        way it shouldn't be retried for ENRICH_COOLDOWN_DAYS). tel/website
        aren't persisted: no column exists for them and nothing downstream
        reads them yet - this connector's real remaining job, now that
        rating/price are Premium-gated, is confirming freshness against a
        second source, not writing new fields."""
        if not rows:
            return 0
        conn = get_connection()
        if conn is None:
            return 0
        now = datetime.now(timezone.utc)
        try:
            with conn, conn.cursor() as cur:
                for r in rows:
                    cur.execute(
                        "UPDATE travel_listing SET foursquare_checked_at = %s WHERE id = %s",
                        (now, r["listing_id"]),
                    )
            return sum(1 for r in rows if r.get("match"))
        except Exception as e:
            logger.error(f"foursquare_enrich upsert failed: {e}")
            return 0
        finally:
            conn.close()


async def run_all(district_filter: str = None, limit: int = PER_DISTRICT_LIMIT) -> None:
    connector = FoursquareEnrichConnector(limit=limit)
    districts = fetch_all_districts()
    if district_filter:
        districts = [d for d in districts if district_filter.lower() in d.name.lower()]
    if not districts:
        print("[FATAL] No districts found.")
        return
    if not settings.foursquare_api_key:
        print("[WARN] FOURSQUARE_API_KEY not set - nothing to enrich.")
        return

    total_matched, total_processed = 0, 0
    for i, d in enumerate(districts, 1):
        print(f"[{i}/{len(districts)}] {d.name} ...", end=" ", flush=True)
        raw = await connector.fetch(d)
        rows = connector.normalize(raw, d)
        matched = connector.upsert(rows)
        total_matched += matched
        total_processed += len(rows)
        print(f"{matched}/{len(rows)} matched ({connector.calls_made} API calls total this run)")

    print(f"\n[SUCCESS] {total_matched}/{total_processed} listing(s) matched on Foursquare "
          f"({connector.calls_made} API call(s) used).")


if __name__ == "__main__":
    import asyncio

    logging.basicConfig(level=logging.INFO)
    ap = argparse.ArgumentParser()
    ap.add_argument("--district", default=None)
    ap.add_argument("--limit", type=int, default=PER_DISTRICT_LIMIT)
    args = ap.parse_args()
    asyncio.run(run_all(args.district, args.limit))
