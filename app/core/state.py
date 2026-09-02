from typing import Any, Dict, List, Optional
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

    # Set by app/utils/slot_filling.py (via app/core/followup.py's
    # classifier) on a follow-up turn only. "shape_only" routes the graph
    # to a deterministic targeted-day rebuild instead of the full
    # orchestrate->recommend->plan pipeline - see
    # docs/master_plan/AGENT_ARCHITECTURE.md §5.
    followup_scope: Optional[str] = None   # "full" | "shape_only" | None (not a follow-up)
    followup_target_days: Optional[List[int]] = None   # None = every day
    followup_cheaper: bool = False

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
    # Phase 5 additions (app/models/schemas.py's ExtractedSlots), carried
    # the same way interests/travel_style already are. Consumed by the
    # recommendation prompt directly and, on the fallback path, by
    # app/core/scoring.py's hard_filter() via app/core/fallback.py's
    # PlanningContext (wired in app/core/orchestrator.py's `_fallback_node`).
    must_avoid: List[str] = Field(default_factory=list)
    pace: Optional[str] = None   # "relaxed" | "balanced" | "packed"

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

    # Phase 6 (ReAct agents + graph rewrite, docs/master_plan/AGENT_ARCHITECTURE.md)
    # additions. trip_context/recommendation_output/planner_output hold each
    # ReAct agent's raw structured output (app/models/schemas.py's
    # TripContext/RecommendationOutput/PlannerOutput, as .model_dump() dicts
    # - kept as plain dicts rather than typed fields so TripState doesn't
    # take a hard import dependency on app/models/schemas.py for what's
    # otherwise a pass-through value).
    trip_context: Optional[Dict[str, Any]] = None
    recommendation_output: Optional[Dict[str, Any]] = None
    planner_output: Optional[Dict[str, Any]] = None

    # Every listing_id the recommendation agent's db_search_* tool calls
    # actually observed this request, and the full item dicts behind them -
    # what app/core/output_validator.py's L1 referential check verifies
    # against, and what the planner agent needs (lat/lon/tags/price_level)
    # since RecommendationOutput.Selection only carries an id + reason.
    candidate_listing_ids: List[str] = Field(default_factory=list)
    candidate_items: Dict[str, dict] = Field(default_factory=dict)

    # The SAME db_search_* observations as candidate_items, bucketed by
    # category ("hotel"/"restaurant"/"attraction"/"event") into the raw pool
    # shape app/core/fallback.py's build_plan() actually expects to rank
    # from scratch. Populated whenever the recommendation agent made ANY
    # successful db_search_* call - including when its own final structured
    # answer failed (Phase 8 fix): before this, a total ReAct failure left
    # this empty, so the fallback planner had nothing to build from and
    # produced an item-less plan on what TODO.md documents as the single
    # most common failure path today. Deliberately separate from
    # state.hotels/etc, which only ever hold the recommendation agent's
    # SELECTED short list (<=3 hotels, etc) - fallback needs the full pool.
    candidate_pools: Dict[str, List[dict]] = Field(default_factory=dict)

    # One repair attempt only (AGENT_ARCHITECTURE.md §1's _route_after_verify) -
    # a second validation failure goes to the deterministic fallback planner,
    # never a second repair.
    repair_attempts: int = 0
    validation_failures: List[str] = Field(default_factory=list)

    # "llm" (planner agent succeeded and passed validation, possibly after
    # one repair) or "fallback" (app/core/fallback.py's zero-LLM planner).
    plan_source: Optional[str] = None

    # Per-agent ReAct trace summary (steps_used/tools_used/stopped_by) -
    # proof ReAct is actually happening, not just declared. Full per-step
    # traces aren't persisted here (they're large and not needed by any
    # caller yet); app/core/react.py's ReActResult.trace has the detail if
    # ever needed for debugging.
    react_traces: Dict[str, Any] = Field(default_factory=dict)

    # Set when Google Calendar's stored token was revoked/expired
    # (invalid_grant) - AGENT_ARCHITECTURE.md §6's degradation matrix says
    # this must not be silent.
    calendar_reconnect_required: bool = False