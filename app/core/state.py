from typing import List, Optional
from pydantic import BaseModel, Field


class TripState(BaseModel):
    # Original user request
    user_input: str

    # Language information
    language: str = "en"

    # Multi-turn conversation support: session_id ties this request to a
    # prior trip-plan turn (see app/utils/session_store.py). is_followup is
    # set when a previous session was found and loaded, so slot_filling and
    # RecommendationAgent know to treat user_input as a MODIFICATION request
    # against the already-carried-over fields below, rather than a fresh
    # trip description.
    session_id: Optional[str] = None
    is_followup: bool = False

    # Trip details
    destination: Optional[str] = None
    duration_days: Optional[int] = None
    budget: Optional[float] = None
    travelers: Optional[int] = None
    user_id: Optional[str] = None
    start_location: Optional[dict] = None    # {"lat": float, "lon": float, "source": "gps"|"ip"|"text"}
    trip_dates: Optional[List[dict]] = None  # [{"start_date": "...", "end_date": "..."}]

    # User preferences
    interests: List[str] = Field(default_factory=list)
    travel_style: Optional[str] = None

    # Retrieved data (raw RAG/API candidates, pre-ranking)
    candidate_attractions: List[dict] = Field(default_factory=list)
    candidate_hotels: List[dict] = Field(default_factory=list)
    candidate_restaurants: List[dict] = Field(default_factory=list)
    candidate_events: List[dict] = Field(default_factory=list)

    # External context
    weather: Optional[dict] = None
    disaster: Optional[dict] = None     # {"safe": bool, "active_events": [...]}
    clarification_needed: Optional[str] = None

    # AI outputs (final, ranked selections)
    attractions: List[dict] = Field(default_factory=list)
    hotels: List[dict] = Field(default_factory=list)
    restaurants: List[dict] = Field(default_factory=list)
    events: List[dict] = Field(default_factory=list)
    itinerary: List[dict] = Field(default_factory=list)

    # Flat, category-tagged view of hotels+restaurants+attractions+events,
    # built by RecommendationAgent. Pydantic models reject attribute
    # assignment for undeclared fields, so this must be declared here --
    # `state.recommendations = [...]` would otherwise raise at runtime.
    recommendations: List[dict] = Field(default_factory=list)

    estimated_cost: Optional[float] = None
    # RecommendationAgent's explanation of budget fit (e.g. "no combination
    # of verified listings fits this budget") - surfaced by _respond_node,
    # kept separate from final_response so it doesn't get silently
    # clobbered by the generic "here's your trip plan" summary.
    budget_notes: Optional[str] = None

    # Final response
    final_response: Optional[str] = None

    # Errors
    errors: List[str] = Field(default_factory=list)

    # Which agents/steps have run — useful for debugging once the graph has multiple nodes
    completed_steps: List[str] = Field(default_factory=list)