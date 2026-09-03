# Member A — Status & Next Steps

**Track:** Orchestrator (router agent + every live, per-request tool it calls directly)
**Owner:** Shaluka Theshan · branch `temp-shaluka`
**Reference:** [BUILD_PLAN.md](../BUILD_PLAN.md) — §4 contracts, §6 graph spec, §9 work split, §10 phases
**Snapshot date:** 2026-08-23

This document covers **Member A's work only**. Member B's track (Recommendation Agent,
Planner Agent, Supabase schema, data pipeline) is listed in the folder structure for
context but is deliberately out of scope here.

---

## 1. Current folder structure

```
ai-backend/
├── app/
│   ├── config/
│   │   └── settings.py                  [A]  API keys via pydantic-settings
│   ├── core/
│   │   ├── base_agent.py                [A]  abstract BaseAgent.execute()
│   │   ├── result.py                    [A]  AgentResult model
│   │   └── state.py                     [A]  TripState — the graph's shared state
│   ├── prompts/
│   │   ├── planning_prompt.py           [B]
│   │   └── recommendation_prompt.py     [B]
│   ├── tools/
│   │   ├── calendar_tool.py             [A]  get_free_days() — Google Calendar freeBusy
│   │   ├── db_tool.py                   [B]  mock; A consumes it read-only
│   │   └── location_tool.py             [A]  resolve_start_location() — GPS → IP
│   ├── utils/
│   │   ├── policy_guard.py              [A]  check_policy() — keyword blocklist
│   │   └── validators.py                [A]  validate_trip_state()
│   └── workflows/
│       └── recommendation_agent.py      [B]
├── Shaluka_development_details/
│   ├── phase1.md                        [A]  Phase 1 completion notes
│   └── MEMBER_A_STATUS.md               [A]  this file
├── BUILD_PLAN.md                             the agreed plan (read first)
├── memberb.md                           [A]  handoff note → B: OAuth token table
├── main.py                                   EMPTY — FastAPI entrypoint not created yet
├── requirements.txt
├── webscrape.py                              scratch/experiment, not wired into the app
├── test_db_tool.py                      [B]
├── test_location_tool.py                [A]
├── test_policy_guard.py                 [A]
├── test_recommendation_agent.py         [B]
├── test_state.py                        [A]
├── .env.example
├── .gitignore
└── Readme.md
```

`[A]` = Member A owns · `[B]` = Member B owns

**Missing directories** (Member A will create these): `app/api/`.
**Note:** no `__init__.py` files exist anywhere under `app/`. Imports currently work
through Python's implicit namespace packages, which is fine for scripts run from the repo
root but gets fragile once `uvicorn` and `pytest` are in play — see §4.

---

## 2. Member A code written so far

### `app/core/state.py` — TripState ✅ done (Phase 1, step 2)

The single Pydantic model passed between every graph node. Phase-1 additions
(`user_id`, `start_location`, `trip_dates`, `disaster`, `events`) are all in.

| Group | Fields |
|---|---|
| Input | `user_input`, `language` |
| Trip details | `destination`, `duration_days`, `budget`, `travelers`, `user_id`, `start_location`, `trip_dates` |
| Preferences | `interests`, `travel_style` |
| Candidates (from DB) | `candidate_attractions`, `candidate_hotels`, `candidate_restaurants`, `candidate_events` |
| External context | `weather`, `disaster` |
| AI outputs | `attractions`, `hotels`, `restaurants`, `events`, `itinerary`, `estimated_cost` |
| Output / control | `final_response`, `errors`, `completed_steps` |

`start_location` → `{"lat": float, "lon": float, "source": "gps"|"ip"}`
`trip_dates` → `[{"start_date": "...", "end_date": "..."}]`

### `app/config/settings.py` — Settings ✅ done (Phase 1, step 3)

`BaseSettings` loading from `.env`, `extra="ignore"`. Only `gemini_api_key` is required;
everything else defaults to `""` so signups don't block development.

Keys: `gemini_api_key`, `openweather_api_key`, `database_url`, `ticketmaster_api_key`,
`eventbrite_api_key`, `yelp_fusion_api_key`, `google_calendar_client_id`,
`google_calendar_client_secret`, `google_calendar_redirect_uri`.

Per §1 of the plan, no keys added for Overpass / EONET / USGS / GDACS / ip-api — those
need none.

### `app/core/base_agent.py` + `app/core/result.py` ✅ done (scaffolding)

`BaseAgent` is an ABC with one abstract `async def execute(self, state: TripState) -> AgentResult`.
`AgentResult` = `{success: bool, state: TripState, error: str | None}`.

⚠️ Member B's `RecommendationAgent` currently does **not** inherit `BaseAgent` and returns a
bare `TripState` rather than `AgentResult` — a Phase-4 integration seam to settle, not a
Member A blocker right now.

### `app/utils/validators.py` — `validate_trip_state()` ✅ done (Phase 2, step 1)

Rule-based sanity checks only — **not** a completeness check. Missing fields are left to
slot-filling/defaults. Appends to `state.errors` and returns the same state, so it drops
into LangGraph as a node unchanged.

Checks: `duration_days > 0`, `budget > 0`, `travelers >= 1`, and each `trip_dates` window
has `start_date <= end_date`. No LLM call, no I/O.

### `app/utils/policy_guard.py` — `check_policy()` ⚠️ working draft (Phase 2, step 2)

Substring blocklist over `state.user_input.lower()`, grouped by category (weapons,
trafficking, violence, wildlife trade, exploitation, fraud). Breaks on first hit and
appends one error. Same node-shaped signature as the validator.

The file is explicitly banner-marked as **not final** — the entries are deliberately
specific ("buy a gun", not "gun") to avoid over-blocking legitimate travel queries like
"gun salute ceremony" or "knife-making workshop", but a substring blocklist is inherently
bypassable.

### `app/tools/location_tool.py` — `resolve_start_location()` ✅ done (Phase 2, step 3)

Priority chain: client GPS → IP geolocation → `None` (which tells the Orchestrator to ask
the user directly). Wrapped in a silent `try/except` per §8 — location failures degrade,
never crash the graph.

**Deviation from the plan, on purpose:** uses `http://ip-api.com/json/{ip}` instead of
ipapi.co. ipapi.co's unkeyed tier returned HTTP 429 during testing (~1,000 req/day);
ip-api.com is unkeyed at 45 req/min. Tradeoff: ip-api.com free tier is HTTP-only. Documented
inline in the file. If plain outbound HTTP is ever blocked in deployment, switch to ipapi.co
with a free key.

### `app/tools/calendar_tool.py` — `get_free_days()` ✅ works, ⚠️ contract mismatch (Phase 2, step 4)

Full Google Calendar path: stored credentials → refresh if expired → `freebusy().query()`
over the next `search_window_days` → expand busy periods into busy dates → return the days
that aren't busy. Catch-all `except` returns `[]`, matching §8 ("if not connected or call
fails, return `[]`, Orchestrator asks for dates directly").

Credential storage is an in-memory `_FAKE_CREDENTIAL_STORE` dict behind
`get_stored_credentials()` / `save_credentials()`. The handoff to Member B for a real
`google_oauth_tokens` table is already written up in [memberb.md](../memberb.md), with the
exact signatures so this file won't need to change when the table lands.

⚠️ **Return type doesn't match §4.** The plan specifies
`list[dict]` → `[{"start_date": "2026-08-20", "end_date": "2026-08-23"}, ...]` (contiguous
ranges), and `TripState.trip_dates` is typed `Optional[List[dict]]` to match. The
implementation returns `list[str]` → `["2026-08-20", "2026-08-21", ...]` (flat days). This
must be reconciled before the calendar node writes into `trip_dates` — see §3.

⚠️ The OAuth **consent flow itself** (redirect to Google, callback handler populating the
credential store) is not written yet. Without it `get_stored_credentials()` always returns
`None`, so `get_free_days()` always short-circuits to `[]`.

### Member A tests

| File | Covers | Notes |
|---|---|---|
| `test_state.py` | TripState constructs | smoke check only |
| `test_policy_guard.py` | 11 cases — 6 should pass, 5 should block | includes the false-positive traps ("gun salute", "knife-making workshop") |
| `test_location_tool.py` | 5 cases — GPS, IP fallback, null GPS, nothing, bad IP | makes **real** network calls; header comment still says "ipapi.co", stale since the ip-api.com switch |

All are plain `python3 test_x.py` scripts run from the repo root, not `pytest` tests —
fine for now; §12 calls for mocked-HTTP unit tests in Phase 5.

---

## 3. Phase status — Member A

| Phase | Member A scope | Status |
|---|---|---|
| **Phase 1** | TripState fields, settings keys, `.env.example` | ✅ Complete |
| **Phase 2** | 9 steps — see below | 🟡 4 / 9 |
| **Phase 4** | Replace stubs with real agent calls | ⬜ Blocked on Phase 2 step 8 |
| **Phase 5** | `app/api/trip.py`, tests | ⬜ Not started |
| **Phase 6** | Error matrix + caching + demo | ⬜ Not started |

**Phase 2 detail:**

| # | Step | Status |
|---|---|---|
| 1 | `app/utils/validators.py` | ✅ Done |
| 2 | `app/utils/policy_guard.py` | ⚠️ Working draft — marked non-final |
| 3 | `app/tools/location_tool.py` | ✅ Done |
| 4 | `app/tools/calendar_tool.py` | ⚠️ Done, minus OAuth flow + return-shape fix |
| 5 | `app/tools/weather_tool.py` | ⬜ **Not started** |
| 6 | `app/tools/disaster_tool.py` | ⬜ **Not started** |
| 7 | `app/utils/slot_filling.py` | ⬜ **Not started** |
| 8 | `app/core/orchestrator.py` | ⬜ **Not started** |
| 9 | Checkpoint: graph vs. 3 scripted inputs | ⬜ Not started |

---

## 4. Next steps for Member A

In order. Steps 1–2 are quick fixes to already-written code; 3–7 are the rest of Phase 2.

### Step 1 — Fix the `get_free_days` return shape (quick, do first)

Pick one and make `TripState.trip_dates`, `calendar_tool.py`, and the eventual calendar node
agree:

- **Recommended:** keep §4's contract — group consecutive free days into ranges before
  returning, so `["08-20","08-21","08-22","08-25"]` becomes
  `[{"start_date":"2026-08-20","end_date":"2026-08-22"}, {"start_date":"2026-08-25","end_date":"2026-08-25"}]`.
  This is what `trip_dates` is already typed for, and the Planner wants contiguous windows,
  not a scatter of days.
- Or: amend §4 + `TripState.trip_dates` to `List[str]` and note the change in BUILD_PLAN.md.

Whichever you choose, update BUILD_PLAN.md §4 so the file and the plan don't drift again.

### Step 2 — Housekeeping (5 minutes, prevents Phase 5 pain)

- Add empty `__init__.py` to `app/`, `app/config/`, `app/core/`, `app/prompts/`,
  `app/tools/`, `app/utils/`, `app/workflows/` — namespace packages will bite once uvicorn
  and pytest load the tree.
- `.env.example` has `GOOGLE_MAPS_API_KEY` with no matching field in `settings.py`; it's
  unused by the current plan (Overpass covers the geo work). Drop it or confirm with the team.
- `requirements.txt` is saved as **UTF-16** — `pip install -r` on a clean machine may choke.
  Re-save as UTF-8. Also missing `google-auth-oauthlib` (needed for the OAuth consent flow
  in step 4) and `beautifulsoup4` (used by `webscrape.py`).
- Fix the stale "ipapi.co" comment in `test_location_tool.py` — it calls ip-api.com now.

### Step 3 — `app/tools/weather_tool.py` (Phase 2, step 5)

```python
async def get_weather(lat: float, lon: float, dates: list[str]) -> dict
```

- Two OpenWeather endpoints per §1: `/data/2.5/weather` (current) and `/data/2.5/forecast`
  (5-day / 3-hour). `units=metric`, key from `settings.openweather_api_key`.
- The forecast endpoint returns 3-hour slices — aggregate them per calendar date into
  `temp_min` / `temp_max` / dominant `condition` / max `rain_probability` (`pop`), and filter
  down to the requested `dates`.
- Return shape (§4):
  `{"current": {"temp","condition","humidity"}, "forecast": [{"date","temp_min","temp_max","condition","rain_probability"}, ...]}`
- Failure mode per §8: return `None` — never raise. The Planner proceeds without weather.
- `httpx.AsyncClient` with a timeout, same pattern as `location_tool.py`.

### Step 4 — `app/tools/disaster_tool.py` (Phase 2, step 6)

```python
async def get_disaster_info(lat: float, lon: float, radius_km: int = 300) -> dict
```

- Three sources, all keyless, all **global** (Sri Lanka is covered with no extra work):
  NASA EONET, USGS earthquakes, GDACS GeoRSS — endpoints in §1.
- Fire all three concurrently with `asyncio.gather(..., return_exceptions=True)`, then filter
  each by haversine distance from `(lat, lon)` and merge into one severity-ranked list.
- Severity scale: GDACS's own green/orange/red where present; map USGS by magnitude
  (≥ 6 → orange, ≥ 7 → red) and EONET by category.
- GDACS is XML/GeoRSS, not JSON — needs a parser (`xml.etree.ElementTree` is enough; no new
  dependency).
- Failure mode per §8: any one source failing → merge the other two. All three failing →
  `{"safe": True, "active_events": [], "note": "disaster data unavailable"}`. Never block the
  trip plan on this.

### Step 5 — `app/utils/slot_filling.py` (Phase 2, step 7)

- LLM-assisted extraction from `state.user_input` using Gemini (`settings.gemini_api_key`),
  layered **on top of** the rule-based checks, not replacing them.
- Extract `destination`, `travelers`, `duration_days`, `interests`, `budget`. Leave anything
  not clearly stated as `None` — do **not** guess; §2's defaulting logic depends on unset
  meaning unset.
- Then apply §2's slot-filling rules:
  - no `destination` → must ask the user, stop everything else;
  - destination only → default to 1 day of activities + travel time, and pull
    `interests` / `travel_style` / `budget` from `db_tool.get_user_profile(user_id)` rather
    than re-asking;
  - no start location → GPS → IP → ask only if both fail.
- Ask for strict JSON back and parse defensively — a malformed LLM response should degrade
  to "extracted nothing", not crash the node.

### Step 6 — `app/core/orchestrator.py` (Phase 2, step 8) — the main event

Build the LangGraph state machine exactly as §6 specifies:

```
validate → policy → location → calendar → context → recommend → plan → respond
```

- `validate` and `policy` both branch to `respond` when `state.errors` is non-empty
  (`add_conditional_edges`) — fail fast before any paid LLM stage.
- `context` is **one node** doing `await asyncio.gather(get_weather(...), get_disaster_info(...))`
  internally. §6 is explicit: don't model true fan-out/join edges in LangGraph for this —
  same behavior, far easier to debug.
- `recommend` and `plan` are **stubs returning fixed dicts** at this stage. Wiring the real
  agents is Phase 4, after Member B's agents are testable.
- Append to `state.completed_steps` in each node — that field exists specifically to make
  multi-node runs debuggable.
- §8's rule: within one graph run, never call the same tool twice for the same input. Pass
  everything through `TripState` (e.g. the Planner reuses `state.weather`, never refetches).

### Step 7 — Phase 2 checkpoint (step 9)

Run the graph end-to-end against the three scripted inputs from §12 and confirm slot-filling
plus tool calls behave against the stubs:

1. `"Plan a 5-day trip to Galle for 2 people, budget $500, interested in beaches and food"`
   → no follow-up questions, all slots filled from the message.
2. `"Plan a trip to Kandy"` → asks nothing; defaults to 1 day + travel time, pulls
   preferences from `get_user_profile`.
3. `"Plan a trip"` → Orchestrator asks for a destination before doing anything else.

Also exercise the no-`client_gps` path and a simulated IP-lookup failure → should end in
"ask the user for a starting location", not an exception.

### After Phase 2

- **Phase 4:** swap the `recommend` / `plan` stubs for real calls into Member B's agents.
  Expect data-shape mismatches — `AgentResult` vs. bare `TripState` is a known one (§2 above).
- **Phase 5:** `app/api/trip.py` — `POST /trip-plan` per §7's request/response contract, and
  fill in the currently empty `main.py` as the FastAPI entrypoint. No JWT yet — auth belongs
  in the NestJS layer in Phase 7.
- **Phase 6:** apply §8's caching TTLs (30–60 min per `(lat, lon)` rounded to 2 decimals, for
  weather and disaster) and walk the §8 failure matrix tool by tool.

---

## 5. Open items Member A is waiting on / owes

| Item | Direction | Status |
|---|---|---|
| `google_oauth_tokens` table + `get_stored_credentials` / `save_credentials` | A → B | Requested in [memberb.md](../memberb.md); not blocking (falls through to `[]`) |
| Google OAuth consent + callback route | A owns | Not started — belongs with the API layer in Phase 5 |
| Policy guard hardening | A owns | Draft flagged as non-final in the file itself |
| `get_free_days` return shape vs. §4 | A owns | Fix before the calendar node is written |
| Agent return type: `AgentResult` vs bare `TripState` | A ↔ B | Settle before Phase 4 integration |
