"""
Automated data-refresh scheduler, wired into the FastAPI app lifecycle
(see main.py's startup/shutdown events).

Cadence (per project decision, see BUILD_PLAN.md §1):
  - Weather:  no scheduled job - fetched live per request with a short Redis
              cache (see app/utils/cache.py + app/workflows/weather_agent.py).
  - Events:   weekly  (Ticketmaster + Eventbrite, all 25 districts).
  - Hotels/restaurants/attractions + real prices: monthly
              (Overpass + Booking.com, all 25 districts).

Runs in-process via APScheduler. Trade-off: jobs only fire while this server
process is running - a restart at the exact scheduled minute skips that run
(the next week/month's run still happens on schedule). Acceptable for this
project's scale; revisit with an external cron runner if that ever matters.
"""
from __future__ import annotations
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.data import events_ingest, overpass_ingest

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None


def start_scheduler() -> AsyncIOScheduler:
    """Creates and starts the background scheduler. Safe to call once at app startup."""
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    _scheduler = AsyncIOScheduler(timezone="Asia/Colombo")

    # Weekly events sync - every Monday at 02:00 local time.
    _scheduler.add_job(
        _run_safely(events_ingest.run_ingestion, "events_ingest"),
        trigger=CronTrigger(day_of_week="mon", hour=2, minute=0),
        id="weekly_events_sync",
        replace_existing=True,
    )

    # Monthly hotels/restaurants/attractions + price sync - 1st of month at 03:00 local time.
    _scheduler.add_job(
        _run_safely(overpass_ingest.run_ingestion, "overpass_ingest"),
        trigger=CronTrigger(day="1", hour=3, minute=0),
        id="monthly_listings_sync",
        replace_existing=True,
    )

    _scheduler.start()
    logger.info("Data-refresh scheduler started: events=weekly (Mon 02:00), listings=monthly (1st 03:00).")
    return _scheduler


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None


def _run_safely(fn, job_name: str):
    """Wraps a blocking ingestion job so one failed run logs instead of killing the scheduler."""
    def wrapped():
        try:
            fn()
        except Exception:
            logger.exception("Scheduled job '%s' failed", job_name)
    return wrapped
