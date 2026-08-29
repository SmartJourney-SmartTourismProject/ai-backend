# app/api/trip.py
import uuid
from typing import Optional

from fastapi import APIRouter, Request
from pydantic import BaseModel

from app.core.state import TripState
from app.core.orchestrator import orchestrator
from app.tools.location_tool import resolve_start_location
from app.utils.session_store import load_session, save_session

router = APIRouter(tags=["trip"])


class ClientGPS(BaseModel):
    lat: float
    lon: float


class TripPlanRequest(BaseModel):
    user_input: str
    language: str = "en"
    user_id: Optional[str] = None
    client_gps: Optional[ClientGPS] = None
    # Multi-turn conversations: omit on the first message, then pass back
    # the session_id this endpoint returned to continue modifying the same
    # trip (e.g. "make it cheaper", "swap the temple for something indoors")
    # instead of starting a brand new plan from scratch.
    session_id: Optional[str] = None


class TripPlanResponse(BaseModel):
    session_id: str
    destination: Optional[str] = None
    itinerary: list = []
    estimated_cost: Optional[float] = None
    budget_notes: Optional[str] = None
    weather: Optional[dict] = None
    disaster: Optional[dict] = None
    final_response: Optional[str] = None
    errors: list[str] = []
    completed_steps: list[str] = []


@router.post("/trip-plan", response_model=TripPlanResponse)
async def create_trip_plan(payload: TripPlanRequest, request: Request):
    """
    Runs the full Orchestrator graph for a single trip-planning turn.
    Resolves start_location here (GPS from the request body if the client
    sent it, else falling back to the request's own IP) before invoking
    the graph, since TripState itself has no client_gps/client_ip fields.

    Multi-turn: if payload.session_id matches a previous turn, that turn's
    destination/budget/interests/itinerary/etc. are loaded onto the new
    state (state.is_followup=True) before running the graph, so this
    message is treated as a modification of the existing plan rather than
    a fresh one. See app/utils/session_store.py for what's carried over.
    """
    client_gps = payload.client_gps.model_dump() if payload.client_gps else None
    client_ip = request.client.host if request.client else None

    start_location = await resolve_start_location(client_gps, client_ip)

    session_id = payload.session_id or str(uuid.uuid4())
    carried_over = load_session(session_id) if payload.session_id else None

    initial_state = TripState(
        user_input=payload.user_input,
        language=payload.language,
        session_id=session_id,
        is_followup=carried_over is not None,
        **(carried_over or {}),
    )
    # This turn's freshly-resolved values always win over carried-over ones:
    # a new user_id/GPS fix is more current than what a prior turn recorded.
    if payload.user_id:
        initial_state.user_id = payload.user_id
    if start_location:
        initial_state.start_location = start_location

    result = await orchestrator.ainvoke(initial_state)

    save_session(session_id, TripState(**result))

    return TripPlanResponse(
        session_id=session_id,
        destination=result.get("destination"),
        itinerary=result.get("itinerary", []),
        estimated_cost=result.get("estimated_cost"),
        budget_notes=result.get("budget_notes"),
        weather=result.get("weather"),
        disaster=result.get("disaster"),
        final_response=result.get("final_response"),
        errors=result.get("errors", []),
        completed_steps=result.get("completed_steps", []),
    )

# -----------------------------------------------------------------------------
# API DESIGN NOTES & BEST PRACTICES
# -----------------------------------------------------------------------------
# 1. State Encapsulation: 
# TripPlanResponse exposes only a curated subset of TripState. Internal-only 
# fields (e.g., candidate_attractions, completed_steps for debug) are hidden. 
# This ensures we never dump internal state objects straight out of the API 
# to the consumer (Flutter/Next.js frontend).
#
# 2. Client IP & Proxies (Phase 7 Deployment Note):
# `request.client.host` exposes the connecting IP. If deployed behind a reverse 
# proxy or load balancer (common on AWS), this becomes the proxy's IP. The fix 
# for production will be reading the `X-Forwarded-For` header instead to get 
# the real user's IP.
#
# 3. Request Validation:
# `client_gps` is implemented as a nested Pydantic model (ClientGPS) instead of 
# a raw dict. This provides automatic validation (FastAPI will return a 422 
# Unprocessable Entity for malformed requests instead of crashing) and ensures 
# it shows up correctly in the auto-generated Swagger /docs UI.
# -----------------------------------------------------------------------------