# Agent Architecture — ReAct, Tools, and the Graph

Companion to [`PROJECT_MASTER_PLAN.md`](PROJECT_MASTER_PLAN.md). Covers **how the agents work**.
The maths they must not do themselves is in [`DETERMINISM_AND_VALIDATION.md`](DETERMINISM_AND_VALIDATION.md).

---

## 1. The graph

Replaces the current linear chain in `app/core/orchestrator.py`.

```python
# app/core/orchestrator.py  (target shape)

graph.add_node("validate",       _validate_node)        # pure Python
graph.add_node("policy",         _policy_node)          # pure Python
graph.add_node("slot_fill",      _slot_fill_node)       # LLM #1, structured
graph.add_node("orchestrate",    _orchestrator_react)   # LLM #2, ReAct
graph.add_node("recommend",      _recommendation_react) # LLM #3, ReAct
graph.add_node("plan",           _planner_react)        # LLM #4, ReAct
graph.add_node("verify",         _verify_node)          # pure Python (L0/L1/L2)
graph.add_node("repair",         _repair_node)          # re-enters planner once
graph.add_node("fallback",       _fallback_node)        # pure Python, no LLM
graph.add_node("respond",        _respond_node)         # LLM #5 optional + template

graph.set_entry_point("validate")

graph.add_conditional_edges("validate",  lambda s: "policy"      if not s.errors else "respond")
graph.add_conditional_edges("policy",    lambda s: "slot_fill"   if not s.errors else "respond")
graph.add_conditional_edges("slot_fill", lambda s: "respond"     if s.clarification_needed else "orchestrate")
graph.add_edge("orchestrate", "recommend")
graph.add_conditional_edges("recommend", lambda s: "plan" if s.ranked else "fallback")
graph.add_edge("plan", "verify")
graph.add_conditional_edges("verify", _route_after_verify)   # → respond | repair | fallback
graph.add_edge("repair",   "verify")
graph.add_edge("fallback", "verify")
graph.add_edge("respond",  END)
```

```python
def _route_after_verify(state: TripState) -> str:
    if state.validation.ok:
        return "respond"
    if state.repair_attempts == 0:
        return "repair"      # exactly one LLM repair attempt
    return "fallback"        # deterministic plan, always valid by construction
```

`fallback` → `verify` is not a loop risk: the fallback planner constructs its output from validated
DB rows through deterministic code, so L0–L2 pass by construction. If they ever don't, that's a bug
in `app/core/fallback.py` and should fail loudly in tests, not spin.

**Node → step mapping against your workflow:**

| Your step | Node(s) |
|---|---|
| user_input | API layer, `app/api/trip.py` |
| validator + policy check | `validate`, `policy` |
| slot filling | `slot_fill` |
| orchestral agent + all tools | `orchestrate` |
| recommendation agent | `recommend` |
| planner agent | `plan` |
| orchestral agent + validate result | `verify` (+ `repair` / `fallback`) |
| user | `respond` |

---

## 2. What "ReAct" means here

### 2.1 The loop

```
  ┌──────────────────────────────────────────────────────────┐
  │  agent node                                              │
  │                                                          │
  │   messages ──► LLM (tools bound) ──► response            │
  │                                        │                 │
  │                     ┌──────────────────┴─────────┐       │
  │              has tool_calls?                  no │       │
  │                     │ yes                        ▼       │
  │                     ▼                    structured      │
  │            execute tools in parallel      final answer   │
  │            (asyncio.gather)                    │         │
  │                     │                          ▼         │
  │            append observations             return        │
  │                     │                                    │
  │                     └───► loop (step += 1)               │
  └──────────────────────────────────────────────────────────┘
              bounded by: max_steps, tool_budget, wall_clock
```

Shared implementation, `app/core/react.py`:

```python
@dataclass
class ReActConfig:
    max_steps: int = 6            # LLM turns, not tool calls
    tool_budget: int = 12         # total tool executions
    wall_clock_s: float = 25.0
    per_tool_timeout_s: float = 8.0

@dataclass
class ReActResult:
    output: BaseModel             # the agent's structured final answer
    trace: list[TraceStep]        # persisted to ai_session.react_trace
    steps_used: int
    tools_used: list[str]
    stopped_by: Literal["answer", "max_steps", "tool_budget", "timeout", "error"]

async def run_react(
    llm, tools: list[StructuredTool], messages: list, output_schema: type[BaseModel],
    config: ReActConfig,
) -> ReActResult: ...
```

Rules the executor enforces:
- **Tool results are facts, not suggestions.** Observations are appended verbatim as `ToolMessage`s. The LLM may not restate them with different values — L1 validation catches it when it tries.
- **Parallel where independent.** `get_weather` + `get_disaster_info` in the same step run under one `asyncio.gather` (the current code already does this correctly; keep that behaviour).
- **Idempotent + cached tools.** A repeated call with identical args returns the cached observation and does **not** consume tool budget. Prevents the classic ReAct thrash loop.
- **Every exit is a valid state.** Hitting `max_steps` or `timeout` doesn't raise — the agent is asked once, without tools, to produce its structured answer from what it already observed. If that fails too, the node returns partial state and the graph degrades.
- **Trace is always captured**, even on success. Cheap, and it's what proves ReAct is happening (Phase 6 gate).

### 2.2 Why this doesn't destroy determinism

The reasonable objection to ReAct in a system that wants reproducibility: *the LLM now chooses the control flow, so runs can diverge*. Three properties keep that bounded:

1. **The decision space is small and typed.** The orchestrator has 6 tools, each with a narrow schema. At `temperature=0` with the same state, it takes the same path — and where it doesn't, the paths converge because...
2. **The tools are pure functions of the DB + cached external state.** Weather is cached 45 min, disasters 1 h, geocoding is cached permanently in `geo_resolution`, listings come from a table that changes nightly. Same input → same observations.
3. **Divergence in tool *order* cannot change the *result*,** because the result is computed by `score_candidates` / `build_day_plan` from the union of observations, not from the sequence of them.

So: the *path* is bounded-nondeterministic, the *output* is deterministic. That's the trade you're making, and it's the right one — it's what buys you retry-on-failure, gap-filling, and graceful handling of "geocoding returned nothing" without a hand-written branch for every case.

---

## 3. The agents

All three subclass `BaseAgent` (keep the existing ABC) and return `AgentResult`.

### 3.1 Slot filler *(not a ReAct agent — one structured call)*

Keep today's behaviour, with three changes: prompt moves to `app/prompts/slot_filling_prompt.py`,
`temperature=0` (already), and the extracted model gains `origin_place`, `date_hints`,
`must_avoid: list[str]`, `pace: Literal["relaxed","balanced","packed"] | None`.

`must_avoid` and `pace` currently reach the LLM only implicitly through `traveler_request`
passthrough. Making them explicit fields lets the **deterministic** layer act on them
(`must_avoid` becomes a hard filter in scoring; `pace` sets items-per-day) instead of hoping the
planner LLM noticed.

### 3.2 Orchestrator agent — ReAct

**Job:** turn a half-specified request into a fully-grounded `TripContext`. Nothing else.

**Tools:** `resolve_place`, `resolve_district`, `resolve_start_location`, `get_calendar_free_days`, `get_weather`, `get_disaster_info`.

**The reasoning it's actually there to do** — this is why a linear chain isn't enough:
- `resolve_place("Ella")` returns coordinates → it must then call `resolve_district(lat, lon)` to get the district id everything else keys on. A chain can't express "call B with A's output, but only if A succeeded."
- If `resolve_place` finds nothing, try the raw user text, then the origin, then ask for clarification — instead of silently producing a plan with no coordinates (today's behaviour: `_context_node` returns early and weather/disaster are simply never fetched, with no error recorded).
- If the user has a calendar, free days determine which dates to fetch weather for. If not, today+duration. Today's code does this with an `if`; with ReAct the agent also handles "calendar returned 30 free days" (pick the soonest window matching `duration_days`) and "calendar returned free days that are all 3 months out" (prefer the nearest workable window and say so).
- If disaster severity is red at the destination, re-check a nearby alternative before the recommendation agent wastes a call on an unsafe district.

**Output schema:**

```python
class TripContext(BaseModel):
    destination_name: str
    district_id: str
    lat: float
    lon: float
    start_location: StartLocation | None
    date_window: DateWindow            # {start_date, end_date, source: "calendar"|"user"|"default"}
    per_day_weather: list[DayWeather]  # one per trip day; [] if unavailable
    disaster: DisasterSummary          # {safe, max_severity, active_events[], note?}
    safety_notes: list[str]
    context_confidence: Literal["high", "medium", "low"]
```

`ReActConfig(max_steps=6, tool_budget=12)`.

### 3.3 Recommendation agent — ReAct

**Job:** decide *what to query*, then explain *what the scorer chose*. It never orders anything.

**Tools:** `db_search_listings`, `db_search_events`, `travel_matrix`, `score_candidates`.

**Loop, in practice:**

```
Thought:  Traveler wants culture + food in Kandy, 3 days, LKR 60k, from Colombo.
Action:   db_search_listings(district_id=…, category="attraction", tags=["culture","history"], limit=40)
Obs:      31 listings
Thought:  Need hotels near the attraction cluster, and restaurants.
Action:   db_search_listings(category="hotel", limit=25) ∥ db_search_listings(category="restaurant", tags=["local"], limit=30)
Obs:      25 / 28
Thought:  Only 4 attractions have ratings — widen tags to include "temple","museum".
Action:   db_search_listings(category="attraction", tags=[…], limit=40)
Obs:      44 listings
Thought:  Enough. Rank them.
Action:   score_candidates(candidates=…, interests=[…], anchor={lat,lon}, budget_per_day=…, category=…)
Obs:      ranked list with per-factor score breakdowns
Answer:   RecommendationOutput(selections=[…], reasons={id: "…"}, coverage_notes=[…])
```

**Hard constraints in the prompt** (see `DETERMINISM_AND_VALIDATION.md §3`):
1. Only ids present in a `db_search_*` observation may appear in the output.
2. You may not reorder the output of `score_candidates`. Take the top N in the order given.
3. You may only *drop* a scored item, and only for a stated reason from a fixed list (`closed_on_trip_dates`, `violates_must_avoid`, `duplicate_of`, `unsafe_area`) — and you must then take the next item down.
4. `reason` is free text about *why it suits this traveller*, ≤ 25 words. Never numbers you computed yourself.

Point 3 is what keeps the agent useful rather than decorative: it can veto on semantic grounds the
scorer can't see, but it can't promote.

**Output schema:**

```python
class Selection(BaseModel):
    listing_id: str
    category: Literal["hotel","restaurant","attraction","event"]
    rank: int                     # from score_candidates, not chosen by the LLM
    score: float                  # copied verbatim from the observation
    reason: str = Field(max_length=200)

class RecommendationOutput(BaseModel):
    hotels: list[Selection]        # ≤ 3
    restaurants: list[Selection]   # ≤ 2 × duration_days
    attractions: list[Selection]   # ≤ 3 × duration_days
    events: list[Selection]        # ≤ 5
    dropped: list[DroppedItem]     # {listing_id, reason_code}
    coverage_notes: list[str]      # e.g. "no verified hotels in this district"
```

`ReActConfig(max_steps=5, tool_budget=10)`.

### 3.4 Planner agent — ReAct

**Job:** set the shape of the trip, then let Python lay it out.

**Tools:** `estimate_costs`, `build_day_plan`, `check_budget`, `travel_matrix`.

The LLM decides: how many items per day (from `pace`), which day gets which theme, which day the
hotel check-in lands on, whether to swap a day when weather is bad. It hands those as *constraints*
to `build_day_plan`, which does routing, timing, opening hours and dwell times deterministically
and hands back a fully-timed day. Then `check_budget` says yes/no, and the agent adjusts
constraints and re-calls if no.

```
Thought:  3 days, balanced pace → 3 activities + 2 meals/day. Day 2 rain 0.75 → indoor theme.
Action:   build_day_plan(day=1, theme="culture", items_target=3, anchor=hotel, exclude_outdoor=False, …)
Obs:      timed day-1 plan, 6 stops, 41 km, est LKR 12,400
Action:   build_day_plan(day=2, theme="museums+food", exclude_outdoor=True, …)
Obs:      …
Action:   check_budget(days=[…], budget=60000, travelers=2)
Obs:      {feasible: false, total: 71200, over_by: 11200, cheapest_swaps: [{…}]}
Thought:  Apply the two cheapest swaps on day 3.
Action:   build_day_plan(day=3, …, prefer_price_level_max=2)
Obs:      …
Action:   check_budget(…)  → {feasible: true, total: 57900}
Answer:   PlannerOutput(...)
```

**Output schema:**

```python
class ItineraryItem(BaseModel):
    time: str                      # "HH:MM", from build_day_plan
    end_time: str
    type: Literal["hotel","restaurant","attraction","event","travel"]
    listing_id: str | None         # None only for type="travel"
    name: str
    lat: float
    lon: float
    est_cost: float
    currency: Literal["LKR"]
    notes: str = Field(max_length=200)   # the only free text the LLM writes

class ItineraryDay(BaseModel):
    day: int
    date: str                      # ISO
    theme: str = Field(max_length=60)
    items: list[ItineraryItem]
    day_cost: float

class PlannerOutput(BaseModel):
    itinerary: list[ItineraryDay]
    estimated_cost: float
    currency: Literal["LKR"]
    budget_notes: str | None
    plan_source: Literal["llm"] = "llm"
```

`ReActConfig(max_steps=5, tool_budget=10)`.

### 3.5 Result validation *(orchestrator, second pass — pure Python)*

The `verify` node. No LLM. Runs L0/L1/L2 from `DETERMINISM_AND_VALIDATION.md §5` and, on failure,
assembles a precise error list for the repair prompt (`"item day=2 idx=3 listing_id=abc not in
candidate pool"` — not `"invalid output"`). Budget: **< 5 ms**.

### 3.6 Response narration

One small LLM call turning the validated plan into 2–4 sentences of chat text, with a Jinja
template fallback that produces acceptable prose from the same data. If the call fails or takes
> 3 s, the template ships. **The plan is never regenerated at this stage** — narration cannot
change facts.

---

## 4. Tool catalog

Every tool: async, never raises, returns a typed dict, has a `ToolError` shape for failures, and is
registered in `app/tools/registry.py` with its JSON schema for Gemini function calling.

### Context tools (orchestrator)

| Tool | Signature | Source | Cache |
|---|---|---|---|
| `resolve_place` | `(name: str) -> {name, lat, lon, district_id, confidence} \| ToolError` | `geo_resolution` table → Nominatim → (opt.) Google | permanent, table |
| `resolve_district` | `(lat: float, lon: float) -> {district_id, name, province} \| ToolError` | `ST_Contains(district.boundary, point)` | n/a (local, <2 ms) |
| `resolve_start_location` | `(client_gps, client_ip) -> {lat, lon, source} \| None` | existing tool, unchanged | none |
| `get_calendar_free_days` | `(user_id: str, window_days=60) -> [{start_date, end_date, length}]` | existing `calendar_tool` | none |
| `get_weather` | `(lat, lon, dates: list[str]) -> {current, forecast[]} \| ToolError` | OpenWeather, existing | Redis 45 min |
| `get_disaster_info` | `(lat, lon, radius_km=300) -> {safe, max_severity, active_events[]}` | EONET/USGS/GDACS, existing | Redis 1 h |

### Data tools (recommendation)

| Tool | Signature | Notes |
|---|---|---|
| `db_search_listings` | `(district_id, category, tags=[], must_avoid=[], max_price_level=None, near=None, radius_km=None, limit=40) -> {items: [...], total, truncated}` | `is_verified = true AND is_active = true`. PostGIS `ST_DWithin` when `near` given. Returns **only** DB rows. |
| `db_search_events` | `(district_id, date_from, date_to, tags=[], limit=20) -> {items, total}` | Overlap semantics, as today's `_SELECT_EVENTS`. |
| `travel_matrix` | `(origins: [{lat,lon}], destinations: [{lat,lon}], mode="drive") -> {minutes[][], km[][], provider}` | `travel_time` cache → OpenRouteService → haversine × 1.35 @ 32 km/h. Always returns something. |
| `score_candidates` | `(candidates, interests, anchor, budget_per_day, category, must_avoid) -> {ranked: [{listing_id, rank, score, breakdown{pref,prox,rating,cost}}]}` | **Pure Python.** `app/core/scoring.py`. The only legal source of an ordering. |

### Planning tools (planner)

| Tool | Signature | Notes |
|---|---|---|
| `estimate_costs` | `(items, travelers, nights) -> {per_item: {...}, subtotals: {stay,food,activity,transport}, total, currency}` | Uses `travel_listing.price_per_night` where present, else `cost_reference`. Marks each cost `exact` \| `estimated`. |
| `build_day_plan` | `(day, date, anchor, selections, constraints) -> {items[], day_cost, total_km, total_travel_min, dropped[]}` | **Pure Python.** Weather/disaster filter → meal slots → nearest-neighbour route → opening-hours check → times. |
| `check_budget` | `(days, budget, travelers) -> {feasible, total, over_by, per_category, cheapest_swaps[]}` | **Pure Python.** `cheapest_swaps` gives the agent actionable, cheapest-first alternatives. |

**Note on tool count:** 13 tools total, but no agent ever sees more than 6. Gemini's tool-selection
quality degrades with large tool lists; per-agent binding keeps each decision small.

---

## 5. Session memory

`app/utils/session_store.py` rewritten against the `ai_session` table (DDL in
[`DATA_PLATFORM.md §2`](DATA_PLATFORM.md)).

**Carried over between turns (9 fields):**
`destination_name`, `district_id`, `lat`, `lon`, `start_location`, `date_window`,
`duration_days`, `budget`, `travelers`, `interests`, `travel_style`, `must_avoid`, `pace`,
plus the last `itinerary` and `selections` (ids only, not full rows).

**Not carried (re-derived each turn):** all four candidate arrays, weather, disaster, errors,
`final_response`, traces. Today all of these are persisted, which is what makes `trip_sessions.json`
grow without bound and makes a follow-up reuse stale weather.

**Follow-up semantics stay as today:** `is_followup=True` → extracted slots overwrite rather than
fill gaps, and the planner receives `previous_itinerary` + `modification_request`. New: the
**recommendation agent is skipped entirely** on a follow-up that only changes plan shape (e.g.
"make day 2 cheaper", "start later"), because the selections are already carried. It re-runs only
when the modification changes destination, dates, interests, budget, or explicitly asks for
different places. That decision is a small deterministic classifier over the extracted slots — not
an LLM call.

**TTL:** 7 days, cleaned by a daily scheduler job.

---

## 6. Degradation matrix

What the system does when each piece fails. The rule throughout: **degrade with an explicit note, never fabricate, never 500.**

| Failure | Behaviour | User sees |
|---|---|---|
| Geocoding finds nothing | Orchestrator retries with raw text, then origin; if all fail → clarification | "Which town or district in Sri Lanka did you mean?" |
| **Destination resolves outside Sri Lanka** ✅ implemented (decision D17) | `slot_filling.py` checks `resolve_place()`'s confidence immediately after extracting `destination`, on both first and follow-up turns — routes straight to `respond`, never reaches location/calendar/weather/recommend/plan. Verified live: a `/trip-plan` request for Paris completes in one graph pass (`validate → policy → slot_fill → respond`), zero wasted downstream calls. | "SmartJourney currently covers destinations within Sri Lanka only. Paris is in France — is there a Sri Lankan destination I can help you plan instead?" |
| District has no verified listings | Recommendation returns `coverage_notes`; planner builds what it can; if nothing → honest message | "I don't have verified listings for X yet." |
| Weather unavailable | `per_day_weather = []`; outdoor filtering skipped; note added | "Weather data wasn't available, so I haven't adjusted for rain." |
| Disaster sources all down | `{safe: true, note: "unavailable"}` (existing correct behaviour — keep it) | advisory note |
| Calendar not connected | `date_window.source = "default"` | nothing (silent, correct) |
| **Calendar consent revoked/expired** (`invalid_grant`) | **Must not be silent** — today it's swallowed and reported as "no calendar connected". Set `calendar_reconnect_required` on the session, plan with default dates, surface a reconnect prompt. (Publishing the OAuth app to Production removes the 7-day token timer; user-initiated revocation remains possible.) | "Your calendar link expired — reconnect to use your free days." |
| `travel_matrix` provider down | Haversine fallback, `provider: "haversine"` | nothing; distances are approximate |
| Recommendation LLM fails | → `fallback` node: scorer picks top-N directly | full plan, `plan_source: "fallback"` |
| Planner LLM fails validation twice | → `fallback` node | full plan, `plan_source: "fallback"` |
| Gemini quota exhausted / key invalid | Whole LLM path skipped; slot-filling degrades to a regex/keyword extractor for destination + duration + budget | plan built entirely deterministically |
| Postgres down | **This one is fatal by design.** 503 with a clear message. | "The travel database is temporarily unavailable." |

That last row is the deliberate reversal of today's behaviour, where a DB outage silently produces
a plan from mock data — a plan that looks real and is not. Failing loudly is the correct trade.
