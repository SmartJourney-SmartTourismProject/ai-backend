"""
FastAPI server for SmartJourney AI Backend.
Provides REST API endpoints for trip planning with multi-agent system.

Endpoints:
  POST /api/plan-trip - Main endpoint for trip planning
  GET  /api/health  - Health check
  POST /api/rag/index - Index new data into RAG store
  GET  /api/agents - List available agents
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from app.core.state import TripState
from app.workflows.orchestrator import get_orchestrator_graph
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

# Global orchestrator graph instance
_orchestrator = None


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


# ------------------- Helper Functions -------------------

def get_orchestrator():
    """Get or create the orchestrator graph instance."""
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = get_orchestrator_graph()
        logger.info("Orchestrator graph initialized")
    return _orchestrator


# ------------------- API Endpoints -------------------

@app.get("/api/health", response_model=HealthResponse)
async def health_check() -> HealthResponse:
    """Health check endpoint."""
    return HealthResponse(
        status="healthy" if _orchestrator is not None else "initializing",
        version="2.0.0",
        agents_available=[
            "policy_agent",
            "location_resolver",
            "calendar_agent",
            "weather_agent",
            "disaster_agent",
            "recommendation_agent",
            "planning_agent",
        ],
    )


@app.post("/api/plan-trip", response_model=PlanTripResponse)
async def plan_trip(request: PlanTripRequest) -> PlanTripResponse:
    """
    Main trip planning endpoint.
    
    Processes user request through multi-agent orchestration:
    validate → policy → location → calendar → weather/disaster → 
    recommendation → planning → finalize
    """
    try:
        # Create TripState
        state = TripState(**request.dict())
        
        # Get orchestrator graph
        graph = get_orchestrator()
        
        # Run the graph
        result = await graph.ainvoke(state)
        
        # Extract final state from result
        # LangGraph returns the final state object
        final_state = result if isinstance(result, TripState) else state
        
        return PlanTripResponse(
            success=len(final_state.errors) == 0,
            final_response=final_state.final_response,
            errors=final_state.errors,
            completed_steps=final_state.completed_steps,
            itinerary=final_state.itinerary,
            recommendations=final_state.recommendations,
            weather=final_state.weather,
            disaster=final_state.disaster,
            estimated_cost=final_state.estimated_cost,
        )
    
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
                "name": "policy_agent",
                "description": "Policy guardrails and validation",
                "status": "active",
            },
            {
                "name": "weather_agent",
                "description": "Weather forecast fetching (OpenWeather)",
                "status": "active",
            },
            {
                "name": "disaster_agent",
                "description": "Disaster alert monitoring (EONET/USGS/GDACS)",
                "status": "active",
            },
            {
                "name": "recommendation_agent",
                "description": "RAG-based listing curation",
                "status": "active",
            },
            {
                "name": "planning_agent",
                "description": "Itinerary assembly with budget constraints",
                "status": "active",
            },
            {
                "name": "calendar_agent",
                "description": "Google Calendar integration for free dates",
                "status": "active",
            },
        ],
        "orchestrator": "LangGraph StateGraph",
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
    """Initialize orchestrator and the automated data-refresh scheduler on startup."""
    logger.info("Starting SmartJourney AI Backend...")
    get_orchestrator()
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
