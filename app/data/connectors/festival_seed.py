"""
Curated Sri Lankan festivals -> local_event, dated from the official Poya
calendar rather than typed in by hand.

Each row of app/data/festival_seed.csv is a RULE ("10 days ending on Esala
Poya"), not a date. This connector resolves the rule against
holidays.country_holidays("LK"), which follows the government gazette - so
leap-month quirks (2026 has an extra Adhi Poson Poya) come out right, which a
plain full-moon calculation would get wrong. A year the gazette data doesn't
cover yet is skipped with a warning, never guessed; upgrading the `holidays`
package and letting this run again picks it up. See
docs/master_plan/SCRAPE_SOURCES.md.

Hand-curated, so rows are inserted already approved (is_verified=true, the
same trust level as an admin-entered event). insert_only keeps a later admin
decision - e.g. rejecting one - from being overwritten by the next run.

    python -m app.data.connectors.festival_seed
"""
from __future__ import annotations

import asyncio
import csv
import logging
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Optional

import holidays

from app.data.connectors.base import District, fetch_all_districts
from app.data.postgres_writer import to_point_wkt, upsert_rows
from app.data.scraping import stable_ref
from app.utils.clock import SRI_LANKA_TZ, today_local

logger = logging.getLogger(__name__)

NAME = "festival_seed"
# Monthly, not manual: it costs no network, and a monthly run is what picks
# up next year's dates as soon as the `holidays` package is upgraded.
CADENCE = "monthly"
REQUIRES_KEY = False
SCOPE = "global"
SOURCE = "curated"

SEED_CSV = Path(__file__).resolve().parent.parent / "festival_seed.csv"


def poya_dates(year: int) -> dict[str, date]:
    """{"Esala": date, "Vesak": date, ...} for one year, from the gazette.
    Empty if the package doesn't cover that year yet. Names come from lines
    like "Esala Full Moon Poya Day"; a date can carry several holidays
    joined with "; " (2026-05-01 is both Workers' Day and Vesak)."""
    out: dict[str, date] = {}
    for day, names in holidays.country_holidays("LK", years=year, language="en_US").items():
        for name in names.split("; "):
            if name.endswith(" Full Moon Poya Day"):
                out[name.removesuffix(" Full Moon Poya Day")] = day
    return out


def load_rules(path: Path = SEED_CSV) -> list[dict[str, str]]:
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def resolve_rule(rule: dict[str, str], year: int,
                 calendars: dict[int, dict[str, date]]) -> Optional[tuple[date, date]]:
    """(start, end) for this rule's occurrence that STARTS in `year`, or None
    if the gazette doesn't cover a Poya it needs. `end_next_year` covers
    seasons that cross New Year (Sri Pada: December to May)."""
    start_poya = calendars.get(year, {}).get(rule["anchor_poya"])
    end_year = year + 1 if rule["end_next_year"].strip().lower() == "true" else year
    end_poya = calendars.get(end_year, {}).get(rule["end_anchor_poya"])
    if start_poya is None or end_poya is None:
        return None
    start = start_poya + timedelta(days=int(rule["start_offset_days"]))
    end = end_poya + timedelta(days=int(rule["end_offset_days"]))
    return start, end


class FestivalSeedConnector:
    name = NAME
    cadence = CADENCE
    requires_key = REQUIRES_KEY
    scope = SCOPE

    async def fetch(self, district: Optional[District]) -> list[dict[str, Any]]:
        """One raw row per (rule, year) that resolves, for this year and next.
        District names -> ids are looked up here (a DB read), so normalize
        stays pure."""
        rules = load_rules()
        this_year = today_local().year
        years = [this_year, this_year + 1]
        # +1 again: a season starting next December ends the year after.
        calendars = {y: poya_dates(y) for y in (*years, this_year + 2)}
        districts = {d.name: d.id for d in await asyncio.to_thread(fetch_all_districts)}

        raw = []
        for rule in rules:
            district_id = districts.get(rule["district"])
            if district_id is None:
                logger.warning(f"festival_seed: unknown district {rule['district']!r} for {rule['name']!r} - skipped")
                continue
            for year in years:
                dates = resolve_rule(rule, year, calendars)
                if dates is None:
                    logger.warning(
                        f"festival_seed: no gazette Poya dates for {rule['name']!r} in {year} yet - "
                        f"skipped (upgrade the `holidays` package to pick it up)"
                    )
                    continue
                raw.append({**rule, "district_id": district_id, "start": dates[0], "end": dates[1]})
        return raw

    def normalize(self, raw: list[dict[str, Any]], district: Optional[District]) -> list[dict[str, Any]]:
        today = today_local()
        rows = []
        for r in raw:
            if r["end"] < today:
                continue    # already over - nothing for a planner or the Explore rail to offer
            rows.append({
                "district_id": r["district_id"],
                "name": r["name"],
                "description": r["description"],
                # Whole days in Sri Lanka time: a festival day is a local day.
                "start_datetime": datetime.combine(r["start"], time.min, SRI_LANKA_TZ),
                "end_datetime": datetime.combine(r["end"], time.max.replace(microsecond=0), SRI_LANKA_TZ),
                "venue_name": r["venue_name"],
                "lat": float(r["lat"]),
                "lon": float(r["lon"]),
                "tags": [t for t in r["tags"].split(";") if t],
                "external_ref": stable_ref(r["name"], r["start"].isoformat()),
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
            # Unknown, not 0: watching from the street is free, but Perahera
            # grandstand seats are sold - a budget must not assume either.
            "price_min": None,
            "price_max": None,
            "source": SOURCE,
            "external_ref": r["external_ref"],
            "is_verified": True,
        } for r in rows]
        return upsert_rows(
            "local_event", db_rows, on_conflict=("source", "external_ref"), geo_columns={"location"},
            insert_only={"is_verified"},
        )


async def run() -> None:
    connector = FestivalSeedConnector()
    raw = await connector.fetch(None)
    rows = connector.normalize(raw, None)
    count = connector.upsert(rows)
    for r in rows:
        print(f"  {r['start_datetime'].date()} -> {r['end_datetime'].date()}  {r['name']}")
    print(f"[SUCCESS] {count} festival(s) upserted ({len(raw)} resolved, {len(raw) - len(rows)} already over).")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())
