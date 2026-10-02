"""
FastAPI server for SmartJourney AI Backend.

Mounts both tracks on one app:
  - Member A's Orchestrator track: trip planning (`app/api/trip.py`) and
    Google Calendar OAuth (`app/api/google_oauth.py`).
  - Member B's data track: the data-pipeline admin triggers/scheduler below.
    (RAG indexing was removed - see docs/AI_BACKEND_OPTIMIZATION_PLAN.md C1.)

Endpoints:
  POST /trip-plan            - Orchestrator: full validate/policy/slot-fill/
                                location/calendar/context/recommend/plan flow
  GET  /auth/google/login,
  GET  /auth/google/callback - Google Calendar OAuth consent flow
  GET  /                     - Health check
  GET  /api/health           - Health check (detailed)
  POST /api/admin/sync/events    - Trigger the events ingestion job
  POST /api/admin/sync/listings  - Trigger the listings ingestion job
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict

from dotenv import load_dotenv

# Must run before any other app import: app.config.settings reads .env via
# pydantic-settings for its own Settings fields, but that never populates
# os.environ - libraries that read env vars directly (e.g. langchain_google_genai's
# ChatGoogleGenerativeAI, which needs GOOGLE_API_KEY/GEMINI_API_KEY in os.environ)
# would otherwise fail at runtime even with a correctly filled-in .env.
load_dotenv()

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.trip import router as trip_router
from app.api.google_oauth import router as google_oauth_router
from app.api.admin_llm import router as admin_llm_router
from app.core import llm_config
from app.scheduler import start_scheduler, stop_scheduler
from app.utils.db_pool import close_pool
from app.data import pipeline
from app.config.settings import settings
from app.utils.internal_auth import require_internal_token

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Smart Tourism Assistant — AI Backend",
    description="Multi-agent travel planning system with LangGraph",
    version="2.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_allowed_origins.split(",") if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(trip_router, dependencies=[Depends(require_internal_token)])
app.include_router(google_oauth_router)
app.include_router(admin_llm_router)


# ------------------- API Endpoints -------------------

@app.get("/")
async def root():
    return {"status": "ok", "service": "smart-tourism-ai-backend"}


@app.get("/api/health")
async def health_check() -> Dict[str, Any]:
    """Health check endpoint."""
    return {"status": "healthy", "version": "2.0.0"}


def _run_pipeline_in_background(source: str) -> None:
    """Dispatches the WHOLE pipeline run to a worker thread via
    pipeline.run_sync(), not asyncio.create_task(pipeline.run(...)).

    Every connector's actual I/O (requests, psycopg2) is synchronous by
    design (app/data/postgres_writer.py's documented convention for batch
    scripts) - scheduling run() as a bare asyncio.Task was tried first and
    reproduced live: it froze the entire FastAPI server for the sync's full
    multi-minute duration, since the coroutine only truly yields at its few
    internal to_thread() points and blocks the event loop everywhere else.
    run_in_executor keeps this endpoint's "started" response fast and every
    other endpoint responsive while the sync runs - the same pattern the
    original code used (run_in_executor(None, events_ingest.run_ingestion)),
    just retargeted at the new pipeline.run_sync()."""
    asyncio.get_running_loop().run_in_executor(None, pipeline.run_sync, source)


@app.post("/api/admin/sync/events", dependencies=[Depends(require_internal_token)])
async def trigger_events_sync():
    """
    Manually trigger the Ticketmaster events sync (all districts).
    Runs in the background and returns immediately - the sync itself takes
    several minutes across 25 districts; check server logs for completion.
    """
    _run_pipeline_in_background("ticketmaster_events")
    return {"status": "started", "message": "Events sync started in the background."}


@app.post("/api/admin/sync/listings", dependencies=[Depends(require_internal_token)])
async def trigger_listings_sync():
    """
    Manually trigger the OSM hotels/restaurants/attractions sync (all districts).
    Runs in the background and returns immediately - the sync itself takes
    several minutes across 25 districts; check server logs for completion.
    """
    _run_pipeline_in_background("osm_listings")
    return {"status": "started", "message": "Listings sync started in the background."}


@app.on_event("startup")
async def startup_event():
    """Start the automated data-refresh scheduler on startup."""
    logger.info("Starting SmartJourney AI Backend...")
    start_scheduler()
    # Admin-chosen models/keys (Admin > AI models), re-read periodically.
    await llm_config.reload()
    llm_config.start_refresh()
    logger.info("Ready to serve requests")


@app.on_event("shutdown")
async def shutdown_event():
    """Stop the background scheduler and release the DB pool cleanly."""
    stop_scheduler()
    llm_config.stop_refresh()
    await close_pool()


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("API_PORT", "8000"))
    host = os.environ.get("API_HOST", "0.0.0.0")

    uvicorn.run(
        "main:app",
        host=host,
        port=port,
        reload=False,
        log_level="info",
    )
