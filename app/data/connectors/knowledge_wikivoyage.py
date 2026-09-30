"""
Monthly free ingest of Sri Lanka's Wikivoyage guides into the RAG knowledge
base (`knowledge_document`/`knowledge_chunk`, migration 0013) - the visa,
safety, etiquette, transport and cost information no other table in this
project holds. Vetting record: docs/master_plan/SCRAPE_SOURCES.md.

Uses the MediaWiki action API directly (like app/data/connectors/
wikidata_enrich.py and wikipedia_popularity.py already do for Wikipedia,
same software, same sanctioned bot route: a descriptive User-Agent +
maxlag + a modest rate, per https://meta.wikimedia.org/wiki/User-Agent_policy
and https://www.mediawiki.org/wiki/Manual:Maxlag_parameter). robots.txt's
blanket `Disallow: /w/` targets page-scraping bots hitting rendered HTML;
it does not cover this API route, which is the intended machine-access
path and is what those two existing connectors already rely on.

Discovery, depth <=3:
    Sri Lanka (country page)
    -> Category:Sri Lanka's direct page members (province/region guides,
       plus the odd stray page like Wilpattu National Park)
    -> each province's own Category:<Province>'s page members (city guides:
       Kandy, Galle, Ella, Sigiriya, ...)
Each page is placed by resolve_place(title) - never dropped for a weak
match; anything that doesn't resolve to a real Sri Lankan district (a
province-wide article can genuinely straddle several) falls back to
country-level (district_id=None) rather than being discarded.

    python -m app.data.connectors.knowledge_wikivoyage
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from typing import Any, Optional

import requests

from app.data.connectors.base import District
from app.rag.ingest import DocumentInput, run_ingest
from app.tools.geo_tool import resolve_place

logger = logging.getLogger(__name__)

API = "https://en.wikivoyage.org/w/api.php"
UA = {"User-Agent": "SmartJourneyBot/1.0 (university trip-planning project; "
                     "contact: smarttourism.project@example.com)"}
LICENSE = "CC BY-SA 3.0"
COUNTRY_PAGE = "Sri Lanka"
ROOT_CATEGORY = "Category:Sri Lanka"
REQUEST_DELAY_S = 1.0   # Wikimedia's own suggested ceiling for anonymous bot traffic
_MAX_RETRIES = 2

NAME = "knowledge_wikivoyage"
CADENCE = "monthly"
REQUIRES_KEY = False
SCOPE = "global"


def _get(params: dict) -> Optional[dict]:
    params = {**params, "format": "json", "maxlag": 5}
    for attempt in range(_MAX_RETRIES + 1):
        time.sleep(REQUEST_DELAY_S)
        try:
            resp = requests.get(API, params=params, headers=UA, timeout=30)
            if resp.status_code == 429 or resp.headers.get("Retry-After"):
                wait = int(resp.headers.get("Retry-After", 2 * (attempt + 1)))
                if attempt < _MAX_RETRIES:
                    time.sleep(wait)
                    continue
                return None
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.RequestException as e:
            logger.warning(f"Wikivoyage API call failed: {e}")
            return None
    return None


def _category_pages(category: str) -> list[str]:
    """Direct page members of `category` - cmtype=page excludes nested
    subcategories, which callers walk separately (one level at a time, so
    the depth-<=3 discovery limit stays an explicit loop, not a recursive
    surprise)."""
    data = _get({"action": "query", "list": "categorymembers", "cmtitle": category,
                 "cmtype": "page", "cmlimit": 500})
    if data is None:
        return []
    return [m["title"] for m in data.get("query", {}).get("categorymembers", [])]


def _subcategories(category: str) -> list[str]:
    data = _get({"action": "query", "list": "categorymembers", "cmtitle": category,
                 "cmtype": "subcat", "cmlimit": 500})
    if data is None:
        return []
    return [m["title"] for m in data.get("query", {}).get("categorymembers", [])]


def discover_pages() -> list[str]:
    """[COUNTRY_PAGE, *region pages, *city pages], de-duplicated, order
    preserved (country first, so a partial run still has the highest-value
    page)."""
    pages = [COUNTRY_PAGE]
    region_pages = _category_pages(ROOT_CATEGORY)
    pages.extend(p for p in region_pages if p not in pages)

    for province_category in _subcategories(ROOT_CATEGORY):
        for title in _category_pages(province_category):
            if title not in pages:
                pages.append(title)
    return pages


def _fetch_page_text(title: str) -> Optional[dict[str, Any]]:
    """One page's section-marked plain text + canonical URL, or None if the
    page doesn't exist / the API call failed (a redirect or a since-deleted
    category member - not an error worth aborting the whole run over)."""
    data = _get({
        "action": "query", "prop": "extracts|info",
        "explaintext": 1, "exsectionformat": "wiki",
        "inprop": "url", "redirects": 1, "titles": title,
    })
    if data is None:
        return None
    pages = list(data.get("query", {}).get("pages", {}).values())
    if not pages or "missing" in pages[0]:
        return None
    page = pages[0]
    text = page.get("extract", "")
    if not text.strip():
        return None
    return {"title": page.get("title", title), "url": page.get("fullurl"), "text": text}


class WikivoyageKnowledgeConnector:
    name = NAME
    cadence = CADENCE
    requires_key = REQUIRES_KEY
    scope = SCOPE

    async def fetch(self, district: Optional[District]) -> list[dict[str, Any]]:
        titles = await asyncio.to_thread(discover_pages)
        raw = []
        for title in titles:
            page = await asyncio.to_thread(_fetch_page_text, title)
            if page is None:
                continue
            place = None if title == COUNTRY_PAGE else await resolve_place(title)
            district_id = place.get("district_id") if place and place.get("confidence") != "out_of_country" else None
            raw.append({**page, "source_ref": title, "district_id": district_id})
        return raw

    def normalize(self, raw: list[dict[str, Any]], district: Optional[District]) -> list[DocumentInput]:
        docs = []
        for page in raw:
            docs.append(DocumentInput(
                source="wikivoyage",
                source_ref=page["source_ref"],
                title=page["title"],
                url=page["url"],
                license=LICENSE,
                district_id=page["district_id"],
                content_hash=hashlib.sha256(page["text"].encode("utf-8")).hexdigest(),
                text=page["text"],
            ))
        return docs

    def upsert(self, rows: list[DocumentInput]) -> int:
        written, embedded = run_ingest(rows)
        logger.info(f"knowledge_wikivoyage: {written} chunk(s) written, {embedded} embedded")
        return written


async def run() -> None:
    connector = WikivoyageKnowledgeConnector()
    raw = await connector.fetch(None)
    docs = connector.normalize(raw, None)
    written = connector.upsert(docs)
    for doc in docs:
        print(f"  {doc.title!r} ({doc.district_id or 'country-level'})")
    print(f"[SUCCESS] {len(docs)} page(s) fetched, {written} new/changed chunk(s) written.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())
