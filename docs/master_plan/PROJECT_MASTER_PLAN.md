# SmartJourney — Project Master Plan

**Written:** 2026-09-02 · **Owner repos:** `ai-backend/` (Python/FastAPI), `backend/` (NestJS — repo exists, not yet scaffolded)
**Status:** plan. Nothing in this document is implemented yet.
**Supersedes for planning purposes:** `docs/build_plan/BUILD_PLAN.md`, `docs/build_plan/NEXT_STEPS.md` (kept for history — they describe the system as built, this describes the system as it must become).

---

## 0. How to read this plan

| File | Contains |
|---|---|
| **`PROJECT_MASTER_PLAN.md`** (this file) | Current state, target architecture, decisions + rationale, your 12 concerns mapped to fixes, the phase plan, and the definition of "done". **Start here.** |
| [`AGENT_ARCHITECTURE.md`](AGENT_ARCHITECTURE.md) | The ReAct loop, agent contracts, the full tool catalog, the graph, session memory, degradation matrix. |
| [`DETERMINISM_AND_VALIDATION.md`](DETERMINISM_AND_VALIDATION.md) | LLM config, prompt centralization, structured-output schemas, output validators, and the **exact scoring/budget math** the agents must use instead of judging for themselves. |
| [`DATA_PLATFORM.md`](DATA_PLATFORM.md) | Docker, full SQL schema, district/place resolution (replaces the hardcoded file), ingestion connectors, scheduler, cost model, mock-removal checklist. |
| [`../../../backend/docs/BACKEND_ALIGNMENT.md`](../../../backend/docs/BACKEND_ALIGNMENT.md) | What changes in the NestJS plan because of this one. |

Implementation order is **§5 Phase plan**. Every phase has a gate — do not start the next one until the gate passes.

---

## 1. Where the project actually is today

Read from the code, not from the older docs. This is the honest baseline.

| Area | Reality |
|---|---|
| **Graph** | `app/core/orchestrator.py` is a **fixed linear LangGraph**: validate → policy → slot_fill → location → calendar → context → recommend → plan → respond. Conditional edges exist only for early bail-outs. |
| **ReAct** | **Absent.** No tool-calling loop, no Thought/Action/Observation, no re-planning. Tools are called unconditionally in hardcoded order by graph nodes. |
| **Agents** | Two: `RecommendationAgent` (which also builds the itinerary) and `PlanningAgent` (only runs as a fallback when the first didn't produce an itinerary). Both are single-shot "dump JSON into a prompt, parse text back" calls. Neither uses tools. |
| **Determinism** | `llm_temperature = 0.2` globally. `slot_filling.py` is the **only** place using `with_structured_output`; both agents parse free-text JSON with a hand-rolled ```` ``` ```` stripper that silently returns `{}` on a parse failure. |
| **Ranking** | Done entirely by the LLM inside the prompt. No Python scoring exists anywhere in the repo. Verified consequence: BUILD_PLAN §12 case 1 picked a `$$$$` hotel on a `$500` budget and produced a `$1,400` plan. |
| **Mock data** | `app/tools/db_tool.py` carries ~90 lines of hardcoded `_MOCK_HOTELS` / `_MOCK_RESTAURANTS` / `_MOCK_ATTRACTIONS` / `_MOCK_EVENTS` for 4 towns, and **every DB query silently falls back to them** on failure. `RecommendationAgent` even hardcodes `"2026-08-20"`–`"2026-08-23"` as the event window. |
| **Districts** | `app/data/sri_lanka_districts.py` — 25 districts hardcoded as a Python literal, with an `assert len(...) == 25`. Used by both ingest jobs and the mock backfill. Cannot resolve a town ("Ella") to its district (Badulla). |
| **Prompts** | Two files in `app/prompts/`, but `slot_filling.py` holds its own `_SYSTEM_PROMPT` inline, and the two agent prompts overlap heavily (planning rules duplicated in both). |
| **Validation** | `validators.py` checks 4 input fields. **Nothing validates any LLM output.** A malformed model response becomes `{}` → empty itinerary → generic error string. |
| **Session memory** | `app/utils/session_store.py` → a local `trip_sessions.json` file. Not multi-instance safe, dies with the container, carries 20 fields of full state including every candidate list. |
| **Database** | Schema does **not exist yet**. `backend/docker-compose.yml` defines a PostGIS container, but there is no migration, no seed, no Prisma project — `backend/` contains only compose + docs. So today the system runs 100% on mocks. |
| **Ingestion** | `overpass_ingest.py` (listings, monthly) and `events_ingest.py` (Ticketmaster, weekly) exist and write via `postgres_writer.py` — but into tables that don't exist. Events ingest is documented as returning **zero events for Sri Lanka**. |
| **RAG** | FAISS + sentence-transformers, indexing that day's candidate pool then retrieving from it — i.e. re-ranking a list it was just handed, per request. |
| **Model** | `settings.llm_model = "gemini-3.6-flash"`. |
| **Tests** | 127 passing, no network, no keys — but they test the mock paths, so they will not protect the rewrite. |

**The core problem, stated plainly:** the system is a well-plumbed pipeline with no data behind it and no decision logic inside it. The LLM is doing the work that Python should do, over data that doesn't exist. Everything below follows from fixing those two things — in that order.

---

## 2. Target architecture

### 2.1 The workflow, as you specified it, made concrete

```
                                     user message
                                          │
                          ┌───────────────▼───────────────┐
                          │ 1. validate + policy guard    │  pure Python, no LLM
                          └───────────────┬───────────────┘
                                          │
                          ┌───────────────▼───────────────┐
                          │ 2. slot filling               │  LLM #1 · structured output
                          │    (+ profile defaults)       │  → clarify & stop, if needed
                          └───────────────┬───────────────┘
                                          │
        ┌─────────────────────────────────▼─────────────────────────────────┐
        │ 3. ORCHESTRATOR AGENT  —  ReAct loop (max 6 steps)                │  LLM #2
        │                                                                   │
        │    Thought → Action → Observation → Thought → …                   │
        │    tools: geocode_place · resolve_district · resolve_start_location│
        │           get_calendar_free_days · get_weather · get_disaster_info │
        │                                                                   │
        │    → emits TripContext (coords, district_id, chosen date window,   │
        │      per-day weather, safety flags)                               │
        └─────────────────────────────────┬─────────────────────────────────┘
                                          │
        ┌─────────────────────────────────▼─────────────────────────────────┐
        │ 4. RECOMMENDATION AGENT — ReAct loop (max 5 steps)                │  LLM #3
        │    tools: db_search_listings · db_search_events · travel_matrix   │
        │           score_candidates  ◄── deterministic Python ranker       │
        │    LLM chooses WHAT to query and writes the "reason" text.        │
        │    LLM never orders the list — score_candidates does.             │
        └─────────────────────────────────┬─────────────────────────────────┘
                                          │
        ┌─────────────────────────────────▼─────────────────────────────────┐
        │ 5. PLANNER AGENT — ReAct loop (max 5 steps)                       │  LLM #4
        │    tools: estimate_costs · build_day_plan · check_budget          │
        │           travel_matrix                                           │
        │    LLM sets pace/theme/constraints. build_day_plan does the        │
        │    routing, timing and sequencing deterministically.               │
        └─────────────────────────────────┬─────────────────────────────────┘
                                          │
                          ┌───────────────▼───────────────┐
                          │ 6. ORCHESTRATOR — result       │  pure Python
                          │    validation (L0–L2)          │  ≤5 ms
                          │    ├ pass → respond            │
                          │    ├ fail → 1 repair retry ────┼──► back to 5
                          │    └ fail again → deterministic fallback plan
                          └───────────────┬───────────────┘
                                          │
                          ┌───────────────▼───────────────┐
                          │ 7. response text               │  LLM #5 (optional,
                          │    (narration only)            │  falls back to template)
                          └───────────────┬───────────────┘
                                          ▼
                                        user
```

**Every fact in the output comes from Postgres. Every ordering decision comes from Python. The LLM decides what to look up, and explains what was chosen.** That sentence is the whole design.

### 2.2 What sits behind it

```
     ai-backend (FastAPI)                        backend (NestJS, later)
   ┌───────────────────────┐                   ┌────────────────────────┐
   │ agents · tools · ReAct│                   │ auth · chat · trips    │
   └───────────┬───────────┘                   └───────────┬────────────┘
               │  asyncpg                                  │  Prisma
               └────────────────┬─────────────────────────-┘
                                ▼
                 ┌──────────────────────────────┐
                 │  PostgreSQL 16 + PostGIS      │  ← Docker (backend/docker-compose.yml)
                 │  districts w/ boundaries      │
                 │  listings · events · costs    │
                 │  ai_session · travel_time     │
                 └──────────────▲───────────────┘
                                │ nightly upserts
                 ┌──────────────┴───────────────┐
                 │  ingestion connectors         │  Overpass · Nominatim · Wikidata
                 │  (APScheduler, idempotent)    │  Foursquare · Booking · Ticketmaster
                 └──────────────────────────────┘
```

---

## 3. Decisions taken

Recorded with rationale so they don't get re-argued later. Items marked **⚠ needs your call** are the only ones I did not settle myself.

| # | Decision | Choice | Why |
|---|---|---|---|
| D1 | **ReAct vs. determinism** — they pull against each other | **Bounded ReAct over tools; zero LLM judgement on rankings.** The LLM reasons about *which* tool to call and *when it has enough*; every comparison, sort, cost and time calculation is Python. | Gives you the ReAct behaviour you asked for (#2) without giving up determinism (#6, #10). See [`AGENT_ARCHITECTURE.md §2`](AGENT_ARCHITECTURE.md). |
| D2 | **The deterministic ranker is exposed as a tool** (`score_candidates`) rather than hidden in the node | The agent's action space *includes* the ranker, so it structurally cannot produce an ordering without calling it. | Makes "the LLM must not rank" enforceable, not just instructed. |
| D3 | **Deterministic fallback planner** — a pure-Python path that builds a valid itinerary with no LLM at all | Ships as a first-class component, not an error handler. Triggered when the planner LLM fails validation twice, or when quota is exhausted. | This is the single biggest lever on your "90% end-to-end, no errors" target (#9). It converts most LLM failures into a slightly-less-eloquent success. |
| D4 | **Districts** | Delete `sri_lanka_districts.py`. Districts become a **table**, seeded once from OSM (Overpass `admin_level=5` relations) with real **boundary polygons**; runtime resolution is `ST_Contains(boundary, point)` — one indexed local query, no per-request API call. Place-name→coords via Nominatim, **cached in `geo_resolution` table**. | Answers #1 properly. Data in a table is not "hardcoded"; and point-in-polygon beats reverse-geocoding on latency, cost and determinism. Google Maps Geocoding is supported as an optional higher-quality provider but is **not required** (it needs billing). See [`DATA_PLATFORM.md §4`](DATA_PLATFORM.md). |
| D5 | **Mock data** | Removed entirely — mocks, fallbacks, and the "fall back on DB error" behaviour. `db_tool` returns `[]` and records an explicit typed error. | #3. But sequencing is critical: **mocks come out only after ingestion is proven** (Phase 3, gated on Phase 2). Removing them first would leave the system with nothing. |
| D6 | **LLM model** | **`gemini-3.5-flash-lite`** (your #4) — ✅ **verified 2026-09-02** as a real model id with free-tier access on Google's pricing page. `temperature=0`, `top_p=1`, `candidate_count=1`, per-agent token caps. Free limits are reported at ~15 RPM / ~1,500 RPD, but Google **no longer publishes** them — read your account's real ceiling at [aistudio.google.com/rate-limit](https://aistudio.google.com/rate-limit) during Phase 0. | Cheaper + higher daily quota, which matters because ReAct multiplies calls per request. At ~12 API calls per trip plan, 1,500 RPD ≈ **125 plans/day** — ample for development, testing and demos. Token budgets in [`DETERMINISM_AND_VALIDATION.md §2`](DETERMINISM_AND_VALIDATION.md). |
| D6b | **Provider failover** | `app/core/llm.py` returns a **provider-agnostic chain**: `gemini-3.5-flash-lite → gemini-3.6-flash → groq/openai/gpt-oss-120b`. Groq is OpenAI-compatible, free with no card, and supports tool calling. Layered under D3's zero-LLM fallback. | The realistic failure isn't "Gemini is down", it's **quota exhausted at 2pm on demo day**. Alternatives surveyed 2026-09-02: **Cerebras** (1M tokens/day) rejected — 8,192-token context cap can't hold the candidate payload; **OpenRouter** rejected — 1,000 RPD needs a one-time $10 purchase; **Mistral** rejected — ~1 RPM free tier can't sustain ReAct loops. Groq's 8K TPM makes it a poor *primary* (one recommendation call ≈ 4–6K tokens ≈ a minute's budget) but a fine *failover*. |
| D6c | **Spend fewer calls before buying more quota** | Response narration uses the template by default (LLM #5 becomes opt-in); identical `/trip-plan` requests are cached; follow-ups that only reshape the plan skip the recommendation agent entirely. | Worth more than a second provider, and it lowers latency at the same time. The skip rule is already specified in [`AGENT_ARCHITECTURE.md §5`](AGENT_ARCHITECTURE.md). |
| D7 | **Prompts** | All prompts move into `app/prompts/` behind a registry (`PromptSpec(name, version, template, output_schema)`), including `slot_filling`'s inline one. A test greps the codebase and fails if a prompt string is defined outside `app/prompts/`. | #5, and the grep test is what keeps it true six weeks from now. |
| D8 | **Structured output everywhere** | Every LLM call uses `with_structured_output(<PydanticModel>)`. The hand-rolled `_parse_json_response` in both agents is deleted. | #6. Removes the silent `{}`-on-parse-failure path, which is currently an invisible failure mode. |
| D9 | **Output validation** | Three cheap layers (schema → referential → business rules), pure Python, **<5 ms budget**, no extra LLM call on the happy path. One bounded repair retry, then D3's fallback. | #7. Detail in [`DETERMINISM_AND_VALIDATION.md §5`](DETERMINISM_AND_VALIDATION.md). |
| D10 | **External MCP servers** | **Not on the request path.** Keep in-process tools. | #8. Honest assessment: every candidate MCP server here (OSM, weather, fetch) wraps an HTTP API you already call directly in ~40 lines. Adding MCP means a second runtime (node/uvx) per server, an extra IPC hop inside a latency budget that's already 5–20 s, non-deterministic tool schemas you don't control, and a new class of "the sidecar died" failures — against a goal of *fewer* moving parts and 90% reliability. Reconsider only for capabilities you'd otherwise have to build (e.g. a real routing engine), and only after the gate in Phase 8 passes. |
| D11 | **RAG** | ✅ **Settled 2026-09-02: demoted, not deleted.** It is *not* a stated requirement — `BUILD_PLAN.md` never mentions it; it was added later and `modification_thisuri.md` §4 records it as "fully implemented but never used" (and carrying a real bug — unnormalized FAISS vectors). It earns its keep only for a future **travel-knowledge Q&A** feature ("do I need to cover my shoulders at the Temple of the Tooth?") — unstructured knowledge no table holds. That needs a curated corpus nobody has written yet, so it is not a Phase 1–8 concern. Preserving it behind a flag costs ~nothing; rebuilding it later would cost days. **Decision review at the end of Phase 8: if no corpus work has started, delete it then.** Original reasoning below. — **Demoted, not deleted.** Removed from the candidate-selection path (it currently re-ranks a list it was just handed — the deterministic scorer does that job better and reproducibly). Kept behind `enable_rag=False` for *knowledge documents* (destination guides / safety tips) which is the one job it's actually suited to. `faiss-cpu` + `sentence-transformers` move to an optional requirements extra. | #9 — that's ~2 heavy dependencies and a per-request embedding pass off the critical path. Kept rather than deleted because RAG is named in your SRS/SAD deliverables; you keep the ability to demo it. **⚠ Confirm** you're comfortable with it being off by default. |
| D12 | **Session memory** | Postgres `ai_session` table (JSONB state + ReAct trace + `expires_at`), replacing `trip_sessions.json`. Carried-over state slims from 20 fields to 9 — candidate lists are re-queried, not carried. | The JSON file is the last local-file fallback and is explicitly flagged in three existing docs. Carrying 4 candidate arrays per session is what makes that file grow unboundedly. |
| D13 | **Schema ownership / who migrates first** | AI backend cannot wait for NestJS. Canonical schema lives in **`backend/db/migrations/*.sql`** (plain SQL, applied by a `migrate` script + compose init). NestJS later runs `prisma db pull` to introspect it rather than owning it. | Unblocks you now. Reverses BACKEND_PLAN's assumption that Prisma owns the schema — see [`BACKEND_ALIGNMENT.md`](../../../backend/docs/BACKEND_ALIGNMENT.md). Also sidesteps the Prisma-vs-`Unsupported(geography)` friction that plan already flagged as its top risk. |
| D14 | **Currency** | Base currency **LKR**, stored explicitly in a `currency` column on every money field. One configured `USD_LKR_RATE` in settings for display only; no live FX API. | Long-standing open item in three docs. SRS mockups are LKR; the data (OSM, local costs) is LKR-native. Booking.com prices are converted at ingest. |
| D15 | **Concurrency / scale** | Explicitly out of scope this round, per your #11. No rate limiting, no queueing, single instance. Recorded as a known limitation, not an oversight. | Nothing in this plan blocks adding it later; `ai_session` in Postgres is in fact the prerequisite. |
| D16 | **Feature deletions** | `check_pickme_coverage()` (returns `True` unconditionally), `get_transit_info()` (returns a constant), Yelp settings (no Sri Lanka coverage), the duplicate root-level `test_recommendation_agent.py`, `pinecone`/`vector_store_type` config, unused feature flags. | #9. Each is a stub that costs reading time and implies a capability that isn't there. |

---

## 4. Your concerns → where each one is resolved

| # | Your concern | Resolution | Where | Phase |
|---|---|---|---|---|
| — | The workflow order | Implemented exactly as drawn, with the orchestrator re-entered for result validation | [`AGENT_ARCHITECTURE.md §1`](AGENT_ARCHITECTURE.md) | 6 |
| — | Orchestrator uses weather/disaster/calendar/location/geocode tools to establish lat-lon + free days first | ReAct loop with those 5 tools + `resolve_district`; free days drive the weather/disaster date window | [`AGENT_ARCHITECTURE.md §3.2`](AGENT_ARCHITECTURE.md) | 6 |
| — | Budget + itinerary should be tools too | They are: `estimate_costs`, `check_budget`, `build_day_plan`, `travel_matrix`, `score_candidates` | [`AGENT_ARCHITECTURE.md §4`](AGENT_ARCHITECTURE.md) | 4, 6 |
| — | All data from the internal database | `db_search_*` are the only sources of place data; no live third-party call in the request path except weather/disaster/geocode | [`DATA_PLATFORM.md §5`](DATA_PLATFORM.md) | 2–3 |
| — | Website APIs → DB via scheduler, with schema, in Docker | Connector framework + `data_source_run` audit table + nightly APScheduler + full DDL + compose | [`DATA_PLATFORM.md §§1,2,5,6`](DATA_PLATFORM.md) | 1–2 |
| **1** | Hardcoded districts | Table + OSM boundaries + `ST_Contains` + cached geocoding. File deleted. | [`DATA_PLATFORM.md §4`](DATA_PLATFORM.md) | 1 |
| **2** | Agents don't ReAct | Bounded ReAct loops in all three agents, with traces persisted | [`AGENT_ARCHITECTURE.md §2`](AGENT_ARCHITECTURE.md) | 6 |
| **3** | Mock data everywhere | Full removal checklist (11 sites), DB-only, explicit typed errors instead of silent fallback | [`DATA_PLATFORM.md §9`](DATA_PLATFORM.md) | 3 |
| **4** | Model → gemini 3.5 lite, more daily tokens | Settings change + verification step + per-agent token budgets + fallback chain | [`DETERMINISM_AND_VALIDATION.md §2`](DETERMINISM_AND_VALIDATION.md) | 0 |
| **5** | Centralize prompts in `app/prompts` | Prompt registry + versioning + a lint test that fails on stray prompt strings | [`DETERMINISM_AND_VALIDATION.md §3`](DETERMINISM_AND_VALIDATION.md) | 5 |
| **6** | Deterministic, structured (JSON) outputs | `temperature=0` + `with_structured_output` on all 5 calls + constrained prompts + fixed tool results | [`DETERMINISM_AND_VALIDATION.md §§1–4`](DETERMINISM_AND_VALIDATION.md) | 5 |
| **7** | Fast validator on every LLM/agent output | L0/L1/L2 validators, <5 ms, one repair retry, then deterministic fallback | [`DETERMINISM_AND_VALIDATION.md §5`](DETERMINISM_AND_VALIDATION.md) | 5 |
| **8** | Use free external MCPs where better | Assessed and **declined for the request path**, with reasoning; revisit gate defined | D10 above | — |
| **9** | Cut features, get to 90% working end-to-end | 6 deletions (D16) + RAG demotion (D11) + deterministic fallback (D3) + a measured acceptance suite | §6 below | 0, 8 |
| **10** | Score-based choice (distance/cost/rating/preference), not just budget | Full weighted-scoring engine with per-category weights, normalization, Bayesian rating shrinkage, budget-aware cost scoring, deterministic tie-breaks | [`DETERMINISM_AND_VALIDATION.md §6`](DETERMINISM_AND_VALIDATION.md) | 4 |
| **11** | Concurrency later | Accepted — D15 | — | — |
| **12** | DB schema for AI backend, in Docker | Full DDL, 14 tables, PostGIS, compose with healthcheck + Redis, migration runner, seed | [`DATA_PLATFORM.md §§1–3`](DATA_PLATFORM.md) | 1 |

---

## 5. Phase plan

Estimates are focused working days for one developer. Total **≈ 12.5 days**. Phases 1→4 are the critical path; 5 can overlap 4.

### Phase 0 — Cleanup and config · 0.5 d
**Phase 0a — API configuration.** Do this first; it's the prerequisite for everything and needs no code. Follow [`API_SETUP.md`](API_SETUP.md), then run `python scripts/check_apis.py` until every **required** row is green.

1. `settings.py`: `llm_model` → `gemini-3.5-flash-lite`; `llm_temperature` → `0`; add `llm_provider_chain`, `groq_api_key`, `enable_response_narration`, `usd_lkr_rate`, `react_max_steps`, `ors_api_key`, `foursquare_api_key`; delete `vector_store_type`, `yelp_fusion_api_key`, dead feature flags.
2. `app/core/llm.py` — the single construction point + provider chain (D6b).
3. Delete: `check_pickme_coverage`, `get_transit_info`, root `test_recommendation_agent.py`, `trip_sessions.json`.
4. Move `faiss-cpu` + `sentence-transformers` into `requirements-rag.txt`; default `enable_rag=False`.

**Gate:** `scripts/check_apis.py` shows every required key green, app boots, `pytest` green (some tests will need the deleted-stub assertions removed), `/api/health` OK.

### Phase 1 — Database, in Docker · 1.5 d
1. `backend/docker-compose.yml`: keep PostGIS, add `redis`, add `./db/migrations` mount + init, keep the healthcheck.
2. Write `backend/db/migrations/0001_init.sql` — the full DDL from [`DATA_PLATFORM.md §2`](DATA_PLATFORM.md) (14 tables, PostGIS, all indexes).
3. Write `backend/db/migrate.py` (or `.sh`) — idempotent, tracks applied files in `schema_migration`.
4. Write `app/data/seed_districts.py` — pulls the 25 district **relations with boundary geometry** from Overpass, upserts into `district`. Run once.
5. Delete `app/data/sri_lanka_districts.py`; repoint `overpass_ingest`/`events_ingest` at the table.

**Gate:** `docker compose up -d` healthy; `SELECT count(*) FROM district` = 25 **and** `SELECT name FROM district WHERE ST_Contains(boundary::geometry, ST_SetSRID(ST_Point(81.0467, 6.8658),4326))` returns `Badulla` (that's Ella). This single query is the proof that #1 is fixed.

### Phase 2 — Ingestion connectors + scheduler · 2 d
1. `app/data/connectors/` — `base.py` (the `Connector` protocol), then `osm_listings.py`, `wikidata_enrich.py`, `foursquare_enrich.py`, `booking_prices.py`, `ticketmaster_events.py`.
2. `app/data/pipeline.py` — runs `district × connector`, writes `data_source_run` rows, `--source`/`--district` CLI flags, idempotent upsert on `(source, external_ref)`.
3. `app/scheduler.py` — nightly 02:30 Asia/Colombo full run; per-source cadence from the registry table.
4. Seed `cost_reference` (district × category × price_level → typical LKR cost) from the CSV in [`DATA_PLATFORM.md §6`](DATA_PLATFORM.md).
5. `travel_time` cache table + `app/tools/routing_tool.py` (OpenRouteService free tier, haversine fallback).

**Gate:** a full pipeline run populates ≥ 2,000 verified-able listings across 25 districts, ≥ 1 event source runs clean, `data_source_run` shows all-green, and a re-run changes `rows_upserted` but not row count (idempotency proof).

### Phase 3 — Kill the mocks · 1 d
1. Rewrite `db_tool.py` against the new schema: `search_listings(district_id, category, tags, bbox, limit)`, `search_events(district_id, date_from, date_to)`, `get_user_profile`. Returns `[]` + a typed `DataUnavailable` error; **no fallbacks**.
2. Delete all `_MOCK_*` dicts, `_get_mock_data`, `_MOCK_TOWN_COORDS`, and every `except → mock` branch.
3. `app/tools/geo_tool.py` — `resolve_place(name) -> {lat, lon, district_id}` using `geo_resolution` cache → Nominatim → optional Google.
4. Rewrite the tests that asserted mock behaviour to run against a seeded test database (docker-compose `db-test`).

**Gate:** `grep -ri "mock" app/` returns nothing outside `tests/`; a `/trip-plan` request for Ella returns real Badulla-district listings.

### Phase 4 — The deterministic core · 2 d
This is the part that makes the system yours rather than the model's. No LLM involved.
1. `app/core/scoring.py` — the weighted scorer, exactly per [`DETERMINISM_AND_VALIDATION.md §6`](DETERMINISM_AND_VALIDATION.md).
2. `app/core/budget.py` — allocation split, per-item cost estimation, feasibility check, `check_budget`.
3. `app/core/itinerary.py` — `build_day_plan`: weather/disaster filtering, nearest-neighbour routing, meal slots, dwell times, opening hours.
4. `app/core/fallback.py` — D3's zero-LLM planner (scorer + budget + itinerary + template narration).
5. Unit tests with fixed fixtures asserting **exact** output — this module must be bit-reproducible.

**Gate:** `fallback.build_plan(fixture)` returns a byte-identical itinerary across 10 runs, respects a tight budget, and puts nothing outdoors on a `rain_probability = 0.8` day.

### Phase 5 — Prompts, structured output, validators · 1 d *(can overlap Phase 4)*
1. `app/prompts/` registry + 6 prompt modules; move `slot_filling`'s inline prompt in.
2. `app/models/schemas.py` — the Pydantic output models for all 5 LLM calls.
3. `app/core/output_validator.py` — L0/L1/L2 + repair-prompt assembly.
4. The prompt-lint test.

**Gate:** every `ainvoke` in the codebase goes through `with_structured_output`; a deliberately corrupted model response is caught by L1 within 5 ms and triggers exactly one repair attempt.

### Phase 6 — ReAct agents + graph rewrite · 2.5 d
1. `app/core/react.py` — the shared bounded ReAct executor (step cap, tool budget, trace capture, timeout).
2. Rewrite `orchestrator.py`: validate → policy → slot_fill → **orchestrator ReAct** → recommend ReAct → plan ReAct → **result validation** (+ repair edge) → respond.
3. `app/agents/` — `orchestrator_agent.py`, `recommendation_agent.py`, `planner_agent.py` (replacing `app/workflows/`).
4. Bind tools via Gemini function calling; the tool catalog from [`AGENT_ARCHITECTURE.md §4`](AGENT_ARCHITECTURE.md).

**Gate:** a request where geocoding fails on the first attempt shows the orchestrator **retrying with a different tool** in its trace — that's the observable proof ReAct is real and not a linear chain wearing a costume.

### Phase 7 — Session memory + API contract · 0.5 d
1. `ai_session` table; rewrite `session_store.py` against it; TTL cleanup job.
2. `/trip-plan` response gains `currency`, `plan_source` (`llm` | `fallback`), `data_freshness`; `completed_steps` becomes `trace` under `DEBUG`.
3. Update `demo/index.html` and `backend/docs/AI_BACKEND_ENDPOINTS.md`.

**Gate:** follow-up turns work after a container restart.

### Phase 8 — Hardening to the 90% target · 1.5 d
1. `scripts/e2e_check.py` — the 12 golden scenarios in §6, run against a live stack, prints a pass rate.
2. Fix whatever it finds. Re-run until ≥ 90%.
3. Determinism run: each scenario 3× → identical selected ids and itinerary structure.

**Gate:** §6's table, filled in with real numbers.

### Phase 9 — NestJS alignment · not on this critical path
Per [`BACKEND_ALIGNMENT.md`](../../../backend/docs/BACKEND_ALIGNMENT.md). Start after Phase 3; `prisma db pull` against the live schema.

---

## 6. Definition of done — the 90% target, made measurable

"90% working end-to-end with no errors" needs a number attached or it can't be claimed. `scripts/e2e_check.py` runs these 12 scenarios against a live stack and scores each pass/fail.

| # | Scenario | Passes when |
|---|---|---|
| 1 | `"Plan a 3-day trip to Kandy, budget LKR 60000, culture and history"` | 3 days, every item from DB, `estimated_cost ≤ budget`, all items in/near Kandy district |
| 2 | `"Plan a trip to Ella"` | Resolves to Badulla district, defaults to 1 day, returns real listings |
| 3 | `"Plan a trip"` | Clarification question, **zero** tool calls, zero LLM calls after slot-filling |
| 4 | Tight budget: `"5 days in Galle, budget LKR 25000"` | Picks the cheapest feasible set, `budget_notes` explains, does **not** exceed silently |
| 5 | Follow-up: `"make day 2 cheaper"` with `session_id` | Day 1 and 3 unchanged byte-for-byte; day 2 cost strictly lower |
| 6 | Follow-up: `"I'm starting from Polonnaruwa"` | `start_location` updated, distances rescored, itinerary reordered |
| 7 | Rainy destination (mock `rain_probability = 0.8` for day 1) | Day 1 has no outdoor-tagged items |
| 8 | Active red disaster within 50 km | Affected items excluded, warning surfaced in `final_response` |
| 9 | Calendar connected, 3 free days | `trip_dates` come from free days; weather fetched for exactly those dates |
| 10 | Policy: a blocked request | Blocked before any LLM or tool call |
| 11 | Gemini unavailable (bad key) | **Deterministic fallback plan returned**, `plan_source = "fallback"`, HTTP 200 |
| 12 | District with thin data (e.g. Mullaitivu) | Returns what exists + an explicit "limited coverage" note, never invents |

**Target: ≥ 11 / 12 (91.7%).** Plus two non-negotiables that aren't scored — they're gates:
- **Determinism:** each scenario run 3× produces identical selected listing ids and identical itinerary structure. (Free-text `reason`/`notes` strings may vary — assert on ids and ordering only.)
- **No unhandled exception** reaches the HTTP layer in any of the 36 runs.

---

## 7. Risks and open questions

| Risk | Impact | Mitigation |
|---|---|---|
| **Event data for Sri Lanka is genuinely thin.** Ticketmaster returns zero; Eventbrite's search API is dead. | The "local events" pillar of the recommendation agent has little to recommend. | Ingest what exists, add an `admin` event source (NestJS admin CRUD) as the primary path, and make the agent degrade cleanly when a district has no events. **Do not** let the LLM invent events — L1 validation makes that impossible. Be upfront about this in your report; it's a data-availability fact, not a build failure. |
| **Restaurant/attraction pricing is not in OSM.** | Cost scoring has weak inputs for 2 of 4 categories. | `cost_reference` table (district × category × price_level → typical cost), seeded manually and admin-editable. Hotels get real prices from Booking. Documented as an approximation, with the source of each cost surfaced in the API response. |
| **Gemini 3.5 Flash-Lite may be weaker at multi-step tool calling** than Flash. | ReAct loops could thrash or stop early. | Step caps + tool budgets bound the damage; D3's fallback catches the rest. If tool-calling quality proves inadequate, use `gemini-3.6-flash` for the orchestrator agent only and lite for the rest — `llm_model_orchestrator` in D6b's chain already supports per-agent models. |
| **~12 API calls per plan vs. today's 2**, against ~1,500 RPD. | ≈125 plans/day, and heavy testing burns that fast. Latency 5–20 s → possibly 10–30 s. | D6c cuts the count; D6b adds a second free provider; D3 survives total exhaustion. Log RPD consumption per run in Phase 8 so the real number replaces this estimate. |
| **Google revokes calendar refresh tokens every 7 days** while the OAuth app is External + Testing. | Calendar integration would break weekly, and today's code reports it as "no calendar connected" — an indistinguishable, unactionable silent failure. | **Resolved cheaply:** `calendar.freebusy` is a **non-sensitive** scope, so the app can be published to Production **without any verification** — one button, and the timer goes away. Still detect `invalid_grant` and prompt to reconnect (Phase 6, ~20 lines), since a user can revoke consent at any time. Details in [`API_SETUP.md §3.1.3`](API_SETUP.md). |
| **Nominatim usage policy** (1 req/s, attribution required). | Getting blocked mid-demo. | `geo_resolution` cache means a repeat destination never hits it twice; ingestion is rate-limited and runs at night. |
| **Overpass boundary relations are large.** | Slow first seed, big geometries. | Run once, `ST_SimplifyPreserveTopology` at ~50 m tolerance before storing, GiST index. Accuracy far exceeds what district assignment needs. |
| **The existing 127 tests mostly test mock paths.** | False confidence during the rewrite. | Expect to rewrite ~40 of them in Phase 3. Budgeted. |

**Open questions:**
1. ~~Confirm the exact Gemini model id~~ — ✅ **resolved 2026-09-02.** `gemini-3.5-flash-lite` is real and free-tier eligible. See D6.
2. ~~Confirm RAG being off by default~~ — ✅ **resolved 2026-09-02.** Demoted, with a delete-review at the end of Phase 8. See D11.
3. **Foursquare / OpenRouteService keys** — both free-tier, no card. Without Foursquare, ratings come only from OSM (sparse) and `rating_score` degrades to the neutral prior for most listings. Without ORS, distance scoring uses haversine × 1.35 instead of real road time. Both usable-but-worse. Setup steps in [`API_SETUP.md`](API_SETUP.md).

**Note on free tiers:** every provider in D6/D6b may use free-tier prompts to improve their products. Fine for synthetic trip requests; do not route real user PII through them once NestJS auth is live.
