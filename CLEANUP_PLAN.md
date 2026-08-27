# Cleanup Plan — Removing Member A's Accidental Work from `thisuri-copy`

**Branch:** `thisuri-copy` (Member B / Thisuri's working copy)
**Reference plan:** [BUILD_PLAN.md](BUILD_PLAN.md) §9 (work split by agent ownership), §4 (tool contracts)
**Reference for Member A's real files/names:** [MEMBER_A_REPORT.md](MEMBER_A_REPORT.md) (snapshot of her `temp-shaluka` branch, 2026-08-27)
**Status:** Executed on this branch — see §4 for what actually changed.

## 1. What happened

Per BUILD_PLAN.md §9, work was split **by agent ownership**, not by layer:

- **Member A — Orchestrator track**: the router agent (`app/core/orchestrator.py`) and every
  tool it calls directly with no DB dependency — policy guard, location, calendar, weather,
  disaster, slot-filling, plus the FastAPI layer.
- **Member B — Recommendation + Planner + Data track**: `db_tool.py`, the Supabase schema, the
  daily ingestion pipeline, and the two DB-backed agents (Recommendation, Planner).

On this branch, `app/workflows/` contained a **second, independent implementation of Member A's
entire Orchestrator track** — `orchestrator.py`, `policy_agent.py`, `calendar_agent.py`, and
`context_agent.py` (a merged weather+disaster agent). These duplicate Member A's scope under
different file paths/class-based designs than what she actually built on `temp-shaluka` (see
MEMBER_A_REPORT.md §3.6–§3.11: her tools are plain async functions in `app/tools/*.py` and
`app/utils/policy_guard.py`, not `BaseAgent` subclasses in `app/workflows/*.py`). Keeping both
would mean two conflicting orchestrators merging into the same repo.

Member B's **real** scope — `db_tool.py`, `recommendation_agent.py`, `planning_agent.py`, the
`app/data/*` ingestion pipeline, `app/rag/*`, `app/prompts/*`, `app/scheduler.py` — never actually
depended on the duplicated files for its own logic. The only coupling was `app/workflows/orchestrator.py`
importing the other three, and `main.py` importing the orchestrator to run the `/api/plan-trip`
endpoint. That made the removal a clean cut.

## 2. Files removed (Member A's scope, duplicated by accident)

| File | Why it's Member A's scope | Her real equivalent (per MEMBER_A_REPORT.md) |
|---|---|---|
| `app/workflows/orchestrator.py` | The router/LangGraph state machine — BUILD_PLAN §6/§9 names this Member A's | `app/core/orchestrator.py` |
| `app/workflows/policy_agent.py` | Rule-based guardrail on `user_input` — BUILD_PLAN §9 step 2 | `app/utils/policy_guard.py` |
| `app/workflows/calendar_agent.py` | Google Calendar free/busy lookup — BUILD_PLAN §9 step 4 | `app/tools/calendar_tool.py` |
| `app/workflows/context_agent.py` | Weather (OpenWeather) + disaster (EONET/USGS/GDACS) — BUILD_PLAN §9 steps 5–6 | `app/tools/weather_tool.py` + `app/tools/disaster_tool.py` (two files, not merged — see §3 below) |
| `app/utils/slot_filling.py` | LLM extraction of destination/duration/budget/travelers/interests — BUILD_PLAN §9 step 7 | **Same path** — `app/utils/slot_filling.py` already exists on her branch; this was a literal duplicate, not just an equivalent |
| `test_orchestrator.py` | Only exercised the deleted `app/workflows/orchestrator.py` | n/a |
| `test_agents.py` | Only exercised `ContextAgent`/`PolicyAgent`/`CalendarAgent` | n/a |

None of Member B's real files (`recommendation_agent.py`, `planning_agent.py`, `db_tool.py`,
`app/data/*`, `app/rag/*`, `app/prompts/*`, `app/scheduler.py`) imported anything from this list —
confirmed with a repo-wide grep before deleting.

## 3. Naming/shape differences to know about (don't silently "fix" these — flag for a real merge)

- **Granularity mismatch**: this branch's `context_agent.py` combined weather + disaster into one
  class to share a single destination→coordinates lookup. Member A's real branch keeps them as two
  separate files (`weather_tool.py`, `disaster_tool.py`) per BUILD_PLAN §4, called together from a
  single `context` node in her `orchestrator.py` (her report §3.11 — one node, two concurrent
  `asyncio.gather` calls internally). The *behavior* this branch had (one coordinate lookup, two
  concurrent fetches) is actually what BUILD_PLAN §6 asks for — it's the two-tool-files-vs-one-class
  split that differs. When her real files land, nothing in Member B's code needs to change: B's code
  never called `context_agent.py` directly.
- **`app/tools/disaster_tool.py` doesn't exist yet anywhere.** Per MEMBER_A_REPORT.md §3.9/§4.1,
  it's the one file she hasn't started. This branch's (now-deleted) `context_agent.py` had a working
  EONET/USGS/GDACS implementation — worth showing her as a reference when she builds the real one,
  since the endpoints/params/severity mapping match BUILD_PLAN §1/§4 exactly. Recovered from git
  history at the commit before this cleanup if needed (`git show ce5eb4f:app/workflows/context_agent.py`).
- **`app/core/state.py` / `base_agent.py` / `result.py` / `app/config/settings.py`** were left
  **as-is** — these are the Phase-1 "both, together" shared contract (BUILD_PLAN §10 Phase 1), not
  one person's accidental duplicate, and Member B's real agents depend on them directly. However,
  Member A's branch has evolved its own version of `settings.py` with different key names in places
  (e.g. `gemini_api_key` vs. this branch's `google_api_key`) — see
  [MEMBER_A_INTEGRATION_TODO.md](MEMBER_A_INTEGRATION_TODO.md) §3 for the reconciliation needed at
  merge time. Don't delete either version pre-emptively.
- **`.env`** on this branch only has Member A's key set (`GEMINI_API_KEY`, no `GOOGLE_API_KEY`,
  `SUPABASE_URL`, `SUPABASE_KEY`, or `REDIS_URL`) — it appears to be a copy of her `.env`, not one
  reflecting this branch's `settings.py`. Left untouched (not code, and out of scope for a file
  cleanup), but see MEMBER_A_INTEGRATION_TODO.md §3.

## 4. Changes made to Member B's own files to remove the dependency

- **`main.py`** — previously built its `/api/plan-trip` endpoint entirely around
  `get_orchestrator_graph()` from the now-deleted `app/workflows/orchestrator.py`. Since Member A's
  real `app/core/orchestrator.py` isn't on this branch, `/api/plan-trip` now calls
  `RecommendationAgent` directly (it curates candidates **and** builds the itinerary in one LLM
  call — see `recommendation_agent.py`'s own docstring). This means the endpoint currently requires
  `destination` in the request body and does no policy/location/calendar/weather/disaster
  resolution — that's explicitly out of Member B's scope. A code comment at the top of `main.py`
  and MEMBER_A_INTEGRATION_TODO.md both document this as temporary, pending Member A's orchestrator
  landing on this branch. `/api/health`, `/api/agents`, `/api/rag/index`, and the two
  `/api/admin/sync/*` endpoints (Member B's own scope — RAG indexing, data pipeline triggers) are
  unaffected.
- **`app/tools/db_tool.py`** — fixed a real (pre-existing, unrelated to the A/B split) contract bug
  surfaced while verifying end-to-end: the mock hotel/restaurant/attraction rows never carried
  `lat`/`lon`, which BUILD_PLAN §4's per-listing shape requires. Backfilled from each destination's
  district centroid (`app/data/sri_lanka_districts.py`), with a small fallback for mock-only town
  names that aren't districts themselves (e.g. "Ella" is in Badulla district).
- **`test_db_tool.py`** — `test_get_user_profile_shape` asserted a `user_id` key that
  `get_user_profile`'s actual return shape (BUILD_PLAN §4) never included. Fixed the assertion to
  match the documented contract instead of adding an undocumented field to `db_tool.py`.

## 5. What was deliberately left alone

- `webscrape.py` — an unrelated scratch script (BeautifulSoup, `booking.com`), not in either
  member's BUILD_PLAN scope. Doesn't import or get imported by anything. Left as-is; flagging here
  only so it isn't mistaken for one of Member A's files.
- `requirements.txt` — empty on this branch. Not caused by this cleanup (verified: none of the
  removed files' dependencies — `langgraph`, mainly — were the only thing pulling in a package
  Member B's own code doesn't also need indirectly). Worth Member B populating it, but out of scope
  for an A/B ownership cleanup.

## 6. Verification performed (see also §12 of BUILD_PLAN.md)

All of the following were run **after** deleting Member A's files and fixing `main.py`:

| Check | Result |
|---|---|
| Repo-wide grep for imports of the 5 deleted modules | Only `main.py` and the 2 deleted test files referenced them — all fixed/removed |
| `test_state.py` | Pass |
| `test_db_tool.py` (both cases) | Pass (after the two fixes in §4) |
| RAG smoke test (`rag_service.index_candidate_data` + `retrieve_candidates`) | Pass |
| `test_phase2_agents.py` (RecommendationAgent → PlanningAgent, real Gemini calls) | **[PASS] PHASE 2 CHECKPOINT PASSED** — produced a real 2-day Kandy itinerary |
| `test_recommendation_agent.py` | 1/2 pass; the "populates all four categories" case is flaky — see §7 |
| `main.py` imports and builds the FastAPI app with no orchestrator dependency | Pass — routes: `/api/health`, `/api/plan-trip`, `/api/rag/index`, `/api/agents`, `/api/admin/sync/events`, `/api/admin/sync/listings` |

## 7. Known pre-existing issue (not caused by this cleanup)

`test_recommendation_agent.py::test_recommendation_agent_populates_state` asserts hotels,
restaurants, *and* attractions are all non-empty after one LLM call. Gemini doesn't always return
all three keys — a real run returned 2 hotels + 1 attraction but 0 restaurants for the same input
that passed on a different run. This is LLM output non-determinism in `recommendation_agent.py`'s
prompt handling, unrelated to anything removed in this cleanup (confirmed: same agent code, same
prompt, same failure mode before and after). Worth Member B tightening the prompt or the test's
assertion (e.g. assert on `recommendations` non-empty overall, not each category individually) —
not addressed here since it's outside "remove Member A's work."
