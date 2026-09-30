"""
Quarterly heritage-site entry fees from the Central Cultural Fund ->
listing_entry_fee (migration 0012), landing as PENDING for admin review.
Vetting record: docs/master_plan/SCRAPE_SOURCES.md.

The source is one plain <table> (ccf.gov.lk/is/index.php, iframed into the
official Ticket Issuance page): Site | Full Ticket (USD) | Full Ticket (LKR) |
Half Ticket (USD) | Half Ticket (LKR). Foreign-visitor prices, VAT included.

Matching a CCF site name to a travel_listing is only a SUGGESTION. Measured
2026-09-30 against the real data, plain trigram similarity paired "Dambulla
(Museum)" with Badulla Museum and "Sigiriya (Museum)" with Abhayagiriya
Museum; even the strict rule used here (same district, CCF name contained
near-verbatim in the listing name) links "Sigiriya" to "Sigiriya viewpoint",
because no listing for the fortress itself exists. So nothing here reaches a
budget until an admin approves the row, and the admin can re-link it.

Re-scrapes never undo a review: status and an existing listing_id are kept,
EXCEPT that a price change on an approved fee sends it back to pending - an
approved number that silently changes is exactly what review is for.

    python -m app.data.connectors.entry_fees_ccf
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Optional

from bs4 import BeautifulSoup

from app.config.settings import settings
from app.data.connectors.base import District
from app.data.postgres_writer import get_connection
from app.data.scraping import parse_lkr, polite_get, stable_ref
from app.tools.geo_tool import resolve_place
from app.utils.db_pool import get_pool

logger = logging.getLogger(__name__)

NAME = "entry_fees_ccf"
CADENCE = "quarterly"
REQUIRES_KEY = False
SCOPE = "global"
SOURCE = "ccf"

PRICES_URL = "https://ccf.gov.lk/is/index.php"
# The page a human would visit - stored as source_url for the admin.
PAGE_URL = "https://ccf.gov.lk/ticket-issuance/"
# word_similarity(ccf_name, listing_name): 1.0 means every trigram of the CCF
# name appears in the listing name. Below this, leave the link to the admin.
MIN_WORD_SIMILARITY = 0.8

_MATCH_SQL = """
    SELECT l.id, l.name, word_similarity($1, l.name) AS ws
    FROM travel_listing l
    JOIN category c ON c.id = l.category_id
    WHERE c.name = 'attraction' AND l.is_active AND l.district_id = $2
    ORDER BY ws DESC
    LIMIT 1
"""

_UPSERT_SQL = """
    INSERT INTO listing_entry_fee
        (listing_id, site_name, foreign_adult, foreign_child, currency,
         source, source_url, external_ref, status, fetched_at)
    VALUES (%s, %s, %s, %s, 'LKR', %s, %s, %s, 'pending', now())
    ON CONFLICT (source, external_ref) DO UPDATE SET
        site_name     = EXCLUDED.site_name,
        source_url    = EXCLUDED.source_url,
        fetched_at    = now(),
        -- Column references on the right are the OLD row's values.
        status = CASE
            WHEN listing_entry_fee.status = 'approved'
             AND (listing_entry_fee.foreign_adult IS DISTINCT FROM EXCLUDED.foreign_adult
               OR listing_entry_fee.foreign_child IS DISTINCT FROM EXCLUDED.foreign_child)
            THEN 'pending'
            ELSE listing_entry_fee.status
        END,
        foreign_adult = EXCLUDED.foreign_adult,
        foreign_child = EXCLUDED.foreign_child,
        -- An admin's re-link (or an earlier match) always wins over a new guess.
        listing_id    = COALESCE(listing_entry_fee.listing_id, EXCLUDED.listing_id)
"""


def _cell_price(lkr_text: str, usd_text: str) -> Optional[float]:
    """The LKR column when present; the USD column converted otherwise."""
    lkr = parse_lkr(lkr_text)
    if lkr is not None:
        return lkr
    return parse_lkr(f"USD {usd_text}") if usd_text.strip() else None


def parse_fee_table(html: str) -> list[dict[str, Any]]:
    """One dict per priced site. Skips the header, group headings with no
    price ("Galle"), and "- Shipwreck diving tour ..." rows - those are
    activities under a heading, not sites with an entry fee."""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    if table is None:
        return []
    out = []
    for tr in table.find_all("tr"):
        cells = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
        if len(cells) < 5:
            continue    # header row uses <th>, or a malformed row
        site = cells[0]
        if not site or site.startswith("-"):
            continue
        adult = _cell_price(cells[2], cells[1])
        if adult is None:
            continue
        out.append({"site_name": site, "foreign_adult": adult, "foreign_child": _cell_price(cells[4], cells[3])})
    return out


def split_site_name(site: str) -> tuple[str, str]:
    """("Galle (Museum)") -> ("Galle", "Galle Museum"): the town to place the
    site with, and the name to match listings against."""
    town = re.sub(r"\s*\(.*?\)", "", site).strip()
    query = re.sub(r"\s+", " ", re.sub(r"[()]", " ", site)).strip()
    return town, query


class CCFEntryFeesConnector:
    name = NAME
    cadence = CADENCE
    requires_key = REQUIRES_KEY
    scope = SCOPE

    async def fetch(self, district: Optional[District]) -> list[dict[str, Any]]:
        html = await asyncio.to_thread(polite_get, PRICES_URL)
        if html is None:
            return []
        rows = parse_fee_table(html)
        pool = await get_pool()
        for row in rows:
            row["listing_id"] = await self._suggest_listing(pool, row["site_name"])
        return rows

    async def _suggest_listing(self, pool, site: str) -> Optional[str]:
        if pool is None:
            return None
        town, query = split_site_name(site)
        try:
            place = await resolve_place(town)
            district_id = place.get("district_id") if place else None
            if not district_id:
                return None
            match = await pool.fetchrow(_MATCH_SQL, query, district_id)
        except Exception as e:
            logger.warning(f"entry_fees_ccf: listing match failed for {site!r}: {e}")
            return None
        if match and match["ws"] >= MIN_WORD_SIMILARITY:
            logger.info(f"entry_fees_ccf: suggesting {match['name']!r} for {site!r} (ws={match['ws']:.2f})")
            return str(match["id"])
        return None

    def normalize(self, raw: list[dict[str, Any]], district: Optional[District]) -> list[dict[str, Any]]:
        return [{**r, "external_ref": stable_ref(r["site_name"])} for r in raw]

    def upsert(self, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0
        conn = get_connection()
        if conn is None:
            return 0
        try:
            with conn, conn.cursor() as cur:
                cur.executemany(_UPSERT_SQL, [
                    (r["listing_id"], r["site_name"], r["foreign_adult"], r["foreign_child"],
                     SOURCE, PAGE_URL, r["external_ref"])
                    for r in rows
                ])
            return len(rows)
        except Exception as e:
            logger.error(f"entry_fees_ccf: upsert failed: {e}")
            return 0
        finally:
            conn.close()


async def run() -> None:
    connector = CCFEntryFeesConnector()
    raw = await connector.fetch(None)
    rows = connector.normalize(raw, None)
    count = connector.upsert(rows)
    for r in rows:
        link = "-> listing suggested" if r["listing_id"] else "(no listing match)"
        print(f"  {r['site_name']:40} {r['foreign_adult']:>10,.0f} LKR  {link}")
    print(f"[SUCCESS] {count} fee(s) upserted for admin review (USD rate {settings.usd_lkr_rate}).")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())
