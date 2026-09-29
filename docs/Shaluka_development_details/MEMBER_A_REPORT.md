# Member A — Full Progress Report & Forward Roadmap

**Track:** Orchestrator — the router agent plus every live, per-request tool it calls directly
**Owner:** Shaluka Theshan · branch `temp-shaluka`
**Reference plan:** [BUILD_PLAN.md](../BUILD_PLAN.md) — §4 contracts, §6 graph spec, §7 API contract, §8 error matrix, §9 work split, §10 phases
**Snapshot date:** 2026-08-27
**Scope of this document:** the `temp-shaluka` branch only, as it stands today.
**Supersedes:** [MEMBER_A_STATUS.md](../MEMBER_A_STATUS.md) (snapshot 2026-08-23 — now out of date: the orchestrator, weather tool, slot filling, API layer and OAuth flow have all landed since)

Per BUILD_PLAN §9, work is split **by agent ownership**, not by layer:

- **Member A — Orchestrator track...** The router agent and every tool it calls directly: all live,
  per-request integrations with no DB dependency (location, calendar, weather, disaster), plus the
  rule-based guards, slot filling, the LangGraph wiring, and the FastAPI layer.
- **Member B — Recommendation + Planner + Data track.** The two DB-backed agents, the Supabase
  schema, and the daily data pipeline that fills their candidate lists.
- **The seam** is the `db_tool.py` contract in §4. Member A's Orchestrator calls
  Recommendation/Planner as black boxes; Member B builds those agents against the Phase-1 mock.

This report is written from **Member A's** point of view. Member B's files are noted only where
they appear on this branch, because Member A's graph consumes them.

---

## 1. Executive summary

| Question | Answer |
|---|---|
| Is Member A's Phase 2 done? | **Almost.** 8 of 9 steps built. The one file never started is `disaster_tool.py` (Phase 2, step 6). |
| Is the graph running end-to-end? | **Yes.** `validate → policy → slot_fill → location → calendar → context → recommend → plan → respond` compiles and runs, with stub Recommendation/Planner nodes. `test_orchestrator.py` pushes 3 scripted cases through it. |
| Is the API layer up? | **Yes, ahead of schedule.** `POST /trip-plan` plus the full Google OAuth login/callback pair exist, and `main.py` boots them. That's Phase 5 step 1 done during Phase 2. |
| What's actually missing? | The disaster tool · §2's slot-filling defaulting rules · a clarification/ask-back path · `db_tool` is never called · §7 response fields · caching · real tests. |

**Rough completion of Member A's own scope: ~70%.** The remaining 30% is one missing tool, the
defaulting rules, a handful of correctness bugs, and the §7/§8 compliance pass.

---

## 2. What's on this branch right now

```
ai-backend/                                   (branch: temp-shaluka)
├── main.py                              [A]  ✅ FastAPI entrypoint, mounts both routers
├── app/
│   ├── api/
│   │   ├── trip.py                      [A]  ✅ POST /trip-plan
│   │   └── google_oauth.py              [A]  ✅ /auth/google/login + /callback
│   ├── config/
│   │   └── settings.py                  [A]  ✅ pydantic-settings, 9 keys
│   ├── core/
│   │   ├── state.py                     [A]  ✅ TripState (all Phase-1 fields present)
│   │   ├── base_agent.py                [A]  ✅ abstract BaseAgent.execute()
│   │   ├── result.py                    [A]  ✅ AgentResult
│   │   └── orchestrator.py              [A]  ✅ full LangGraph, stub recommend/plan
│   ├── tools/
│   │   ├── location_tool.py             [A]  ✅ resolve_start_location()
│   │   ├── calendar_tool.py             [A]  ✅ get_free_days()
│   │   ├── weather_tool.py              [A]  ✅ get_weather()
│   │   ├── disaster_tool.py             [A]  ❌ DOES NOT EXIST
│   │   └── db_tool.py                   [B]  🟡 Phase-1 mock (Kandy data only)
│   ├── utils/
│   │   ├── validators.py                [A]  ✅ validate_trip_state()
│   │   ├── policy_guard.py              [A]  ⚠️ working draft, self-flagged non-final
│   │   └── slot_filling.py              [A]  🟡 LLM extraction only, no defaulting rules
│   ├── prompts/                         [B]  ⚠️ empty on this branch (only __init__.py)
│   ├── workflows/                       [B]  ⚠️ empty on this branch (only __pycache__)
│   └── models/                               empty placeholder
├── Shaluka_development_details/
│   ├── phase1.md                        [A]  Phase 1 completion notes
│   └── MEMBER_A_REPORT.md               [A]  this file
├── MEMBER_A_STATUS.md                   [A]  older snapshot, superseded by this file
├── member_B.md                          [A]  running handoff notes → Member B
├── BUILD_PLAN.md                             the agreed plan
├── test_*.py  (9 files)                      see §5
├── requirements.txt                     ⚠️ still saved as UTF-16
├── .env.example
└── Readme.md
```

`[A]` = Member A owns · `[B]` = Member B owns

**Fixed since the last status doc:** `__init__.py` files now exist under `app/`, `app/api/`,
`app/config/`, `app/core/`, `app/models/`, `app/prompts/`, `app/tools/`, `app/utils/` — the
namespace-package fragility flagged for uvicorn/pytest is resolved. `app/workflows/` has no
`__init__.py`, but it's empty on this branch anyway.

**Member B's files on this branch:** only the Phase-1 mock `db_tool.py`. `app/prompts/` and
`app/workflows/` are empty here, so the Recommendation and Planner agents Member A's graph is
supposed to call in Phase 4 are not present on this branch — which is exactly why the `recommend`
and `plan` nodes are still stubs (§3.11).

---

## 3. What has been built — file by file

### 3.1 `app/core/state.py` — TripState ✅ (Phase 1, step 2)

The single Pydantic model threaded through every graph node. All Phase-1 additions are in.

| Group | Fields |
|---|---|
| Input | `user_input`, `language` |
| Trip details | `destination`, `duration_days`, `budget`, `travelers`, `user_id`, `start_location`, `trip_dates` |
| Preferences | `interests`, `travel_style` |
| Candidates (from DB) | `candidate_attractions`, `candidate_hotels`, `candidate_restaurants`, `candidate_events` |
| External context | `weather`, `disaster` |
| AI outputs | `attractions`, `hotels`, `restaurants`, `events`, `itinerary`, `estimated_cost` |
| Output / control | `final_response`, `errors`, `completed_steps` |

- `start_location` → `{"lat": float, "lon": float, "source": "gps"|"ip"}`
- `trip_dates` → `[{"start_date": "...", "end_date": "..."}]`

Using Pydantic here (rather than a dict) means `state.budget` instead of `state["budget"]`, with
validation for free — but note it also means **any field an agent wants to set must be declared
here first**; assigning an undeclared attribute raises at runtime. Worth telling Member B before
their agents land.

### 3.2 `app/config/settings.py` ✅ (Phase 1, step 3)

`BaseSettings` over `.env` with `extra="ignore"`. `gemini_api_key` and `openweather_api_key` are
required (no default); everything else defaults to `""` so nobody is blocked on API signups.

Keys: `gemini_api_key`, `openweather_api_key`, `database_url`, `ticketmaster_api_key`,
`eventbrite_api_key`, `yelp_fusion_api_key`, `google_calendar_client_id`,
`google_calendar_client_secret`, `google_calendar_redirect_uri`.

Per §1 of the plan, no keys for Overpass / EONET / USGS / GDACS / ip-api — none needed.

⚠️ Two issues:
1. `openweather_api_key` has **no default**, so importing anything under `app/` without a `.env`
   raises a `ValidationError`. Fine locally, hostile in CI or on a teammate's fresh clone.
2. The docstring at the bottom of the file describes `location_tool.py`'s design decisions and
   still says "ipapi.co" — stale text sitting in the wrong file.

`.env.example` matches the current key list (the old `GOOGLE_MAPS_API_KEY` placeholder is gone —
Overpass covers the geo work per §1).

### 3.3 `app/core/base_agent.py` + `result.py` ✅

`BaseAgent` is an ABC with one abstract `async def execute(self, state: TripState) -> AgentResult`.
`AgentResult` = `{success: bool, state: TripState, error: str | None}`.

This is the shape Member B's Recommendation and Planner agents need to match at Phase 4 — it's
already written up for them in [member_B.md](../member_B.md).

### 3.4 `app/utils/validators.py` ✅ (Phase 2, step 1)

Rule-based sanity checks only — deliberately **not** a completeness check; missing fields are
slot-filling's job. Appends to `state.errors`, returns the same state, so it drops into LangGraph
as a node unchanged. No LLM call, no I/O.

Checks: `duration_days > 0`, `budget > 0`, `travelers >= 1`, and every `trip_dates` window has
`start_date <= end_date`.

⚠️ Ordering consequence: `validate` runs **before** `slot_fill` in the graph, so it only ever sees
fields the API caller passed directly. Anything the LLM extracts is never validated — an LLM that
returns `duration_days: -3` sails straight through.

### 3.5 `app/utils/policy_guard.py` ⚠️ working draft (Phase 2, step 2)

Substring blocklist over `state.user_input.lower()`, grouped by category (weapons, trafficking,
violence, wildlife trade, exploitation, fraud). Breaks on first hit, appends one error. Same
node-shaped signature as the validator.

The file carries its own banner saying it isn't final. Entries are deliberately specific
("buy a gun", not "gun") to avoid blocking legitimate travel queries like "gun salute ceremony" or
"knife-making workshop" — both are covered as false-positive traps in the test file. A substring
blocklist is still inherently bypassable; hardening is a known open item.

### 3.6 `app/tools/location_tool.py` ✅ (Phase 2, step 3)

Priority chain: client GPS → IP geolocation → `None` (which tells the caller to ask the user
directly). Silent `try/except` per §8 — location failures degrade, never crash the graph. Takes
`client_gps`/`client_ip` as parameters rather than reading `TripState`, which keeps it testable in
isolation.

**Deliberate deviation from the plan:** uses `http://ip-api.com/json/{ip}` instead of ipapi.co.
ipapi.co's unkeyed tier returned HTTP 429 during testing (~1,000 req/day); ip-api.com is unkeyed at
45 req/min. Tradeoff: ip-api.com's free tier is HTTP-only. Documented inline in the file. If plain
outbound HTTP gets blocked in deployment, switch to ipapi.co with a free key.

### 3.7 `app/tools/calendar_tool.py` ✅ (Phase 2, step 4) — contract mismatch now **fixed**

Full Google Calendar path: stored credentials → refresh if expired → `freebusy().query()` over the
next `search_window_days` → expand busy periods into busy dates → return the non-busy days.
Catch-all `except` returns `[]`, exactly per §8 ("if not connected or the call fails, return `[]`;
Orchestrator asks for dates directly").

The §4 return-shape mismatch flagged in the previous status doc is **resolved**:
`_group_into_ranges()` now collapses free days into contiguous `{"start_date", "end_date"}` ranges,
matching §4 and `TripState.trip_dates`. ✅

⚠️ Credential storage is still an in-memory `_FAKE_CREDENTIAL_STORE` dict behind
`get_stored_credentials()` / `save_credentials()`. It resets on every restart, so a user "connects"
their calendar and it's gone. The handoff for a real `google_oauth_tokens` table is written up in
[member_B.md](../member_B.md) with the exact signatures, so this file won't need to change when the
table lands.

⚠️ Only requests the `calendar.freebusy` scope — enough for free/busy, not enough if we later want
to *write* the finished itinerary into the user's calendar.

### 3.8 `app/tools/weather_tool.py` ✅ (Phase 2, step 5) — **new since last status**

`async def get_weather(lat, lon, dates) -> dict | None`. Hits both OpenWeather endpoints from §1
(`/data/2.5/weather` current, `/data/2.5/forecast` 5-day/3-hour), `units=metric`, 5s httpx timeout.
Groups the 3-hour slices by calendar date and aggregates `temp_min` / `temp_max` / dominant
`condition` / max `rain_probability` (`pop`), then filters to the requested `dates`. Returns exactly
the §4 shape. Returns `None` on any failure or missing key — never raises, per §8. ✅

⚠️ Two issues: (a) `datetime.fromtimestamp(slice_["dt"])` uses the **server's** local timezone to
decide which calendar date a 3-hour slice belongs to — for a Sri Lanka trip planned from a server in
another zone, days get bucketed wrong. Use `datetime.fromtimestamp(dt, tz=timezone.utc)` or the
`dt_txt` field. (b) OpenWeather's free forecast only covers 5 days; a longer trip silently gets a
short `forecast` list with no note explaining the gap.

### 3.9 `app/tools/disaster_tool.py` ❌ **NOT BUILT** (Phase 2, step 6)

The one outright missing piece of Member A's Phase 2. `state.disaster` is therefore always `None`,
and `_context_node` has a commented-out `asyncio.gather` placeholder where it belongs. Full spec in
§7.1 of this document.

### 3.10 `app/utils/slot_filling.py` 🟡 half-built (Phase 2, step 7) — **new since last status**

`async def fill_slots(state) -> TripState`. Uses `ChatGoogleGenerativeAI` (`gemini-3.6-flash`,
`temperature=0`) with `with_structured_output(_ExtractedSlots)` to pull `destination`,
`duration_days`, `budget`, `travelers`, `interests` out of `state.user_input`. Only fills fields
currently `None` — never overwrites what the caller supplied. Wrapped in a catch-all that appends to
`state.errors` rather than raising, so a malformed LLM response degrades to "extracted nothing". ✅

The system prompt is genuinely well-tuned: it explicitly forbids defaulting `travelers` to 1 just
because a trip is being discussed, and forbids guessing duration/budget/destination. That's the
right behavior — §2's defaulting logic depends on "unset" actually meaning unset.

(Model note: `gemini-3.6-flash`, not `gemini-2.5-flash` — the 2.5 model 404s for new API keys. This
is already flagged to Member B in [member_B.md](../member_B.md).)

⚠️ **But §2's defaulting rules themselves are not implemented anywhere.** Extraction is only half
of step 7. Missing:
- no `destination` → the Orchestrator must **ask the user** and stop. Right now the graph runs all
  the way to `respond` with `destination=None` and emits "Here's your trip plan for your destination".
- destination-only → default to **1 day of activities + travel time**, and pull
  `interests` / `travel_style` / `budget` from `db_tool.get_user_profile(user_id)` instead of
  re-asking. `get_user_profile` exists in the mock and is **called from nowhere in the codebase.**
- The graph also never populates `candidate_*` from `db_tool` at all — the stub Recommendation
  node slices four empty lists.

### 3.11 `app/core/orchestrator.py` ✅ (Phase 2, step 8) — **new since last status**

The main event, and it's built. Graph:

```
validate → policy → slot_fill → location → calendar → context → recommend → plan → respond
```

- `validate` and `policy` both use `add_conditional_edges` to jump straight to `respond` when
  `state.errors` is non-empty — fail fast before any paid LLM stage. ✅
- `slot_fill` inserted between `policy` and `location` — a sensible addition to §6's spec, which
  didn't name a slot-filling node explicitly.
- `location` skips re-resolving if the API layer already set `start_location`, honoring §8's "never
  call the same tool twice per run" rule. ✅
- `calendar` fills `trip_dates` only when `user_id` is set.
- `context` is **one node**, per §6's explicit instruction not to model fan-out/join edges in
  LangGraph for a 2-person project. ✅
- `recommend` / `plan` are `BaseAgent` subclasses with stub bodies — deliberately shaped as the real
  agents' interface so Phase 4 is a body swap, not a rewiring. Documented for Member B in
  [member_B.md](../member_B.md).
- Every node appends to `state.completed_steps`. ✅

⚠️ Open issues in this file:
- **`_context_node` only runs when both `start_location` *and* `trip_dates` are set.** With no
  calendar connected — the common path — `trip_dates` is `None`, so **weather is silently never
  fetched.** This is the most impactful bug in Member A's current code.
- `context` uses `start_location` (where the user *is*) for the weather lookup, not the
  **destination**. §4 and the Planner both want destination weather.
- No disaster call (see §3.9).
- `location`'s "ask the user" path just appends an error string, so `respond` emits
  "Sorry, I ran into an issue: location_unresolved…" — a technical error, not a question.
- The graph has **no clarification / ask-back branch** at all, so §2's slot-filling rules have
  nowhere to land even once written.

### 3.12 `app/api/trip.py` ✅ (Phase 5, step 1) — **new, ahead of schedule**

`POST /trip-plan`. Resolves `start_location` inside the route (GPS from the body if the client sent
it, else the request's own IP) before building `TripState` and calling `orchestrator.ainvoke(...)` —
correct, because `TripState` has no `client_gps`/`client_ip` fields. `ClientGPS` is a nested
Pydantic model, so malformed GPS returns a clean 422 and shows up properly in `/docs`. The response
model exposes a curated subset of state rather than dumping internals. Good design notes are written
into the file's footer, including the `X-Forwarded-For` caveat for deployment behind a proxy.

⚠️ **Request and response both deviate from §7:**

| §7 says | Code has |
|---|---|
| request key `message` | `user_input` |
| request `session_id` | absent |
| response `weather` | absent |
| response `disaster_warnings` | absent |
| — | extra: `destination`, `completed_steps` (debug field exposed publicly) |

Either the code or BUILD_PLAN §7 has to move. NestJS in Phase 7 will be written against whichever
one is written down, so settle it before then.

### 3.13 `app/api/google_oauth.py` ✅ — **new; was "not started" in the last status doc**

`GET /auth/google/login?user_id=…` builds the consent URL (`access_type=offline`, `prompt=consent`,
`state=user_id`) and redirects; `GET /auth/google/callback` exchanges the code and calls
`save_credentials(user_id, {...})`. This closes the "OAuth consent flow not written yet" gap. ✅

⚠️ Items to fix before this is public-facing:
- `state` carries the raw `user_id` — that's exactly the parameter CSRF protection relies on. An
  attacker can call `/callback` with an arbitrary `state` and bind their Google account to another
  user's id. Needs a signed or opaque state token mapped server-side.
- Tokens land in the in-memory store (§3.7), so nothing persists across a restart.
- The stored dict has no `token_expiry` or `scope`, unlike the table requested in
  [member_B.md](../member_B.md).

### 3.14 `main.py` ✅

FastAPI app titled "Smart Tourism Assistant — AI Backend", mounts the trip and OAuth routers, plus a
`GET /` health check. Was empty at the last status; now done.

⚠️ No CORS middleware — the Next.js/Flutter clients in Phase 7 will need it. No logging config.

---

## 4. Phase status — Member A

| Phase | Member A scope | Status |
|---|---|---|
| **Phase 1** — Foundations & contracts | TripState fields, settings keys, `.env.example` | ✅ **Complete** |
| **Phase 2** — Parallel build | 9 steps, see below | 🟡 **8 / 9 built, 6 / 9 clean** |
| **Phase 4** — Integration | Swap stubs for the real Recommendation/Planner agents | ⬜ **Waiting** — those agents aren't on this branch yet |
| **Phase 5** — API layer + testing | `app/api/trip.py`, real tests | 🟡 **API done, tests aren't real tests** |
| **Phase 6** — Error matrix + caching + demo | §8 matrix, TTLs, demo script | ⬜ Not started |
| **Phase 7** — NestJS + Admin Panel | Member A's suggested Phase-7 half | ⬜ Not started (correctly — gated on Phase 6) |

**Phase 2 detail:**

| # | Step | Status | Note |
|---|---|---|---|
| 1 | `app/utils/validators.py` | ✅ Done | runs before slot_fill, so LLM-extracted values are unvalidated |
| 2 | `app/utils/policy_guard.py` | ⚠️ Draft | self-flagged non-final |
| 3 | `app/tools/location_tool.py` | ✅ Done | ip-api.com deviation, documented inline |
| 4 | `app/tools/calendar_tool.py` | ✅ Done | return shape fixed; storage still in-memory |
| 5 | `app/tools/weather_tool.py` | ✅ Done | timezone bucketing bug |
| 6 | `app/tools/disaster_tool.py` | ❌ **Not started** | the only fully missing file |
| 7 | `app/utils/slot_filling.py` | 🟡 Half | extraction ✅, §2 defaulting rules ❌ |
| 8 | `app/core/orchestrator.py` | ✅ Done | context-node gating bug, no ask-back branch |
| 9 | Checkpoint vs. 3 scripted inputs | 🟡 Partial | script exists; asserts "didn't crash", not "behaved correctly" |

---

## 5. Test coverage on this branch

| File | Owner | What it does | Verdict |
|---|---|---|---|
| `test_state.py` | A | Constructs a TripState and prints it | smoke only |
| `test_policy_guard.py` | A | 11 cases, 6 pass / 5 block, incl. the "gun salute" and "knife-making" false-positive traps | good coverage, print-based |
| `test_location_tool.py` | A | 5 cases: GPS, IP fallback, null GPS, nothing, bad IP | **real network calls**; header comment still says "ipapi.co" (stale) |
| `test_calendar_tool.py` | A | Mocks Google's API, tests range-grouping + busy-date filtering | ✅ **the best test in the repo** — the only one exercising logic in isolation |
| `test_weather_tool.py` | A | Kandy coords, 4 dates | **real OpenWeather calls**, needs a live key |
| `test_slot_filling.py` | A | Scripted extraction cases | **real Gemini calls**, costs tokens every run |
| `test_orchestrator.py` | A | 3 end-to-end graph cases (full details / destination only / no details) | asserts only "didn't crash + produced a final_response" — **does not check the §12 expectations** |
| `test_db_tool.py` | B | Mock db_tool shapes | 🔴 **stale on this branch** — queries `"Ella"`, but the mock only holds `"kandy"` |
| `test_recommendation_agent.py` | B | RecommendationAgent | 🔴 **broken on this branch** — imports `app.core.recommendation_agent`, which doesn't exist here |

**Systemic problems:**
1. Nothing is a `pytest` test with real assertions. `pytest` is in `requirements.txt` but Member A's
   files are all `python test_x.py` print-scripts.
2. Four of them make live paid or rate-limited network calls, so the suite can't run in CI or
   offline. §12 explicitly asks for **mocked-HTTP** unit tests — only `test_calendar_tool.py` does that.
3. There is **no failure-mode test at all** for the §8 matrix (e.g. "OpenWeather times out →
   `state.weather is None` → the plan still completes").
4. The two Member B test files fail on this branch because the code they target isn't here.

---

## 6. What Member A still has to do — ordered roadmap

### Priority 0 — correctness bugs in code that already exists (a few hours total)

| # | Fix | Where |
|---|---|---|
| 1 | **Context node never fetches weather.** Drop the `and state.trip_dates` requirement; fall back to today + N days when no calendar is connected. | [orchestrator.py:89](../app/core/orchestrator.py#L89) |
| 2 | **Weather is fetched for the origin, not the destination.** Resolve `state.destination` to coordinates and use those; keep `start_location` for travel-time reasoning only. | [orchestrator.py:89-93](../app/core/orchestrator.py#L89-L93) |
| 3 | **Timezone bucketing.** Use `datetime.fromtimestamp(dt, tz=timezone.utc)` or OpenWeather's `dt_txt`. | [weather_tool.py:59](../app/tools/weather_tool.py#L59) |
| 4 | **Validation runs before extraction.** Re-run `validate_trip_state` after `slot_fill`, or move the node. | [orchestrator.py](../app/core/orchestrator.py) |
| 5 | **`openweather_api_key` has no default** → import-time crash without a `.env`. Give it `= ""`. | [settings.py:9](../app/config/settings.py#L9) |
| 6 | **`requirements.txt` is UTF-16** — `pip install -r` can choke on a clean machine. Re-save as UTF-8. | [requirements.txt](../requirements.txt) |
| 7 | Stale comments: "ipapi.co" in `test_location_tool.py`'s header and in `settings.py`'s footer docstring. | 2 files |

### Priority 1 — finish Phase 2 (the real remaining scope)

**6.1 Build `app/tools/disaster_tool.py`** (Phase 2, step 6 — the only missing file)

```python
async def get_disaster_info(lat: float, lon: float, radius_km: int = 300) -> dict:
    # {"safe": bool,
    #  "active_events": [{"type": "flood", "severity": "orange", "title": "...",
    #                     "source": "GDACS", "distance_km": 42.0}]}
```
- Three keyless, **global** sources (Sri Lanka needs no special handling): NASA EONET
  (`/api/v3/events?status=open&days=20`), USGS FDSN
  (`format=geojson&maxradiuskm=…&minmagnitude=4`), GDACS (`https://www.gdacs.org/xml/rss.xml`, GeoRSS).
- `asyncio.gather(..., return_exceptions=True)`, haversine-filter each by distance from `(lat, lon)`,
  merge into one severity-ranked list.
- Severity: GDACS's own green/orange/red where present; USGS by magnitude (≥ 6 → orange, ≥ 7 → red);
  EONET by category.
- GDACS is XML — `xml.etree.ElementTree` is enough, no new dependency.
- §8 failure mode: one source down → merge the other two. All three down →
  `{"safe": True, "active_events": [], "note": "disaster data unavailable"}`. **Never block the plan.**

**6.2 Wire it into `_context_node` with the real gather**

```python
weather, disaster = await asyncio.gather(
    get_weather(lat, lon, dates),
    get_disaster_info(lat, lon),
)
```

**6.3 Implement §2's slot-filling rules** — the missing half of step 7

- No `destination` → set a clarification flag and route to `respond` with an actual **question**
  ("Which destination would you like to visit?"), not an error string.
- Destination only → `duration_days = 1` (activities + travel time), and pull
  `interests` / `travel_style` / `budget` from `db_tool.get_user_profile(state.user_id)`. That
  function exists in the mock and is currently called from nowhere.
- No start location after GPS → IP → same clarification path, phrased as a question.

**6.4 Add a clarification branch to the graph**

Today every failure lands in `respond`'s `"Sorry, I ran into an issue: …"` string. Separate
`state.errors` (something broke) from a new `state.clarification_needed` (we need to ask the user
something) so the chat UI gets a real question. This is exactly what §12's third scripted case
(`"Plan a trip"` → asks for a destination) is testing.

**6.5 Populate `candidate_*` from `db_tool`**

Nothing currently calls `get_hotels` / `get_restaurants` / `get_attractions` / `get_events`. Whether
that lives in a new node before `recommend`, or inside the Recommendation Agent itself, is a
question for Member B — but today the stub agent slices four empty lists, so the whole
recommendation path is untested against data. Worth resolving in the next sync, since it sits right
on the §9 seam.

### Priority 2 — Phase 4 integration (needs Member B's agents on this branch)

1. Pull in the real `RecommendationAgent` / `PlannerAgent` and swap the stub class bodies in
   [orchestrator.py:21-36](../app/core/orchestrator.py#L21-L36). The `add_node`/`add_edge` wiring
   shouldn't need to change — that's why the stubs are `BaseAgent` subclasses.
2. Confirm both agents return `AgentResult(success, state, error)` as `app/core/result.py` defines it
   — the known seam already flagged in [member_B.md](../member_B.md).
3. Confirm the `db_tool.py` §4 dict shapes still match what the agents expect once the mock is
   swapped for real Supabase queries.
4. Re-run `test_orchestrator.py`, expect data-shape mismatches, fix with Member B on standby.
5. Verify §2's defaults end-to-end against §12's scripted inputs.

### Priority 3 — Phase 5 finish

1. **Reconcile `app/api/trip.py` with §7** — pick one: rename the code's fields to §7 (`message`,
   add `session_id`, add `weather` + `disaster_warnings` to the response, drop `completed_steps`
   from the public shape), or amend BUILD_PLAN §7 to match the code. Write the decision down; NestJS
   in Phase 7 gets built against it.
2. **Harden the OAuth callback** — replace `state=user_id` with a signed, single-use state token.
3. **Real tests.** Convert every `test_*.py` to `pytest` with assertions, mock the HTTP layer
   (`respx` or `httpx.MockTransport`) so nothing hits a live API, and add one failure-mode test per
   tool from the §8 matrix.
4. Add CORS + logging config to `main.py`.

### Priority 4 — Phase 6

1. Apply §8's caching TTLs — 30–60 min per `(lat, lon)` rounded to 2 decimals, for weather and
   disaster.
2. Walk the §8 failure matrix tool by tool, confirming graceful degradation rather than a crash.
3. Finish policy-guard hardening, or consciously accept the blocklist for the capstone demo and note
   that decision.
4. Persist OAuth tokens in the real `google_oauth_tokens` table once Member B lands it.
5. Build the demo script / walkthrough for the mentor.

### Priority 5 — Phase 7 (only after 6 is solid)

Per §10, Member A's suggested half: the NestJS backend (JWT auth, user/trip endpoints proxying to
`POST /trip-plan`, admin endpoints) and the Admin Panel's pending-listings approve/reject view — the
UI for the `is_verified` flag the data pipeline sets to `false`.

---

## 7. Open items — waiting on / owed

| Item | Direction | Status |
|---|---|---|
| `google_oauth_tokens` table + persistent `get_stored_credentials` / `save_credentials` | A → B | Requested in [member_B.md](../member_B.md); not blocking (falls through to `[]`) |
| Recommendation + Planner agents matching `AgentResult(success, state, error)` | A ↔ B | Shape documented for B; confirm at Phase 4 |
| Who populates `candidate_*` — an orchestrator node or the Recommendation Agent | A ↔ B | Open; sits on the §9 seam |
| Gemini model name (`gemini-3.6-flash`, not `2.5`) | A → B | Noted in [member_B.md](../member_B.md) |
| §7 API contract vs. the built endpoint | A owns | Pick one, write it down |
| OAuth `state`-parameter CSRF hole | A owns | Fix before anything public-facing |
| Policy guard hardening | A owns | Draft, self-flagged non-final |
| `disaster_tool.py` | A owns | Not started |
| §2 slot-filling defaulting rules + clarification branch | A owns | Not started |

---

## 8. One-paragraph summary for the mentor

Member A's Orchestrator track is substantially built. The LangGraph state machine specified in
BUILD_PLAN §6 runs end to end with all rule-based nodes (validation, policy guard), the live
per-request tools (location with GPS → IP fallback, Google Calendar free/busy, OpenWeather
forecast), LLM-assisted slot filling on Gemini, and a working FastAPI surface with `POST /trip-plan`
plus a complete Google OAuth consent flow — the last two landing ahead of their scheduled phase.
Four things remain before this track is genuinely complete: the disaster tool (EONET / USGS / GDACS)
is the one file never started; §2's slot-filling defaulting rules — user-profile fallback and asking
the user for a missing destination — are specified but not implemented, and the graph has no
ask-back branch for them to use; a small set of correctness bugs means weather is currently never
fetched on the no-calendar path; and the test suite is print-scripts making live API calls rather
than mocked assertions. Phase 4 integration is queued behind Member B's Recommendation and Planner
agents arriving on this branch — the `recommend` and `plan` nodes are already shaped as `BaseAgent`
subclasses so that swap is a body replacement, not a rewiring.
