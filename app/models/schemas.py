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
    exclude_categories: list[Literal["hotel", "restaurant", "attraction"]] = Field(
        default_factory=list,
        description=(
            "Kinds of place the traveler does NOT want in the itinerary at all. "
            "This is about the KIND of stop, not its subject matter - use "
            "must_avoid for subject matter ('no hiking'). "
            "Examples: 'give viewpoints only' -> ['hotel', 'restaurant']; "
            "'no restaurants' -> ['restaurant']; 'just places to see, I'll sort "
            "my own food' -> ['restaurant']. Empty list unless the traveler "
            "clearly ruled a kind of stop out."
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
    # Part 3 (itinerary-quality/token-reduction pass) - pace's 3-value enum
    # cannot express "one fewer than before", which is exactly what a
    # follow-up like "reduce the destinations per day" is asking for. These
    # two fields exist so that request has somewhere to go at all: an
    # ABSOLUTE count when the traveler states one directly ("just 2 places
    # a day"), or a RELATIVE delta when they say it comparatively
    # ("fewer"/"add one more") without a previous count to compare from in
    # this message alone - app/core/state.py's TripState.items_per_day is
    # what actually persists the resolved value across turns.
    items_per_day: Optional[int] = Field(
        None, ge=1, le=8,
        description=(
            "An EXACT number of attractions/activities per day, only if the traveler "
            "stated one directly (e.g. 'just 2 places a day', 'limit it to 3 stops "
            "daily'). Null if they spoke comparatively ('fewer', 'a bit more') instead "
            "of naming a number - use items_per_day_delta for that."
        ),
    )
    items_per_day_delta: Optional[int] = Field(
        None, ge=-3, le=3,
        description=(
            "A RELATIVE change to the number of attractions/activities per day, only "
            "if the traveler asked comparatively without naming an exact number - "
            "'fewer places each day'/'too many stops' = -1, 'a lot fewer' = -2, 'add "
            "one more stop' = +1. Null if not mentioned, or if they gave an exact "
            "number instead (use items_per_day for that)."
        ),
    )


# ─────────────────────────── orchestrator (Phase 6 target) ─────────────────


class StartLocation(BaseModel):
    lat: float
    lon: float
    source: Literal["gps", "ip", "text"]
    # Only set when the traveler named the place ("from Galle"); a GPS or IP
    # fix has coordinates but no name worth showing.
    name: Optional[str] = None


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
# listing_id/time/date/lat/lon pattern and range constraints were dropped from
# every schema below (AI_BACKEND_OPTIMIZATION_PLAN.md / itinerary quality plan,
# "A1") - Gemini's structured-output schema translation was reproducibly
# rejecting RecommendationOutput/PlannerOutput with a bare 400 INVALID_ARGUMENT
# on a fresh account/key (so not quota), and upstream reports describe the same
# failure shape for nested arrays with multiple constrained bounds. Nothing is
# lost: output_validator.py's L1 validate_referential already proves every
# listing_id came from a real tool observation (stronger than a UUID regex),
# and geo_in_country/dates_in_window (L2) already enforce the Sri Lanka bbox
# and real trip dates - constraints belong there now, not in the schema that
# has to survive provider-side structured decoding.


class Selection(BaseModel):
    listing_id: str
    category: Literal["hotel", "restaurant", "attraction", "event"]
    rank: int
    score: float
    reason: str = Field(max_length=200)


class DroppedItem(BaseModel):
    listing_id: str
    reason_code: Literal["closed_on_trip_dates", "violates_must_avoid", "duplicate_of", "unsafe_area"]


class RecommendationOutput(BaseModel):
    """docs/master_plan/DETERMINISM_AND_VALIDATION.md §3's worked-example
    rules apply to this schema: every listing_id must have come from a
    db_search_* observation, ordering must be score_candidates' own, and
    `reason` may only quote the score breakdown, never invent a number."""

    hotels: list[Selection] = Field(default_factory=list)
    restaurants: list[Selection] = Field(default_factory=list)
    attractions: list[Selection] = Field(default_factory=list)
    events: list[Selection] = Field(default_factory=list)
    dropped: list[DroppedItem] = Field(default_factory=list)
    coverage_notes: list[str] = Field(default_factory=list)


# ─────────────────────────── planner (Phase 6 target) ──────────────────────


class ItineraryItem(BaseModel):
    time: str
    end_time: str
    type: Literal["hotel", "restaurant", "attraction", "event", "travel"]
    listing_id: Optional[str] = None   # None only for type="travel"
    name: str
    lat: float                                # bounds enforced by L2 geo_in_country, not the schema
    lon: float
    est_cost: float
    currency: Literal["LKR"] = "LKR"
    notes: str = Field(default="", max_length=200)


class ItineraryDay(BaseModel):
    day: int
    date: str
    theme: str = Field(default="", max_length=60)
    items: list[ItineraryItem] = Field(default_factory=list)
    day_cost: float


class PlannerOutput(BaseModel):
    itinerary: list[ItineraryDay] = Field(default_factory=list)
    estimated_cost: float
    currency: Literal["LKR"] = "LKR"
    budget_notes: Optional[str] = Field(None, max_length=500)
    # plan_source was a Literal["llm"]="llm" field here - live-found
    # (itinerary-quality/token-reduction pass) that it broke structured
    # output on almost every real run: `output.plan_source` is never read
    # anywhere (grep confirmed) - app/agents/planner_agent.py always
    # hardcodes state.plan_source = "llm" itself, exactly like the fallback
    # planner hardcodes "fallback" (see fallback.py's FallbackPlanResult,
    # which never asked an LLM for this at all). But the model consistently
    # wrote something else instead of the fixed literal - "tool_observations",
    # "tool_plan" - both observed live, both a bare pydantic
    # literal_error that failed the ENTIRE structured-output call (and then
    # the one repair attempt too, on the same field) for a field nothing
    # downstream ever consumed. Removed entirely rather than "fixed the
    # prompt" - there was never a reason to ask the model for a constant.


# ─────────────────────────── repair (Phase 6 target) ───────────────────────


class RepairedPlannerOutput(PlannerOutput):
    """Identical shape to PlannerOutput - the repair call returns the same
    schema, just with L0-L2's failures corrected. A distinct class only so
    call sites are explicit about which pass produced a given object."""
