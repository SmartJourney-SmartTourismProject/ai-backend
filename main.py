"""
FastAPI server for SmartJourney AI Backend.

Mounts both tracks on one app:
  - Member A's Orchestrator track: trip planning (`app/api/trip.py`) and
    Google Calendar OAuth (`app/api/google_oauth.py`).
  - Member B's data/RAG track: RAG indexing and the data-pipeline admin
    triggers/scheduler below.

Endpoints:
  POST /trip-plan            - Orchestrator: full validate/policy/slot-fill/
                                location/calendar/context/recommend/plan flow
  GET  /auth/google/login,
  GET  /auth/google/callback - Google Calendar OAuth consent flow
  GET  /                     - Health check
  GET  /api/health           - Health check (detailed)
  POST /api/rag/index        - Index new data into the RAG store
  POST /api/admin/sync/events    - Trigger the events ingestion job
  POST /api/admin/sync/listings  - Trigger the listings ingestion job
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

# Must run before any other app import: app.config.settings reads .env via
# pydantic-settings for its own Settings fields, but that never populates
# os.environ - libraries that read env vars directly (e.g. langchain_google_genai's
# ChatGoogleGenerativeAI, which needs GOOGLE_API_KEY/GEMINI_API_KEY in os.environ)
# would otherwise fail at runtime even with a correctly filled-in .env.
load_dotenv()

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from app.api.trip import router as trip_router
from app.api.google_oauth import router as google_oauth_router
from app.rag.rag_service import rag_service
from app.scheduler import start_scheduler, stop_scheduler
from app.data import events_ingest, overpass_ingest

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Smart Tourism Assistant — AI Backend",
    description="Multi-agent travel planning system with RAG and LangGraph",
    version="2.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(trip_router)
app.include_router(google_oauth_router)


class IndexDataRequest(BaseModel):
    data: Dict[str, List[dict]]
    destination: Optional[str] = None


# ------------------- API Endpoints -------------------

@app.get("/")
async def root():
    return {"status": "ok", "service": "smart-tourism-ai-backend"}


@app.get("/api/health")
async def health_check() -> Dict[str, Any]:
    """Health check endpoint."""
    return {"status": "healthy", "version": "2.0.0"}


@app.post("/api/rag/index")
async def index_rag_data(request: IndexDataRequest):
    """
    Index candidate data into RAG store.
    Used by daily data pipeline to refresh listings.
    """
    try:
        counts = rag_service.index_candidate_data(
            request.data,
            destination=request.destination,
        )
        return {
            "status": "success",
            "indexed_counts": counts,
            "total_documents": len(rag_service.retriever._stores),
        }
    except Exception as e:
        logger.exception("RAG indexing failed: %s", str(e))
        raise HTTPException(status_code=500, detail=f"Indexing error: {str(e)}")


@app.post("/api/admin/sync/events")
async def trigger_events_sync():
    """
    Manually trigger the weekly events sync (Ticketmaster, all districts).
    Runs in a background thread and returns immediately - the sync itself takes
    several minutes across 25 districts; check server logs for completion.
    """
    asyncio.get_running_loop().run_in_executor(None, events_ingest.run_ingestion)
    return {"status": "started", "message": "Events sync started in the background."}


@app.post("/api/admin/sync/listings")
async def trigger_listings_sync():
    """
    Manually trigger the monthly hotels/restaurants/attractions + price sync (all districts).
    Runs in a background thread and returns immediately - the sync itself takes
    several minutes across 25 districts; check server logs for completion.
    """
    asyncio.get_running_loop().run_in_executor(None, overpass_ingest.run_ingestion)
    return {"status": "started", "message": "Listings sync started in the background."}


@app.on_event("startup")
async def startup_event():
    """Start the automated data-refresh scheduler on startup."""
    logger.info("Starting SmartJourney AI Backend...")
    start_scheduler()
    logger.info("Ready to serve requests")


@app.on_event("shutdown")
async def shutdown_event():
    """Stop the background scheduler cleanly."""
    stop_scheduler()


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
