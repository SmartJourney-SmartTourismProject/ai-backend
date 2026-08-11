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

    # User preferences
    interests: List[str] = Field(default_factory=list)
    travel_style: Optional[str] = None

    # Retrieved data
    attractions: List[dict] = Field(default_factory=list)
    hotels: List[dict] = Field(default_factory=list)
    restaurants: List[dict] = Field(default_factory=list)
    events: List[dict] = Field(default_factory=list)

    # External context
    weather: Optional[dict] = None
    # Missing Phase 1 fields
    disaster: dict | None = None
    events: list[dict] | None = None

    # AI outputs
    recommendations: List[dict] = Field(default_factory=list)
    itinerary: List[dict] = Field(default_factory=list)

    estimated_cost: Optional[float] = None

    # Final response
    final_response: Optional[str] = None

    # Errors
    errors: List[str] = Field(default_factory=list)