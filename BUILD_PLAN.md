# Smart Tourism Assistant — Build Plan (Multi-Agent + Free-API Data Pipeline)

This is the current build plan for the project, agreed as of 2026-08-11. It supersedes
`revised_build_plan.md` and `agentic_system_plan.md` (kept in the repo for history/context) —
read this file first. This version adds concrete schemas, function contracts, and API request
shapes so each phase can be implemented without re-deriving the interfaces mid-build.

## Context

The repo currently has only scaffolding: `TripState`, `BaseAgent`, `AgentResult`, a settings
loader, a mocked `app/tools/db_tool.py`, and one prompt file. No agents, tools, orchestrator,
or API layer exist yet. This plan is organized in **phases**, not days — move to the next phase
once the current one genuinely works.

Decisions this plan assumes:
- **3 agents**: Orchestrator (router + live-context tools), Recommendation (curated DB data),
  Planner (budget + itinerary). Orchestrator calls the other two.
- **Daily-refreshed data** (hotels/restaurants, travel services, local events) via free,
  no-billing-card APIs, synced into Supabase — not fetched live per user request.
- **Only free, no-card APIs.** Amadeus/Duffel are explicitly excluded — booking is out of scope
  (the platform links out to external sites for reservations, per the project proposal).
- **Work is split by agent ownership**, not by layer (this round's decision).
- **Scope order**: finish the AI backend + data pipeline completely first. NestJS backend and
  Next.js/Flutter UI only start after that's working end-to-end (Phase 7).
- Supabase is the DB for now but must stay swappable — PostGIS, pg_cron, and RLS are plain
  Postgres/extension features, not Supabase-proprietary, as long as all DB access goes through
  `app/tools/db_tool.py` and plain SQL/SQLAlchemy rather than the Supabase client SDK directly.

**Disaster coverage for Sri Lanka:** EONET, USGS, and GDACS are all **global** feeds (unlike
NWS, which is US-only), so Sri Lanka is already covered by all three with no extra work. NWS is
excluded. ReliefWeb is a possible future enrichment for humanitarian situation reports, not
needed for MVP disaster alerts.

---

## 1. Final free-API selection, with concrete endpoints

| Need | API | Endpoint / query shape | Auth |
|---|---|---|---|
| Hotels & restaurants (base) | Overpass API | `POST https://overpass-api.de/api/interpreter` with Overpass QL body (see §5, Phase 3) | none |
| Hotels & restaurants (enrichment) | Yelp Fusion | `GET https://api.yelp.com/v3/businesses/search?latitude={lat}&longitude={lon}&radius=1000&term={name}` | `Authorization: Bearer {key}` |
| Bus/train detection | Overpass API | same interpreter endpoint, `amenity=bus_station` / `railway=station` / `public_transport=station`, `around:1000` | none |
| PickMe/Uber coverage | PostGIS `ST_Contains()` | in-DB spatial query against `pickme_zone.polygon` | n/a |
| Local events | Ticketmaster Discovery | `GET https://app.ticketmaster.com/discovery/v2/events.json?apikey={key}&latlong={lat},{lon}&radius=50&unit=km` | API key query param |
| Local events | Eventbrite | `GET https://www.eventbriteapi.com/v3/events/search/?location.latitude={lat}&location.longitude={lon}&location.within=50km` | `Authorization: Bearer {key}` |
| Weather (current) | OpenWeather | `GET https://api.openweathermap.org/data/2.5/weather?lat={lat}&lon={lon}&units=metric&appid={key}` | API key query param |
| Weather (forecast) | OpenWeather | `GET https://api.openweathermap.org/data/2.5/forecast?lat={lat}&lon={lon}&units=metric&appid={key}` | API key query param |
| Disaster — wildfires/storms/volcanoes | NASA EONET | `GET https://eonet.gsfc.nasa.gov/api/v3/events?status=open&days=20` | none |
| Disaster — earthquakes | USGS | `GET https://earthquake.usgs.gov/fdsnws/event/1/query?format=geojson&latitude={lat}&longitude={lon}&maxradiuskm=500&minmagnitude=4&starttime={date-7d}` | none |
| Disaster — floods/tsunami/cyclone alerts | GDACS | `GET https://www.gdacs.org/xml/rss.xml` (GeoRSS; filter by bounding box around destination) | none |
| Calendar | Google Calendar API v3 | `POST https://www.googleapis.com/calendar/v3/freeBusy` (OAuth token from user consent) | OAuth 2.0 |
| Location fallback | ipapi.co | `GET https://ipapi.co/{client_ip}/json/` | none |
| Flights/hotel booking | — | explicitly skipped | — |

---

## 2. Agent architecture

```
User message
     │
     ▼
[Param Check]  — plain Pydantic validation, not an LLM call
     │
     ▼
[Orchestrator Agent] (LLM router, LangGraph state machine)
     │
     ├─► Policy Guardrail   (rule-based, runs first — fail fast before paid stages)
     ├─► Location tool       (client GPS → ipapi.co fallback → ask)
     ├─► Calendar tool       (Google Calendar — find user's free days first)
     ├─► Weather tool     ┐
     ├─► Disaster tool    ┘  run concurrently via asyncio.gather — after calendar
     │
     ▼
[Recommendation Agent] (LLM)
     tools: hotels/restaurants (Overpass+Yelp), travel services (transit/PickMe flags,
            precomputed by the daily data pipeline), local events (Ticketmaster/Eventbrite),
            travel destinations/attractions
     │
     ▼
[Planner Agent] (LLM)
     tools: budget calculation, itinerary assembly (day-by-day, using Recommendation's
            picks + weather + travel time between stops)
     │
     ▼
Final response
```

Flow order: **calendar → weather → disaster → recommendation → planner**, weather/disaster
concurrent, policy checked first.

**Slot-filling rules:**
- No destination → must ask (can't plan without one).
- Destination given, nothing else → default to 1 day of activities + travel time to/from that
  destination; pull `travel_style`/`interests`/`budget` from the user's stored profile
  (captured at registration) instead of re-asking.
- No start location → client GPS → ipapi.co IP fallback → ask only if both fail.

---

## 3. Data model — Supabase schema (detailed)

All tables live in Postgres via Supabase; PostGIS enabled for `geography(Point,4326)` /
`geometry(Polygon,4326)` columns. Types below are the practical minimum — add indexes on every
foreign key and on `district_id`/`is_verified` (the Recommendation Agent filters on those constantly).

**`district`**
| Column | Type | Notes |
|---|---|---|
| id | uuid, pk | |
| name | text | e.g. "Kandy" |
| province | text | |
| center_lat / center_lon | double precision | for radius-based API calls |

**`category`**
| id (pk) | name (text) | — "hotel", "restaurant", "attraction" |

**`travel_listing`**
| Column | Type | Notes |
|---|---|---|
| id | uuid, pk | |
| district_id | fk → district | |
| category_id | fk → category | |
| name, description | text | |
| location | geography(Point,4326) | lat/lon |
| price_range | text | `$`/`$$`/`$$$` |
| source | text | `overpass`\|`admin`\|`yelp_enriched` |
| external_ref | text, nullable | OSM/Yelp id, for de-duping on re-sync |
| rating | numeric, nullable | from Yelp |
| photo_url | text, nullable | from Yelp |
| opening_hours | text, nullable | from Yelp |
| has_public_transit | boolean, default false | |
| nearest_transit_stop | text, nullable | |
| pickme_available | boolean, default false | |
| is_verified | boolean, default false | admin approval gate |
| created_at / updated_at | timestamptz | |

**`listing_image`** — `id`, `listing_id` (fk), `url`, `caption` (nullable)

**`local_event`**
| id (pk) | district_id (fk) | source (`ticketmaster`\|`eventbrite`\|`admin`) | external_ref | name | description | start_datetime | end_datetime (nullable) | venue_name | location (geography Point) | price_info (jsonb) | is_verified (bool) |

**`user_profile`** — `user_id` (pk), `default_interests` (text[]), `default_travel_style` (text), `default_budget` (numeric, nullable), `home_location` (geography Point, nullable)

**`itinerary`** — `id` (pk), `user_id` (fk), `district_id` (fk), `start_date`/`end_date` (date), `travelers` (int), `budget` (numeric, nullable), `estimated_cost` (numeric, nullable), `status` (text), `created_at`

**`itinerary_day`** — `id`, `itinerary_id` (fk), `day_number` (int), `date` (date)

**`itinerary_item`** — `id`, `itinerary_day_id` (fk), `listing_id` (fk, nullable), `event_id` (fk, nullable), `item_type` (`attraction`\|`hotel`\|`restaurant`\|`event`), `start_time`/`end_time` (time, nullable), `order_index` (int), `notes` (text, nullable)

**`expense`** — `id`, `itinerary_id` (fk), `category` (text), `amount` (numeric), `description` (nullable)

**`pickme_zone`** — `id`, `district_id` (fk, nullable), `name` (text), `polygon` (geometry(Polygon,4326))

---

## 4. Tool & agent function contracts

These are the exact shapes to build against — lock these before writing tool bodies so
Orchestrator (Member A) and Recommendation/Planner (Member B) never have to renegotiate an
interface mid-build.

### `app/tools/location_tool.py`
```python
async def resolve_start_location(
    client_gps: Optional[dict],   # {"lat": float, "lon": float} if the frontend sent it
    client_ip: Optional[str],
) -> Optional[dict]:              # {"lat": float, "lon": float, "source": "gps" | "ip"} or None
```
`None` means both GPS and IP lookup failed — Orchestrator must ask the user directly in that case.

### `app/tools/calendar_tool.py`
```python
async def get_free_days(
    user_id: str,
    search_window_days: int = 30,
) -> list[dict]:   # [{"start_date": "2026-08-20", "end_date": "2026-08-23"}, ...]
```
Uses `freeBusy.query` against the OAuth-connected calendar. If the user hasn't connected a
calendar, return `[]` — Orchestrator falls back to asking for dates directly rather than failing.

### `app/tools/weather_tool.py`
```python
async def get_weather(lat: float, lon: float, dates: list[str]) -> dict:
    # {
    #   "current": {"temp": 28.4, "condition": "clear", "humidity": 70},
    #   "forecast": [{"date": "2026-08-20", "temp_min": 22, "temp_max": 30,
    #                 "condition": "rain", "rain_probability": 0.6}, ...]
    # }
```

### `app/tools/disaster_tool.py`
```python
async def get_disaster_info(lat: float, lon: float, radius_km: int = 300) -> dict:
    # {
    #   "safe": bool,
    #   "active_events": [
    #     {"type": "flood", "severity": "orange", "title": "...",
    #      "source": "GDACS", "distance_km": 42.0}
    #   ]
    # }
```
Calls EONET, USGS, and GDACS concurrently (`asyncio.gather`), filters each by distance from
`(lat, lon)`, and merges into one severity-ranked list. `severity` uses GDACS's own
green/orange/red scale where available; map EONET/USGS events to the same scale by category
(e.g. magnitude ≥ 6 earthquake → orange, ≥ 7 → red).

### `app/tools/db_tool.py` (already scaffolded as a mock — this is the target contract)
```python
async def get_hotels(destination: str, interests: list[str] | None = None) -> list[dict]: ...
async def get_restaurants(destination: str, interests: list[str] | None = None) -> list[dict]: ...
async def get_attractions(destination: str, interests: list[str] | None = None) -> list[dict]: ...
async def get_events(destination: str, start_date: str, end_date: str) -> list[dict]: ...
async def get_transit_info(listing_id: str) -> dict: ...
async def check_pickme_coverage(lat: float, lon: float) -> bool: ...
async def get_user_profile(user_id: str) -> dict:
    # {"interests": [...], "travel_style": "...", "budget": float | None,
    #  "home_location": {"lat":..., "lon":...} | None}
```
Per-listing dict shape returned by `get_hotels`/`get_restaurants`/`get_attractions` (matches
`travel_listing` columns, `is_verified = true` only):
```python
{
  "id": "uuid", "name": "...", "description": "...", "price_range": "$$",
  "lat": 7.29, "lon": 80.63, "rating": 4.2, "photo_url": "...", "opening_hours": "...",
  "has_public_transit": True, "nearest_transit_stop": "Kandy Bus Stand",
  "pickme_available": True,
}
```
Per-event dict shape from `get_events`:
```python
{"id": "uuid", "name": "...", "description": "...", "start_datetime": "...",
 "end_datetime": "...", "venue_name": "...", "price_info": {...}}
```

---

## 5. Agent prompts

### Recommendation Agent (`app/prompts/recommendation_prompt.py` — already exists, keep as-is)
Selects/ranks from candidate hotels/restaurants/attractions/events; never invents places outside
the candidate list; returns strict JSON (`hotels`/`restaurants`/`attractions` with `reason`).
**Add**: an `"events"` key to the returned JSON shape (currently missing) so `state.events` has
a source.

### Planner Agent (`app/prompts/planning_prompt.py` — new)
```python
PLANNING_SYSTEM_PROMPT = """You are the Planner Agent for a travel planning assistant.
You are given: destination, duration_days, budget, travelers, weather forecast, disaster
warnings, and the Recommendation Agent's selected hotels/restaurants/attractions/events.

Build a day-by-day itinerary:
- Avoid scheduling outdoor-heavy activities on days with severe weather or active disaster
  warnings near the destination — prefer indoor alternatives from the candidate list on those days.
- Respect the given budget by choosing a price_range mix that fits; if no candidate combination
  fits, say so in "budget_notes" rather than silently exceeding it.
- Minimize backtracking between stops within a day.

Return strictly as JSON:
{
  "itinerary": [
    {"day": 1, "date": "2026-08-20", "items": [
      {"time": "09:00", "type": "attraction", "name": "...", "notes": "..."}
    ]}
  ],
  "estimated_cost": 0.0,
  "budget_notes": "..."
}
"""
```

### Orchestrator slot-filling (inline prompt or `app/utils/slot_filling.py` — new)
LLM-assisted extraction from `user_input`, applied on top of rule-based checks: extract
`destination`, `travelers`, `duration_days`, `interests`, `budget` if mentioned; leave unset
fields `None` rather than guessing, so the defaulting logic in §2 can apply cleanly.

---

## 6. LangGraph graph specification

```python
graph = StateGraph(TripState)
graph.add_node("validate", validate_node)          # utils/validators.py
graph.add_node("policy", policy_node)               # utils/policy_guard.py
graph.add_node("location", location_node)           # tools/location_tool.py
graph.add_node("calendar", calendar_node)           # tools/calendar_tool.py
graph.add_node("context", context_node)             # weather + disaster together
graph.add_node("recommend", recommendation_node)    # workflows/recommendation_agent.py
graph.add_node("plan", planning_node)               # workflows/planning_agent.py
graph.add_node("respond", respond_node)

graph.set_entry_point("validate")
graph.add_conditional_edges("validate", lambda s: "policy" if not s.errors else "respond")
graph.add_conditional_edges("policy", lambda s: "location" if not s.errors else "respond")
graph.add_edge("location", "calendar")
graph.add_edge("calendar", "context")
graph.add_edge("context", "recommend")
graph.add_edge("recommend", "plan")
graph.add_edge("plan", "respond")
graph.set_finish_point("respond")
```
**Note on weather ∥ disaster:** implement as a single `context_node` that internally does
`await asyncio.gather(get_weather(...), get_disaster_info(...))` rather than trying to model
true fan-out/join edges in LangGraph — for a 2-person capstone, a plain graph with one node
doing two concurrent awaits is far simpler to reason about and debug than LangGraph-level
parallel branches with a join, and produces the same behavior.

---

## 7. FastAPI endpoint contract (`app/api/trip.py`)

**Request** — `POST /trip-plan`
```json
{
  "user_id": "uuid",
  "message": "Plan a 3-day trip to Kandy for 2 people",
  "client_gps": {"lat": 7.29, "lon": 80.63},
  "session_id": "optional-for-multi-turn-later"
}
```

**Response**
```json
{
  "final_response": "text summary for the chat UI",
  "itinerary": [{"day": 1, "date": "2026-08-20", "items": [...]}],
  "estimated_cost": 450.0,
  "weather": {...},
  "disaster_warnings": [...],
  "errors": []
}
```
`client_gps` is optional — omit it to force the IP-fallback → ask chain in `location_tool.py`.
No JWT header is required yet (see Phase 5, step 2).

---

## 8. Error handling & caching

| Source | On timeout/failure | Cache TTL |
|---|---|---|
| Overpass | retry once, then skip that district for this sync run (log it, don't crash the whole pipeline) | n/a (daily batch) |
| Yelp Fusion | skip enrichment for that listing, keep Overpass-only data | n/a (daily batch) |
| Ticketmaster / Eventbrite | skip that source for this district this run, other source still upserts | n/a (daily batch) |
| OpenWeather | return `None` for `state.weather`; Planner proceeds without weather-based day adjustment, notes this in `budget_notes`/response | 30–60 min per `(lat, lon)` rounded to 2 decimals |
| EONET / USGS / GDACS | if any one fails, merge results from the other two; if all three fail, `state.disaster = {"safe": True, "active_events": [], "note": "disaster data unavailable"}` — never block the trip plan on this | 30–60 min per `(lat, lon)` rounded to 2 decimals |
| Google Calendar | if not connected or call fails, `get_free_days` returns `[]`; Orchestrator asks the user for dates directly | n/a (live per request) |
| ipapi.co | if it fails and no GPS given, `resolve_start_location` returns `None`; Orchestrator asks | n/a (live per request) |

Within a single graph run, never call the same tool twice for the same input — pass results
through `TripState` rather than re-fetching (e.g. Planner reuses `state.weather`, doesn't refetch it).

---

## 9. Work split — by agent ownership

**Member A — Orchestrator track.** Owns the router agent and every tool it calls directly —
all live, per-request integrations with no DB dependency, so this track is unblocked from day one.

**Member B — Recommendation + Planner + Data track.** Owns the two DB-backed agents and the
data layer under them — whoever owns Recommendation Agent needs the pipeline that fills its
candidate lists, so this keeps that dependency inside one person.

**Seam:** the `db_tool.py` contract in §4 is the interface. Member A's Orchestrator calls
Recommendation/Planner as black boxes; Member B builds those agents against the Phase-1 mock
while Supabase work is in progress. Neither blocks the other until Phase 4.

---

## 10. Phases — step by step

### Phase 1 — Foundations & contracts (both, together)

1. Both: confirm the `db_tool.py` contract in §4 is agreed as final before writing any tool bodies.
2. Member A: add the fields from §3 (TripState additions — `start_location`, `trip_dates`,
   `disaster`, `events`, `user_id`) to `app/core/state.py`.
3. Member A: add new keys to `app/config/settings.py` and `.env.example` — Ticketmaster,
   Eventbrite, Yelp Fusion, Google Calendar OAuth client id/secret. (Overpass, EONET, USGS,
   GDACS, ipapi.co need no key — don't add placeholders for them.)
4. Member B: extend the mocked `app/tools/db_tool.py` to implement the full §4 contract
   (still hardcoded data) so Member A can build against it immediately.
5. Checkpoint: both can import `TripState` with the new fields and call every mocked
   `db_tool.py` function without errors before moving on.

### Phase 2 — Parallel build

**Member A — steps:**
1. `app/utils/validators.py` — param-check: date range sane, budget > 0 if given, travelers ≥ 1
   if given. Pure Pydantic/plain-Python, no LLM call.
2. `app/utils/policy_guard.py` — rule-based blocklist/keyword check on `user_input`.
3. `app/tools/location_tool.py` — implement `resolve_start_location` per §4.
4. `app/tools/calendar_tool.py` — implement `get_free_days` per §4 (Google OAuth flow +
   `freeBusy.query`).
5. `app/tools/weather_tool.py` — implement `get_weather` per §4, hitting the two OpenWeather
   endpoints from §1.
6. `app/tools/disaster_tool.py` — implement `get_disaster_info` per §4, hitting EONET/USGS/GDACS
   concurrently per §1, applying the severity mapping described in §4.
7. `app/utils/slot_filling.py` — LLM-assisted extraction per §5's Orchestrator prompt outline.
8. `app/core/orchestrator.py` — build the graph from §6, using stub functions returning fixed
   dicts for `recommend`/`plan` at this point (real wiring is Phase 4).
9. Checkpoint: run the graph against 2–3 scripted inputs (full details given, destination-only,
   no details at all) and confirm slot-filling + tool calls behave correctly against the stubs.

**Member B — steps:**
1. Create the Supabase project; enable **PostGIS** and **pg_cron** in the dashboard.
2. Set up baseline **RLS** policies (permissive is fine now — easier than retrofitting later).
3. Build the schema from §3 exactly — table by table, with the columns/types listed there.
4. Seed 10–20 hand-written rows across 2–3 districts (see §11 for which) so there's real data
   to query before the pipeline is live.
5. `app/workflows/recommendation_agent.py` implementing `BaseAgent.execute()` — select/rank
   from `db_tool.py` candidates (still the Phase-1 mock here) using the §5 prompt; add the
   `"events"` key to the returned JSON.
6. `app/workflows/planning_agent.py` implementing `BaseAgent.execute()` using the §5 Planner
   prompt — produces `state.itinerary` + `state.estimated_cost`.
7. Checkpoint: test both agents standalone with a hardcoded `TripState` (same pattern as
   `test_state.py`) before Supabase data is real.

### Phase 3 — Data pipeline (Member B)

1. Ingest job per district: Overpass query for hotels/restaurants/attractions, e.g.
   ```
   [out:json][timeout:25];
   area["name"="Kandy District"]->.searchArea;
   ( node["tourism"="hotel"](area.searchArea);
     node["amenity"="restaurant"](area.searchArea); );
   out body;
   ```
   then a second Overpass call per listing for transit within 1km:
   ```
   [out:json][timeout:25];
   ( node["amenity"="bus_station"](around:1000,{lat},{lon});
     node["railway"="station"](around:1000,{lat},{lon});
     node["public_transport"="station"](around:1000,{lat},{lon}); );
   out body;
   ```
   → sets `has_public_transit` / `nearest_transit_stop`.
2. Yelp Fusion enrichment: for each Overpass entry, call the §1 Yelp search endpoint by
   coordinates + radius, match by normalized name within ~100m (lock the exact rule per §11
   before starting); attach `rating`/`opening_hours`/`photo_url` on match, leave blank on no match.
3. PickMe geofencing: hand-draw GeoJSON polygons per launch district into `pickme_zone`; on
   ingest, run `ST_Contains(polygon, location)` → set `pickme_available`.
4. Events refresh: pull Ticketmaster + Eventbrite (§1 endpoints) for the next ~30 days per
   district, upsert into `local_event`.
5. All newly synced rows land with `is_verified = false`. `db_tool.py` queries must filter
   `WHERE is_verified = true` — the approve/reject UI itself is Phase 7's Admin Panel, not built now.
6. Wire scheduling: pg_cron → Supabase Edge Function, or a plain scheduled script if pg_cron is
   a fight to debug. Keep the sync *logic* as a plain Python script either way.
7. Swap `app/tools/db_tool.py` from the Phase-1 mock to real Supabase queries, **keeping the
   §4 function signatures identical**.
8. Checkpoint: run the pipeline once manually, spot-check a handful of synced rows for correct
   `has_public_transit`, `pickme_available`, `is_verified=false`.

### Phase 4 — Integration (both meet)

1. Member A: replace the Phase-2 stub calls in `core/orchestrator.py` with real calls to
   Recommendation Agent and Planner Agent.
2. Member B: on standby to debug data-shape mismatches as Member A hits them.
3. Confirm the full graph order live, matching §6.
4. Verify slot-filling defaults end-to-end against §2's rules.
5. Checkpoint: 3–4 scripted end-to-end runs (full-details, destination-only, default-heavy),
   all producing a sane `final_response`.

### Phase 5 — API layer + testing (both)

1. Member A: `app/api/trip.py` — implement `POST /trip-plan` per the §7 contract.
2. Both: skip JWT auth in FastAPI for now — this service stays internal, called by NestJS in
   Phase 7, which is where real user-facing auth belongs.
3. Both: unit tests per agent/tool with fixed `TripState` inputs, extending `test_state.py`'s pattern.
4. Checkpoint: full manual end-to-end test via the API, per §12.

### Phase 6 — Minimal demo UI + polish (whoever's free first, both)

1. Bare chat page (or CLI/Postman flow) calling `POST /trip-plan` and rendering the response.
2. Apply the §8 error-handling matrix — confirm the agent degrades gracefully (not a crash) for
   each failure mode listed there.
3. Apply the §8 caching TTLs for weather/disaster.
4. Prepare a demo script/walkthrough for the mentor.

### Phase 7 — NestJS backend + Web/Mobile UI (starts only after Phase 6 is solid)

Lighter detail here since it's deliberately after the AI backend is stable — refine this section
once Phase 6 is actually done.

1. Scaffold NestJS backend: JWT auth, user/trip endpoints proxying to the FastAPI service
   (§7's contract becomes the internal call NestJS makes), admin endpoints.
2. Build the Admin Panel's "pending listings" approve/reject view — the UI for `is_verified`
   from Phase 3.
3. Next.js web app: chat UI, itinerary viewer, district/category filters, reports/analytics view.
4. Flutter mobile app: chat UI, nearby-attractions map, notifications.
5. Suggested split (revisit closer to the time): Member A → NestJS + Admin Panel; Member B →
   Next.js + Flutter (closer to their data/content track, since they know the schema best).

---

## 11. Open items — decide before Phase 3 starts

- **Launch districts:** pick 2–3 (e.g. Kandy, Colombo, Galle) to fully populate via the
  pipeline; expand once the loop works.
- **PickMe polygon source:** hand-drawn GeoJSON per district is good enough for a capstone demo.
- **Ticketmaster/Eventbrite Sri Lanka coverage:** do a quick manual spot-check early — both
  skew US/Europe-heavy; admin-entered events may end up mattering more than the API pipeline.
- **Yelp/Overpass name-matching rule:** agree the exact fuzzy-match threshold (e.g.
  normalized-name equality + within ~100m) before Phase 3.

---

## 12. Verification checklist

- Unit-test each tool independently (weather/disaster/calendar/location) with mocked HTTP
  responses first — pure functions, cheapest to verify in isolation. Include a failure-mode
  test per tool matching the §8 matrix (e.g. OpenWeather times out → `state.weather is None`,
  plan still completes).
- Test Recommendation/Planner agents against the Phase-1 mock `db_tool.py` before Supabase is live.
- After Phase 4, run the full graph against these scripted inputs and confirm the outcome:
  - `"Plan a 5-day trip to Galle for 2 people, budget $500, interested in beaches and food"` →
    no slot-filling questions, itinerary respects budget and interests.
  - `"Plan a trip to Kandy"` → asks nothing further; defaults to 1 day + travel time, pulls
    `interests`/`travel_style`/`budget` from `get_user_profile`.
  - `"Plan a trip"` (no destination) → Orchestrator asks for a destination before doing anything else.
  - No `client_gps` in the request → falls through to ipapi.co; simulate ipapi.co failure too
    → Orchestrator asks for a starting location.
- After Phase 3's pipeline runs once, manually spot-check synced rows in Supabase for
  `has_public_transit`, `pickme_available`, `is_verified=false` before trusting the daily job unattended.
