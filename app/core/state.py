from typing import List, Optional
from pydantic import BaseModel, Field
# pydantic Instead of state["budget"] use state.budget

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
    start_location: Optional[dict] = None       # {"lat": float, "lon": float, "source": "gps"|"ip"}
    trip_dates: Optional[List[dict]] = None     # [{"start_date": "...", "end_date": "..."}]

    # User preferences
    interests: List[str] = Field(default_factory=list)
    travel_style: Optional[str] = None

    # Retrieved data
    candidate_attractions: List[dict] = Field(default_factory=list)
    candidate_hotels: List[dict] = Field(default_factory=list)
    candidate_restaurants: List[dict] = Field(default_factory=list)
    candidate_events: List[dict] = Field(default_factory=list)

    # External context
    weather: Optional[dict] = None
    disaster: Optional[dict] = None     # {"safe": bool, "active_events": [...]}

    # AI outputs
    attractions: List[dict] = Field(default_factory=list)
    hotels: List[dict] = Field(default_factory=list)
    restaurants: List[dict] = Field(default_factory=list)
    events: List[dict] = Field(default_factory=list)
    itinerary: List[dict] = Field(default_factory=list)

    estimated_cost: Optional[float] = None

    # Final response
    final_response: Optional[str] = None

    # Errors
    errors: List[str] = Field(default_factory=list)

    # Which agents/steps have run — useful for debugging once the graph has multiple nodes
    completed_steps: List[str] = Field(default_factory=list)