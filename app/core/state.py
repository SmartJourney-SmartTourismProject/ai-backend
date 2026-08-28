from typing import List, Optional
from pydantic import BaseModel, Field


class TripState(BaseModel):
    # Original user request
    user_input: str

    # Language information
    language: str = "en"

    # Trip details
    destination: Optional[str] = None
    duration_days: Optional[int] = None
    budget: Optional[float] = None
    travelers: Optional[int] = None
    user_id: Optional[str] = None
    start_location: Optional[dict] = None    # {"lat": float, "lon": float, "source": "gps"|"ip"}
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

    # Final response
    final_response: Optional[str] = None

    # Errors
    errors: List[str] = Field(default_factory=list)

    # Which agents/steps have run — useful for debugging once the graph has multiple nodes
    completed_steps: List[str] = Field(default_factory=list)