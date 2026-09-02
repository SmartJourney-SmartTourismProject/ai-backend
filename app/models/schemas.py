"""
Every LLM output model in the system, in one place
(docs/master_plan/DETERMINISM_AND_VALIDATION.md §4, AGENT_ARCHITECTURE.md §3).
Field-level constraints (patterns, bounds, lengths) turn a whole class of L2
validation checks into free ones - the schema is enforced, the prompt is
merely read.

Every LLM call site must go through with_structured_output(<model from here>),
never a raw .ainvoke() + hand-rolled JSON parsing - see
app/core/output_validator.py's module docstring for why the old
_parse_json_response pattern this replaces was a real, silent failure path.

Every model below is live-wired as of Phase 6: ExtractedSlots by
app/utils/slot_filling.py, TripContext/RecommendationOutput/PlannerOutput by
the three app/agents/ ReAct agents, RepairedPlannerOutput by
app/core/orchestrator.py's `_repair_node`.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

# ─────────────────────────── slot filling (live) ───────────────────────────


class ExtractedSlots(BaseModel):
    """What slot_filling.py asks the LLM to extract. Every field is
    Optional - the model must leave anything not mentioned in user_input as
    null, never guess. This is the schema class; app/utils/slot_filling.py
    imports it from here rather than defining its own copy."""

    destination: Optional[str] = Field(
        None, description="The travel destination, if mentioned. Null if not mentioned."
    )
    duration_days: Optional[int] = Field(
        None, ge=1, le=30,
        description="Trip length in days, if mentioned or inferable from phrases like 'a week' (=7). Null if not mentioned.",
    )
    budget: Optional[float] = Field(
        None, gt=0, le=100_000_000,
        description="Total trip budget as a number, if mentioned. Null if not mentioned.",
    )
    travelers: Optional[int] = Field(
        None, ge=1, le=20,
        description="Total number of travelers including the user, if mentioned or inferable (e.g. 'my wife and kid' = 3). Null if not mentioned.",
    )
    interests: list[str] = Field(
        default_factory=list,
        description=(
            "List of travel interests/activity types mentioned, as short, "
            "singular, lowercase tags (e.g. 'beach' not 'beaches', 'hike' not "
            "'hiking trips'). Empty list if none mentioned."
        ),
    )
    origin_location: Optional[str] = Field(
        None, description=(
            "The place the traveler says they are starting/departing FROM, "
            "if explicitly mentioned (e.g. 'I'm starting from Polonnaruwa', "
            "'coming from Colombo', 'leaving from the airport'). This is the "
            "traveler's ORIGIN, not their destination - never confuse the "
            "two, and never guess this from the destination alone. Null if "
            "no starting location was mentioned."
        )
    )
    must_avoid: list[str] = Field(
        default_factory=list,
        description=(
            "Things the traveler explicitly wants to avoid, as short lowercase "
            "tags matching the interest tag style (e.g. 'no hiking, my knees are "
            "bad' -> ['hike']). Empty list if nothing was mentioned to avoid."
        ),
    )
    pace: Optional[Literal["relaxed", "balanced", "packed"]] = Field(
        None, description=(
            "How busy the traveler wants each day to be, only if they said "
            "something indicating pace ('relaxed', 'take it easy', 'packed "
            "schedule', 'see as much as possible'). Null if not indicated - "
            "do not default to 'balanced' just because none was mentioned."
        )
    )


# ─────────────────────────── orchestrator (Phase 6 target) ─────────────────


class StartLocation(BaseModel):
    lat: float
    lon: float
    source: Literal["gps", "ip", "text"]


class DateWindow(BaseModel):
    start_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    end_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    source: Literal["calendar", "user", "default"]
    dates: list[str] = Field(default_factory=list)   # every individual date in the window, ISO strings


class DayWeather(BaseModel):
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    temp_min: float
    temp_max: float
    condition: str
    rain_probability: float = Field(ge=0.0, le=1.0)


class DisasterEvent(BaseModel):
    type: str
    severity: Literal["red", "orange", "green"]
    title: str
    source: str
    distance_km: Optional[float] = None


class DisasterSummary(BaseModel):
    safe: bool
    max_severity: Optional[Literal["red", "orange", "green"]] = None
    active_events: list[DisasterEvent] = Field(default_factory=list)
    note: Optional[str] = None


class TripContext(BaseModel):
    """Orchestrator agent's output (AGENT_ARCHITECTURE.md §3.2) - a fully
    grounded context the recommendation/planner agents build on."""

    destination_name: str
    district_id: str
    lat: float
    lon: float
    start_location: Optional[StartLocation] = None
    date_window: DateWindow
    per_day_weather: list[DayWeather] = Field(default_factory=list)
    disaster: DisasterSummary
    safety_notes: list[str] = Field(default_factory=list)
    context_confidence: Literal["high", "medium", "low"]


# ─────────────────────────── recommendation (Phase 6 target) ───────────────

_UUID_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"


class Selection(BaseModel):
    listing_id: str = Field(pattern=_UUID_PATTERN)
    category: Literal["hotel", "restaurant", "attraction", "event"]
    rank: int = Field(ge=1, le=50)
    score: float = Field(ge=0.0, le=1.0)
    reason: str = Field(max_length=200)


class DroppedItem(BaseModel):
    listing_id: str = Field(pattern=_UUID_PATTERN)
    reason_code: Literal["closed_on_trip_dates", "violates_must_avoid", "duplicate_of", "unsafe_area"]


class RecommendationOutput(BaseModel):
    """docs/master_plan/DETERMINISM_AND_VALIDATION.md §3's worked-example
    rules apply to this schema: every listing_id must have come from a
    db_search_* observation, ordering must be score_candidates' own, and
    `reason` may only quote the score breakdown, never invent a number."""

    hotels: list[Selection] = Field(max_length=3)
    restaurants: list[Selection] = Field(max_length=30)     # capped generously; real cap (2 x duration_days) is a business rule, not a fixed schema bound
    attractions: list[Selection] = Field(max_length=45)      # 3 x duration_days at the 30-day max
    events: list[Selection] = Field(max_length=5)
    dropped: list[DroppedItem] = Field(default_factory=list)
    coverage_notes: list[str] = Field(default_factory=list)


# ─────────────────────────── planner (Phase 6 target) ──────────────────────


class ItineraryItem(BaseModel):
    time: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    end_time: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    type: Literal["hotel", "restaurant", "attraction", "event", "travel"]
    listing_id: Optional[str] = Field(None, pattern=_UUID_PATTERN)   # None only for type="travel"
    name: str
    lat: float = Field(ge=5.85, le=9.95)      # Sri Lanka's real latitude bounds - see L2 geo_in_country
    lon: float = Field(ge=79.5, le=82.0)      # Sri Lanka's real longitude bounds
    est_cost: float = Field(ge=0.0)
    currency: Literal["LKR"] = "LKR"
    notes: str = Field(default="", max_length=200)


class ItineraryDay(BaseModel):
    day: int = Field(ge=1, le=30)
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    theme: str = Field(default="", max_length=60)
    items: list[ItineraryItem] = Field(min_length=1, max_length=12)
    day_cost: float = Field(ge=0.0)


class PlannerOutput(BaseModel):
    itinerary: list[ItineraryDay] = Field(min_length=1, max_length=30)
    estimated_cost: float = Field(ge=0.0)
    currency: Literal["LKR"] = "LKR"
    budget_notes: Optional[str] = Field(None, max_length=500)
    plan_source: Literal["llm"] = "llm"   # the fallback planner sets "fallback" itself, never via this schema


# ─────────────────────────── repair (Phase 6 target) ───────────────────────


class RepairedPlannerOutput(PlannerOutput):
    """Identical shape to PlannerOutput - the repair call returns the same
    schema, just with L0-L2's failures corrected. A distinct class only so
    call sites are explicit about which pass produced a given object."""
