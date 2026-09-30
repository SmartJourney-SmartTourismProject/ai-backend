# app/api/trip.py
import hashlib
import uuid
from typing import Optional

from fastapi import APIRouter, Request
from pydantic import BaseModel

from app.config.settings import settings
from app.core.state import TripState
from app.core.orchestrator import orchestrator
from app.tools.db_tool import get_data_freshness
from app.tools.location_tool import resolve_start_location
from app.utils.cache import cache_get, cache_set
from app.utils.session_store import load_session, save_session

router = APIRouter(tags=["trip"])


def _estimated_cost(result: dict) -> Optional[float]:
    """The plan's total, falling back to the sum of its day costs.

    `itinerary` is carried across turns (session_store) but `estimated_cost`
    is not, so a turn that re-shows the existing plan without rebuilding it
    (a weather or budget question) came back with its per-day costs and a
    null total - and the chat's Save stored the trip with no cost, so Saved
    Itineraries and the Budget tracker read 0.
    """
    if result.get("estimated_cost") is not None:
        return result["estimated_cost"]
    itinerary = result.get("itinerary") or []
    if not any(day.get("items") for day in itinerary):
        return None
    return round(sum(float(day.get("day_cost") or 0) for day in itinerary), 2)


def _trip_plan_cache_key(message: str, language: str, user_id: Optional[str]) -> str:
    """C3 (AI_BACKEND_OPTIMIZATION_PLAN.md): PROJECT_MASTER_PLAN.md §D6c has
    always claimed identical /trip-plan requests are cached; nothing ever
    implemented it. Keyed on the normalized message text (not on a
    session_id, which only exists on a FOLLOW-UP turn) - only applies to
    fresh, stateless requests, since a follow-up is by definition a
    modification of already-carried state and has no meaningful "identical
    request" to hit. Case/whitespace-insensitive so trivial phrasing
    differences ("Plan a trip to Ella" vs "plan a trip to ella ") still hit
    the same key - the LLM path is what's expensive here, not string
    matching precision."""
    raw = f"{message.strip().lower()}|{language}|{user_id or ''}"
    return f"trip_plan:{hashlib.sha256(raw.encode()).hexdigest()}"


class ClientGPS(BaseModel):
    lat: float
    lon: float


class TripPlanRequest(BaseModel):
    # Named "message" (not "user_input") to match BUILD_PLAN.md §7's API
    # contract - internally this still becomes TripState.user_input,
    # since that name is used throughout the Orchestrator/agents and
    # renaming it there would ripple through every file that reads it.
    message: str
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
    currency: str = "LKR"
    budget_notes: Optional[str] = None
    # "llm" (the planner ReAct agent produced this, possibly after one
    # repair) or "fallback" (app/core/fallback.py's deterministic planner -
    # see PROJECT_MASTER_PLAN.md's Phase 6 writeup for why this is common
    # today, not a bug). None only when no plan was built at all (e.g. a
    # clarification response).
    plan_source: Optional[str] = None
    # ISO timestamp of the oldest successful sync among enabled data
    # sources (app/tools/db_tool.py's get_data_freshness) - None if unknown
    # or any enabled source has never synced. Informational only; never
    # blocks or changes the plan itself.
    data_freshness: Optional[str] = None
    weather: Optional[dict] = None
    disaster: Optional[dict] = None
    # Where the trip departs from, when it is known - {lat, lon, source, name}.
    # The itinerary lists stops at the DESTINATION only, so without this the
    # client has no way to draw "Galle to Kandy" as anything but Kandy: the
    # origin is never a stop, and the map had nothing else to plot.
    start_location: Optional[dict] = None
    final_response: Optional[str] = None
    # RAG Q&A citations (app/rag/, app/core/orchestrator.py's _answer_node) -
    # [{"title", "url", "section", "license"}] for whatever _answer_node
    # actually cited. Empty unless this turn's intent was "question" or
    # "both"; final_response already reads fine on its own (the answer
    # text includes inline [N] markers), this is what the client turns
    # into clickable source links under it.
    sources: list[dict] = []
    errors: list[str] = []
    # Debug-only, per §7 - never populated unless settings.debug=True.
    # Renamed from completed_steps (Phase 7): now carries both the step
    # sequence and each ReAct agent's trace summary, not just step names.
    trace: dict = {}


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

    C3: a fresh (non-follow-up) request identical to one seen recently is
    served from cache instead of re-running the graph - see
    _trip_plan_cache_key's docstring. Each cache hit still gets its own new
    session_id and session row, so follow-ups behave exactly as if the
    graph had actually run.
    """
    is_fresh_request = payload.session_id is None
    cache_key = _trip_plan_cache_key(payload.message, payload.language, payload.user_id) if is_fresh_request else None
    cached_result = await cache_get(cache_key) if cache_key else None

    if cached_result is not None:
        result = cached_result
        session_id = str(uuid.uuid4())
    else:
        client_gps = payload.client_gps.model_dump() if payload.client_gps else None
        client_ip = request.client.host if request.client else None

        start_location = await resolve_start_location(client_gps, client_ip)

        session_id = payload.session_id or str(uuid.uuid4())
        carried_over = await load_session(session_id) if payload.session_id else None

        initial_state = TripState(
            user_input=payload.message,
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

        # Only cache a real LLM plan. Caching a fallback result meant a
        # repeated identical prompt kept replaying that same fallback for a
        # full cache_ttl even after the LLM path itself started working
        # again - live-found (fallback investigation, 2026-09-25) to be the
        # single biggest reason later "fallback" responses looked
        # unchanged. A fallback plan is fast to build fresh (no LLM call),
        # so there's no real caching benefit to it anyway.
        if cache_key and not result.get("clarification_needed") and result.get("plan_source") == "llm":
            await cache_set(cache_key, result, settings.cache_ttl)

    await save_session(session_id, TripState(**result))

    trace = {}
    if settings.debug:
        trace = {
            "completed_steps": result.get("completed_steps", []),
            "react_traces": result.get("react_traces", {}),
        }

    return TripPlanResponse(
        session_id=session_id,
        destination=result.get("destination"),
        itinerary=result.get("itinerary", []),
        estimated_cost=_estimated_cost(result),
        currency="LKR",
        budget_notes=result.get("budget_notes"),
        plan_source=result.get("plan_source"),
        data_freshness=await get_data_freshness(),
        weather=result.get("weather"),
        disaster=result.get("disaster"),
        start_location=result.get("start_location"),
        final_response=result.get("final_response"),
        sources=result.get("sources", []),
        errors=result.get("errors", []),
        trace=trace,
    )

# -----------------------------------------------------------------------------
# API DESIGN NOTES & BEST PRACTICES
# -----------------------------------------------------------------------------
# 1. State Encapsulation:
# TripPlanResponse exposes only a curated subset of TripState. Internal-only
# fields (e.g., candidate_attractions) are never exposed at all; `trace`
# is a debug field only populated when settings.debug=True (see below). This
# ensures we never dump internal state objects straight out of the API
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