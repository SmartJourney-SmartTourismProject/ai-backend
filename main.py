"""
FastAPI server for SmartJourney AI Backend.
Provides REST API endpoints for trip planning with multi-agent system.

Endpoints:
  POST /api/plan-trip - Main endpoint for trip planning
  GET  /api/health  - Health check
  POST /api/rag/index - Index new data into RAG store
  GET  /api/agents - List available agents

NOTE: this file previously called a `get_orchestrator_graph()` built out of
this branch's own `app/workflows/orchestrator.py` (plus a `policy_agent.py`,
`calendar_agent.py`, and `context_agent.py`). Those were all a duplicate,
independent re-implementation of Member A's Orchestrator track (router agent,
policy guard, location/calendar/weather/disaster tools) - see
CLEANUP_PLAN.md and MEMBER_A_INTEGRATION_TODO.md for why they were removed.

Until Member A's real `app/core/orchestrator.py` (plus her `app/tools/*` and
`app/utils/*`) lands on this branch, `/api/plan-trip` below calls
RecommendationAgent directly - it curates candidates AND builds the
itinerary in one LLM call (see recommendation_agent.py) - skipping the
validate/policy/location/calendar/weather/disaster steps that are Member A's
job. Caller must supply `destination` (and may supply `weather`/`disaster`)
directly since nothing here resolves them. Swap this for a call into her
orchestrator once it's merged; see MEMBER_A_INTEGRATION_TODO.md.
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
from pydantic import BaseModel, Field

from app.core.state import TripState
from app.workflows.recommendation_agent import RecommendationAgent
from app.rag.rag_service import rag_service
from app.scheduler import start_scheduler, stop_scheduler
from app.data import events_ingest, overpass_ingest

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize FastAPI app
app = FastAPI(
    title="SmartJourney AI Backend",
    description="Multi-agent travel planning system with RAG and LangGraph",
    version="2.0.0",
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ------------------- Request/Response Models -------------------

class PlanTripRequest(BaseModel):
    user_input: str
    destination: Optional[str] = None
    duration_days: Optional[int] = Field(default=None, ge=1, le=180)
    budget: Optional[float] = Field(default=None, ge=0)
    travelers: Optional[int] = Field(default=None, ge=1, le=50)
    user_id: Optional[str] = None
    interests: List[str] = Field(default_factory=list)
    travel_style: Optional[str] = None
    start_location: Optional[dict] = None
    trip_dates: Optional[List[dict]] = None
    language: str = "en"


class PlanTripResponse(BaseModel):
    success: bool
    final_response: Optional[str] = None
    errors: List[str] = Field(default_factory=list)
    completed_steps: List[str] = Field(default_factory=list)
    itinerary: List[dict] = Field(default_factory=list)
    recommendations: List[dict] = Field(default_factory=list)
    weather: Optional[dict] = None
    disaster: Optional[dict] = None
    estimated_cost: Optional[float] = None


class IndexDataRequest(BaseModel):
    data: Dict[str, List[dict]]
    destination: Optional[str] = None


class HealthResponse(BaseModel):
    status: str
    version: str
    agents_available: List[str]


# ------------------- API Endpoints -------------------

@app.get("/api/health", response_model=HealthResponse)
async def health_check() -> HealthResponse:
    """Health check endpoint."""
    return HealthResponse(
        status="healthy",
        version="2.0.0",
        agents_available=[
            "recommendation_agent",  # also builds the itinerary - see recommendation_agent.py
            "planning_agent",  # standalone re-planning, not on this endpoint's path
        ],
    )


@app.post("/api/plan-trip", response_model=PlanTripResponse)
async def plan_trip(request: PlanTripRequest) -> PlanTripResponse:
    """
    Trip planning endpoint - Member B's scope only (see module docstring).

    Calls RecommendationAgent directly: curates candidates from db_tool/RAG
    and builds the itinerary in one LLM call. `request.destination` is
    required - there is no policy/location/calendar/weather/disaster
    resolution here, that's Member A's Orchestrator track. Pass `weather`/
    `disaster` in the request body if you want them considered.
    """
    try:
        state = TripState(**request.dict())

        if not state.destination:
            raise HTTPException(
                status_code=422,
                detail="destination is required (no Orchestrator on this branch to resolve/ask for it)",
            )

        result = await RecommendationAgent().execute(state)

        return PlanTripResponse(
            success=result.success,
            final_response=state.final_response,
            errors=state.errors,
            completed_steps=state.completed_steps,
            itinerary=state.itinerary,
            recommendations=state.recommendations,
            weather=state.weather,
            disaster=state.disaster,
            estimated_cost=state.estimated_cost,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Trip planning failed: %s", str(e))
        raise HTTPException(status_code=500, detail=f"Internal error: {str(e)}")


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


@app.get("/api/agents")
async def list_agents() -> Dict[str, Any]:
    """List available agents and their status."""
    return {
        "agents": [
            {
                "name": "recommendation_agent",
                "description": "RAG-based listing curation + itinerary assembly with budget constraints (one combined LLM call)",
                "status": "active",
            },
            {
                "name": "planning_agent",
                "description": "Standalone itinerary re-planning from existing recommendations - not called by the default trip-plan endpoint (merged into recommendation_agent to save an LLM call per request)",
                "status": "standalone",
            },
        ],
        "orchestrator": "none on this branch - policy/location/calendar/weather/disaster routing is Member A's Orchestrator track, see MEMBER_A_INTEGRATION_TODO.md",
    }


@app.post("/api/admin/sync/events")
async def trigger_events_sync():
    """
    Manually trigger the weekly events sync (Ticketmaster/Eventbrite, all districts).
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
    """Initialize the automated data-refresh scheduler on startup."""
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
