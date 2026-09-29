"""
Ingestion pipeline orchestrator - runs each registered connector across
districts (or once, for global-scope connectors), records a
`data_source_run` row per (connector, district) pair, and never lets one
failure abort the rest (docs/master_plan/DATA_PLATFORM.md §5.1).

    python -m app.data.pipeline                          # everything due
    python -m app.data.pipeline --source osm_listings     # one connector, all districts
    python -m app.data.pipeline --source osm_listings --district Kandy
    python -m app.data.pipeline --dry-run                 # show the plan, run nothing
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.data.connectors.base import District, fetch_all_districts
from app.data.postgres_writer import get_connection

logger = logging.getLogger(__name__)

# name -> (connector CLASS, cadence, requires_key, scope, display_name).
# Deliberately NOT instantiated here - see _get_connector()'s docstring for
# why eager construction was a real bug, not a style preference.
_REGISTRY: dict[str, dict] = {}
_INSTANCE_CACHE: dict[str, object] = {}


def _load_registry() -> dict[str, dict]:
    """Deferred import so a connector missing an optional dependency (e.g.
    requests) doesn't break `python -m app.data.pipeline --help`. Stores
    connector CLASSES, not instances - construction happens lazily in
    _get_connector(), only for the connector(s) actually being run."""
    global _REGISTRY
    if _REGISTRY:
        return _REGISTRY

    from app.data.connectors import osm_listings, booking_prices, ticketmaster_events

    _REGISTRY = {
        osm_listings.NAME: {
            "connector_cls": osm_listings.OSMListingsConnector,
            "cadence": osm_listings.CADENCE,
            "requires_key": osm_listings.REQUIRES_KEY,
            "scope": osm_listings.SCOPE,
            "display_name": "OSM base listings",
        },
        booking_prices.NAME: {
            "connector_cls": booking_prices.BookingPricesConnector,
            "cadence": booking_prices.CADENCE,
            "requires_key": booking_prices.REQUIRES_KEY,
            "scope": booking_prices.SCOPE,
            "display_name": "Booking.com hotel prices",
        },
        ticketmaster_events.NAME: {
            "connector_cls": ticketmaster_events.TicketmasterEventsConnector,
            "cadence": ticketmaster_events.CADENCE,
            "requires_key": ticketmaster_events.REQUIRES_KEY,
            "scope": ticketmaster_events.SCOPE,
            "display_name": "Ticketmaster events",
        },
    }
    return _REGISTRY


async def _get_connector(name: str, meta: dict):
    """Constructs (and caches) a connector on demand - never eagerly for
    the whole registry.

    Found live 2026-09-02: the previous version built every registered
    connector inside _load_registry(), including OSMListingsConnector,
    whose __init__ makes a blocking synchronous psycopg2 call
    (fetch_tag_mapping). Since _load_registry() ran on first use regardless
    of --source, requesting even --source ticketmaster_events still built
    OSMListingsConnector - and when triggered from a FastAPI endpoint via
    asyncio.create_task(), that blocking call executed ON THE EVENT LOOP,
    freezing the entire server for its duration (reproduced: a background
    /api/admin/sync/listings call made the whole app unresponsive).
    asyncio.to_thread() moves any such blocking __init__ work off the loop,
    and constructing only the requested connector avoids paying for the
    other two entirely."""
    if name in _INSTANCE_CACHE:
        return _INSTANCE_CACHE[name]
    instance = await asyncio.to_thread(meta["connector_cls"])
    _INSTANCE_CACHE[name] = instance
    return instance


_CADENCE_DAYS = {"daily": 1, "weekly": 7, "monthly": 30, "quarterly": 90}


def _due_connectors(registry: dict[str, dict]) -> dict[str, dict]:
    """Filters to connectors whose cadence has actually elapsed since their
    last successful run - the nightly scheduler job runs against this, not
    the full registry, or a weekly connector would burn its Overpass/API
    budget every night instead of once a week (docs/master_plan/DATA_PLATFORM.md §5.4)."""
    conn = get_connection()
    if conn is None:
        return registry  # can't check - let each connector's own fetch() decide

    due = {}
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SELECT name, last_success_at FROM data_source")
            last_success = dict(cur.fetchall())
    except Exception as e:
        logger.warning(f"_due_connectors: could not read data_source, running everything: {e}")
        return registry
    finally:
        conn.close()

    now = datetime.now(timezone.utc)
    for name, meta in registry.items():
        if meta["cadence"] == "manual":
            continue   # manual-only connectors (e.g. a future cost_seed) never run unattended
        last = last_success.get(name)
        gap_days = _CADENCE_DAYS.get(meta["cadence"], 1)
        if last is None or (now - last) >= timedelta(days=gap_days):
            due[name] = meta
    return due


def _ensure_data_source_rows(registry: dict[str, dict]) -> None:
    """Upserts the data_source registry table itself from what's actually
    wired up in code - single source of truth, no drift between the two."""
    conn = get_connection()
    if conn is None:
        return
    try:
        with conn, conn.cursor() as cur:
            for name, meta in registry.items():
                cur.execute(
                    "INSERT INTO data_source (name, display_name, cadence, requires_key) "
                    "VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT (name) DO UPDATE SET "
                    "display_name = EXCLUDED.display_name, cadence = EXCLUDED.cadence, "
                    "requires_key = EXCLUDED.requires_key",
                    (name, meta["display_name"], meta["cadence"], meta["requires_key"]),
                )
    except Exception as e:
        logger.error(f"_ensure_data_source_rows failed: {e}")
    finally:
        conn.close()


def _start_run(source: str, district_id: Optional[str]) -> Optional[str]:
    conn = get_connection()
    if conn is None:
        return None
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO data_source_run (source, district_id, status) "
                "VALUES (%s, %s, 'running') RETURNING id",
                (source, district_id),
            )
            return str(cur.fetchone()[0])
    except Exception as e:
        logger.error(f"_start_run failed: {e}")
        return None
    finally:
        conn.close()


def _finish_run(run_id: Optional[str], status: str, rows_fetched: int = 0,
                rows_upserted: int = 0, error: Optional[str] = None) -> None:
    if run_id is None:
        return
    conn = get_connection()
    if conn is None:
        return
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE data_source_run SET status=%s, finished_at=%s, "
                "rows_fetched=%s, rows_upserted=%s, error=%s WHERE id=%s",
                (status, datetime.now(timezone.utc), rows_fetched, rows_upserted, error, run_id),
            )
            if status == "success":
                cur.execute(
                    "UPDATE data_source SET last_success_at=%s WHERE name = "
                    "(SELECT source FROM data_source_run WHERE id=%s)",
                    (datetime.now(timezone.utc), run_id),
                )
    except Exception as e:
        logger.error(f"_finish_run failed: {e}")
    finally:
        conn.close()


async def _run_connector(name: str, meta: dict, district_filter: Optional[str]) -> None:
    connector = await _get_connector(name, meta)
    display = meta["display_name"]

    if meta["scope"] == "global":
        targets: list[Optional[District]] = [None]
    else:
        # fetch_all_districts() is sync/psycopg2 (postgres_writer.py's
        # convention for batch scripts) - to_thread() keeps it off the
        # event loop, same fix as _get_connector() above.
        districts = await asyncio.to_thread(fetch_all_districts)
        if district_filter:
            districts = [d for d in districts if district_filter.lower() in d.name.lower()]
        targets = list(districts)
        if not targets:
            print(f"  [!] {display}: no matching districts - skipped.")
            return

    for i, district in enumerate(targets, 1):
        label = district.name if district else "(global)"
        print(f"  [{i}/{len(targets)}] {display} / {label} ...", end=" ", flush=True)

        run_id = _start_run(name, district.id if district else None)
        try:
            raw = await connector.fetch(district)
            rows = connector.normalize(raw, district)
            upserted = connector.upsert(rows)
            status = "success" if raw or not meta["requires_key"] else "partial"
            _finish_run(run_id, status, rows_fetched=len(raw), rows_upserted=upserted)
            print(f"{upserted} upserted (fetched {len(raw)})")
        except Exception as e:
            logger.exception(f"{name} failed for {label}")
            _finish_run(run_id, "failed", error=str(e))
            print(f"FAILED: {e}")
        # A failing district must never abort the rest of the pipeline - each
        # (connector, district) pair is independent (DATA_PLATFORM.md §5.1).


async def run(source_filter: Optional[str], district_filter: Optional[str], dry_run: bool,
              due_only: bool = False) -> None:
    registry = _load_registry()
    _ensure_data_source_rows(registry)

    if source_filter and source_filter not in registry:
        print(f"[FATAL] Unknown source '{source_filter}'. Known: {', '.join(registry)}")
        return
    targets = {source_filter: registry[source_filter]} if source_filter else registry

    if due_only:
        targets = _due_connectors(targets)
        if not targets:
            print("--- PIPELINE: nothing due (every connector ran within its cadence window) ---")
            return

    print(f"--- PIPELINE: {len(targets)} connector(s) ---")
    for name, meta in targets.items():
        key_note = " (needs API key)" if meta["requires_key"] else ""
        print(f"  - {name}: {meta['display_name']}{key_note}, scope={meta['scope']}")

    if dry_run:
        print("\n[DRY RUN] Nothing executed.")
        return

    for name, meta in targets.items():
        print(f"\n=== {name} ===")
        await _run_connector(name, meta, district_filter)

    print("\n[DONE] Pipeline run complete. Check data_source_run for per-district results.")


def run_sync(source_filter: Optional[str] = None, district_filter: Optional[str] = None,
             dry_run: bool = False, due_only: bool = False) -> None:
    """Blocking entry point for callers that are NOT already inside an
    asyncio event loop needing to stay responsive - e.g. main.py's admin
    sync endpoints dispatch this via run_in_executor/to_thread rather than
    asyncio.create_task(run(...)).

    Why this exists: every connector's fetch/normalize/upsert body is
    genuinely synchronous I/O (requests, psycopg2 - the same sync-batch-
    script convention as postgres_writer.py throughout app/data/), so
    run()'s few internal `await asyncio.to_thread(...)` calls reduce but
    don't eliminate event-loop blocking. Scheduling run() itself as a bare
    asyncio.Task inside a live FastAPI process was reproduced live
    2026-09-02 to freeze the whole server for the sync's full duration.
    Running the entire thing in one dedicated worker thread (this function,
    via asyncio.run()) is what actually keeps the main event loop free -
    matching the original design's run_in_executor(None, ...) pattern
    exactly, just retargeted at the new pipeline."""
    asyncio.run(run(source_filter, district_filter, dry_run, due_only))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", default=None, help="run only this connector by name")
    ap.add_argument("--district", default=None, help="substring match, e.g. 'Kandy'")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--due-only", action="store_true",
                    help="skip connectors whose cadence hasn't elapsed since their last success")
    args = ap.parse_args()
    asyncio.run(run(args.source, args.district, args.dry_run, args.due_only))
