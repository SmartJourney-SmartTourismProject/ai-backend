"""
Shared plumbing for connectors that read websites rather than APIs - see
docs/master_plan/SCRAPE_SOURCES.md for which sites, and why each was allowed.

Politeness is enforced here, once, so no individual connector can forget it:
every request checks the host's robots.txt, identifies itself, and waits
MIN_INTERVAL_S between hits to the same host. Parsing stays in the
connectors and uses structured data (JSON-LD, plain <table>s) - never an LLM,
whose token budget belongs to trip planning.

Synchronous on purpose (`requests`, time.sleep). The scheduler runs inside
the FastAPI process, so connectors must call their crawl through
asyncio.to_thread() - a 2 s politeness sleep on the event loop would stall
every API request for the length of the crawl.
"""
from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from typing import Optional
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import requests

from app.config.settings import settings

logger = logging.getLogger(__name__)

USER_AGENT = "SmartJourneyBot/1.0 (university trip-planning project; respects robots.txt)"
MIN_INTERVAL_S = 2.0
TIMEOUT_S = 30
RETRIES = 2

_robots: dict[str, Optional[RobotFileParser]] = {}
_last_hit: dict[str, float] = {}
_lock = threading.Lock()


def _robots_for(scheme: str, host: str) -> Optional[RobotFileParser]:
    """Cached per host. None means "no usable robots.txt" (404 or
    unreachable), which by convention places no restriction - the same
    reading every mainstream crawler uses."""
    key = f"{scheme}://{host}"
    if key not in _robots:
        parser = RobotFileParser()
        try:
            resp = requests.get(f"{key}/robots.txt", headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT_S)
            if resp.status_code == 200:
                parser.parse(resp.text.splitlines())
                _robots[key] = parser
            else:
                _robots[key] = None
        except requests.RequestException as e:
            logger.warning(f"robots.txt unreachable for {host}, treating as unrestricted: {e}")
            _robots[key] = None
    return _robots[key]


def _wait_turn(host: str) -> None:
    with _lock:
        wait = _last_hit.get(host, 0.0) + MIN_INTERVAL_S - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_hit[host] = time.monotonic()


def polite_get(url: str) -> Optional[str]:
    """The page body, or None if robots.txt disallows it or every attempt
    failed. Never raises - one bad page must not abort a whole crawl."""
    parts = urlsplit(url)
    robots = _robots_for(parts.scheme, parts.netloc)
    if robots is not None and not robots.can_fetch(USER_AGENT, url):
        logger.warning(f"robots.txt disallows {url} - skipped")
        return None

    for attempt in range(RETRIES + 1):
        _wait_turn(parts.netloc)
        try:
            resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT_S)
        except requests.RequestException as e:
            logger.warning(f"GET {url} failed (attempt {attempt + 1}): {e}")
            continue
        if resp.status_code == 200:
            resp.encoding = resp.encoding or "utf-8"
            return resp.text
        if resp.status_code < 500:
            # 4xx is an answer, not a blip - retrying a 404 or 403 is just noise.
            logger.warning(f"GET {url} -> {resp.status_code}")
            return None
        logger.warning(f"GET {url} -> {resp.status_code} (attempt {attempt + 1})")
    return None


def stable_ref(*parts: object) -> str:
    """A deterministic external_ref for sources with no id of their own, so a
    re-scrape updates the same row instead of inserting a duplicate.
    Case/whitespace-insensitive: "Kandy  Perahera" and "kandy perahera" are
    the same event."""
    norm = "|".join(re.sub(r"\s+", " ", str(p)).strip().lower() for p in parts)
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:20]


_AMOUNT = re.compile(r"(\d[\d,]*(?:\.\d+)?)")


def parse_lkr(text: object) -> Optional[float]:
    """A single price in LKR from free text or a bare number: "Rs. 1,500",
    "LKR 1500", "1500.00", "USD 30" (converted at settings.usd_lkr_rate).
    None for anything without a number - "Free" is a real zero, returned as 0."""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    s = str(text).strip()
    if not s:
        return None
    if re.search(r"\bfree\b", s, re.I):
        return 0.0
    m = _AMOUNT.search(s)
    if not m:
        return None
    value = float(m.group(1).replace(",", ""))
    if re.search(r"(usd|us\$|\$)", s, re.I):
        value *= settings.usd_lkr_rate
    return value
