"""
Automated data-refresh scheduler, wired into the FastAPI app lifecycle
(see main.py's startup/shutdown events).

One nightly pipeline job, not one job per connector - each connector
carries its own cadence (data_source.cadence) and pipeline.py's --due-only
flag skips anything that hasn't reached its interval yet
(docs/master_plan/DATA_PLATFORM.md §5.4). This replaced the old two-job
design (weekly events, monthly listings) once ticketmaster_events actually
became daily-cadence and osm_listings/booking_prices/foursquare_enrich all
became weekly - a per-connector cron entry would have needed editing every
time a cadence changed; --due-only doesn't.

Also runs the two housekeeping jobs the plan calls for: expiring old
ai_session rows and pruning the travel_time cache.

Runs in-process via APScheduler. Trade-off: jobs only fire while this server
process is running - a restart at the exact scheduled minute skips that run
(the next day's run still happens on schedule). Acceptable for this
project's scale; revisit with an external cron runner if that ever matters.
"""
from __future__ import annotations
import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.data import pipeline
from app.utils.db_pool import get_pool

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None


def start_scheduler() -> AsyncIOScheduler:
    """Creates and starts the background scheduler. Safe to call once at app startup."""
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    _scheduler = AsyncIOScheduler(timezone="Asia/Colombo")

    # Nightly pipeline - runs whichever connectors are actually due per
    # their own cadence, never every connector every night.
    _scheduler.add_job(
        _run_safely_async(_run_due_pipeline, "pipeline_daily"),
        trigger=CronTrigger(hour=2, minute=30),
        id="pipeline_daily",
        replace_existing=True,
    )

    # Expired multi-turn conversation state - ai_session.expires_at defaults
    # to 7 days out (AGENT_ARCHITECTURE.md §5).
    _scheduler.add_job(
        _run_safely_async(_session_gc, "session_gc"),
        trigger=CronTrigger(hour=4, minute=0),
        id="session_gc",
        replace_existing=True,
    )

    # travel_time entries older than 90 days (their own TTL, DATA_PLATFORM.md §7).
    _scheduler.add_job(
        _run_safely_async(_travel_time_gc, "travel_time_gc"),
        trigger=CronTrigger(day_of_week="mon", hour=4, minute=10),
        id="travel_time_gc",
        replace_existing=True,
    )

    _scheduler.start()
    logger.info(
        "Data-refresh scheduler started: pipeline=nightly 02:30 (due-only), "
        "session_gc=daily 04:00, travel_time_gc=weekly Mon 04:10."
    )
    return _scheduler


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None


async def _run_due_pipeline() -> None:
    # to_thread, not a direct await - pipeline.run() only truly yields at a
    # few internal points and otherwise blocks on synchronous requests/
    # psycopg2 calls throughout (see pipeline.run_sync()'s docstring for
    # the live-reproduced freeze this avoids). APScheduler's AsyncIOScheduler
    # runs jobs on the same event loop FastAPI serves requests on, so this
    # matters here exactly as much as it does for the admin sync endpoints.
    await asyncio.to_thread(pipeline.run_sync, None, None, False, True)


async def _session_gc() -> None:
    pool = await get_pool()
    if pool is None:
        logger.debug("session_gc: no database configured, skipping.")
        return
    result = await pool.execute("DELETE FROM ai_session WHERE expires_at < now()")
    logger.info(f"session_gc: {result}")


async def _travel_time_gc() -> None:
    pool = await get_pool()
    if pool is None:
        logger.debug("travel_time_gc: no database configured, skipping.")
        return
    result = await pool.execute("DELETE FROM travel_time WHERE fetched_at < now() - interval '90 days'")
    logger.info(f"travel_time_gc: {result}")


def _run_safely_async(coro_fn, job_name: str):
    """Wraps an async job so one failed run logs instead of killing the scheduler."""
    async def wrapped():
        try:
            await coro_fn()
        except Exception:
            logger.exception("Scheduled job '%s' failed", job_name)
    return wrapped
