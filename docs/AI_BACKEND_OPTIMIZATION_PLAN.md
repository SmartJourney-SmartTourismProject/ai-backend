# AI Backend Optimization Plan — Triage, Deletions & Token Reduction

Written 2026-09-24. Scope: `ai-backend/` is a ~17k-line Python service
(≈7.7k `app/`, ≈6.6k `tests/`, ≈1k `scripts/`) that turns a free-text trip
request into a costed, day-by-day Sri Lanka itinerary. It is a LangGraph
state machine driving three bounded ReAct agents on free-tier LLMs only
(Gemini primary, Groq failover — decision D6b).

Two problems prompted this review.

**Token ceiling.** `TODO.md` records, with live-measured evidence, that the
LLM path is effectively unused in practice. A single `recommend` call needs
~5,000 tokens against Groq's **8,000 tokens/minute** free-tier cap; real
requests have 413'd at 8,510 tokens. Almost every `/trip-plan` therefore
lands on the deterministic fallback planner. The system never breaks — the
fallback produces a complete, valid, budget-checked plan and `plan_source`
reports honestly which path ran — but `plan_source: "llm"` has been observed
exactly once, ever.

**Accumulated dead weight.** The service is, in practice, a one-endpoint
service: `POST /trip-plan` is the only route with a caller anywhere in the
repo (`backend/src/chat/ai-backend.service.ts`). The RAG delete-review
(decision D11) was overdue.

This document is the triage; the token fixes and deletions it describes were
implemented in the same pass.

## What exists today

Request path — `POST /trip-plan` → 11-node LangGraph
(`app/core/orchestrator.py`):

```
validate -> policy -> slot_fill -> orchestrate -> recommend -> plan -> verify -> (repair|fallback) -> respond
slot_fill -> targeted_replan -> verify -> respond        (shape-only follow-up, zero LLM)
```

Five LLM call sites, all structured-output, with per-purpose output budgets
(`app/core/llm.py`):

| # | Site | Purpose | max_tokens |
|---|---|---|---|
| 1 | `app/utils/slot_filling.py` | `slots` | 512 |
| 2 | `app/agents/orchestrator_agent.py` | `orchestrator` | 1024 |
| 3 | `app/agents/recommendation_agent.py` | `recommend` | 2048 |
| 4 | `app/agents/planner_agent.py` | `plan` | 3072 |
| 5 | `app/core/orchestrator.py` (`_repair_node`) | `plan` | 3072 |

Sites 2–5 each run through `run_react` (`app/core/react.py`), which makes up
to `max_steps` loop calls plus one mandatory finalization call. A fresh plan
is ≈10–12 provider calls.

Endpoint reality (8 routes, 1 live): only `POST /trip-plan` has a real
caller. `GET /api/health` is kept for the AWS ALB health check even though
nothing calls it today. `POST /api/rag/index` and the admin sync triggers
have no caller; the Google OAuth flow is built but nothing starts it.

Already deterministic, and kept that way: ranking (`core/scoring.py`), day
layout/routing/timing (`core/itinerary.py`), cost arithmetic
(`core/budget.py`), the zero-LLM planner (`core/fallback.py`), the targeted
follow-up rebuild (`core/followup_replan.py`), the follow-up classifier
(`core/followup.py`), and the response template (`_respond_node`).

## Must-have — kept, untouched

1. `POST /trip-plan` + the LangGraph orchestrator.
2. Deterministic core: `scoring.py`, `itinerary.py`, `budget.py`,
   `fallback.py` — what actually serves nearly all traffic today, at zero
   token cost.
3. `verify -> repair -> fallback` loop (`output_validator.py`), one-repair
   cap.
4. Slot filling (`slot_filling.py`) — cheapest LLM call, the only thing
   turning free text into structured intent.
5. Session / multi-turn (`session_store.py`).
6. Shape-only follow-up path (`followup.py` + `followup_replan.py`) — one
   LLM call instead of ten.
7. Real data layer: `db_tool.py`, `geo_tool.py`, `db_pool.py`, the
   `DataUnavailable` no-mock-data convention.
8. Weather + disaster tools.
9. Data pipeline + scheduler — off the request path entirely.
10. `GET /api/health` — needed for the AWS ALB check.

## Deletions implemented

### C1 — RAG subsystem (`app/rag/`)
Deleted `app/rag/`, the `/api/rag/index` endpoint and its
`IndexDataRequest` model, `requirements-rag.txt`, and the four RAG settings
(`embeddings_model`, `chunk_size`, `chunk_overlap`, `enable_rag`).
`ENABLE_RAG` removed from `.env.example`. Rationale: `enable_rag` was read
nowhere in `app/`; `faiss-cpu`/`sentence-transformers` are not installed, so
the embeddings path silently used an MD5 hashing stub; the vector store was
in-memory, rebuilt per call, never persisted; no agent imported `app.rag`;
zero tests existed for it.

### C2 — Dead config and dead state fields
Deleted `llm_model`, `llm_max_tokens` (never read — `llm.py` uses its own
token-budget table and provider chain), `rate_limit_per_minute` /
`rate_limit_per_day` (unenforced, D15, and the rate-limiting nice-to-have
was not taken this round), and `TripState.candidate_attractions` /
`candidate_hotels` / `candidate_restaurants` / `candidate_events` (never
written by production code, superseded by `candidate_pools`; the
identically-named symbols in `fallback.py` are unrelated local function
parameters).

### C3 — `RESPONSE_SPEC` / `app/prompts/response_prompt.py`
Deleted. Registered in the prompt registry but never consumed —
`_respond_node` hardcodes its own template, and `enable_response_narration`
was never read anywhere. Removed the module, its registry entry, and the
flag (plus `ENABLE_RESPONSE_NARRATION` from `.env.example`).

### C4 — `demo/index.html`
Not deleted this round — kept as the only manual visual check available
until a replacement exists; flagged as low priority in the original triage
and left for a follow-up pass so as not to lose a debugging convenience
without a substitute in hand.

### C5 — Ticketmaster events — demoted, not deleted
`db_search_events` removed from `DATA_TOOLS`
(`app/tools/registry.py`) so the recommendation agent no longer carries a
tool it can never get real Sri Lanka results from (local_event has 0 rows;
Ticketmaster returns zero events for Sri Lanka, re-verified). The
recommendation prompt's tool list and finalize-system text were updated to
stop mentioning it. The Ticketmaster connector's cadence
(`app/data/connectors/ticketmaster_events.py`) was changed from `"daily"` to
`"manual"` so the nightly scheduler sweep (`_due_connectors`) no longer
burns API quota on it automatically; `POST /api/admin/sync/events` still
triggers it explicitly by name when admin-entered/real event coverage is
worth re-checking.

## Token optimization implemented

### O5 — token accounting (done first, so later changes are measurable)
Added `app/core/llm.py:log_token_usage()` — a best-effort logger that reads
`response_metadata`/`usage_metadata` off a real provider `AIMessage` (field
names differ: Groq nests under `token_usage`, Gemini under
`usage_metadata`) and logs it at INFO, tagged by purpose. Wired into
`app/core/react.py`'s loop (every turn) and into its finalize call, which
now requests `include_raw=True` from `with_structured_output()` so the raw
`AIMessage` (and its token usage) is available even for the
structured-output call — the dominant cost per the ~5,000-token
`recommend`-call measurement. The finalize path stays backward compatible
with a bare parsed-object return (what a test double or a future provider
without `include_raw` support would give back).

### O1 — trim ReAct loop observations, not just the finalize call
`_trim_observation` was previously applied only to the finalization
message; the loop transcript (`ToolMessage` appended on every turn, then
resent on every subsequent turn) was untrimmed. It is now applied at both
points. Also dropped `db_search_listings`'s default `limit` from 40 to 15 —
the agent selects at most 3 hotels / 2×days restaurants / 3×days attractions
regardless, and `score_candidates` does the ordering, so 40 raw rows per
category was pure overhead.

### O2 — stop double-stuffing the planner payload
`build_planner_human_message` previously sent both the full selections
(hotels/restaurants/attractions/events) and the entire `candidate_items` map
— the same data, twice, at full width, and the repair call sent that plus
the whole previous itinerary. It now sends only the selections, field
stripped to what the planner's own RULES reference (`id`, `name`, `tags`,
`lat`, `lon`, `price_level`, `rating`), and drops `candidate_items`
entirely — `build_day_plan` gets its real item data from the tool call, not
the prompt.

### O3 — enforce `max_input_chars`
`PromptSpec.max_input_chars` was documented but never read anywhere. Added
`app/prompts/_base.py:enforce_max_input_chars()`, called at each agent's
human-message construction site (orchestrator, recommendation, planner, and
the repair node) — logs a warning and trims when a payload exceeds the
spec's limit, converting a silent 413 into a diagnosable event.

### O4 — align `.env.example` with `settings.py`
`.env.example` set `REACT_MAX_STEPS=6`; `settings.py` defaults to `3`. A
fresh clone was doubling the LLM turns per agent versus the live `.env`.
Aligned to `3`.

### O6 — request cache (not implemented this round)
`PROJECT_MASTER_PLAN.md` states identical `/trip-plan` requests are cached;
they are not. Left as a follow-up — out of scope for this pass, which
focused on the token-ceiling fixes and the overdue deletions.

## Verification

- `pytest` in `ai-backend/` must pass after each step (438 tests before this
  pass; paired tests updated alongside C1–C3/C5 deletions).
- `python scripts/check_apis.py` confirms external keys still resolve after
  config deletions.
- `python scripts/e2e_check.py` — the 12 golden scenarios; current honest
  tally is 11/12 (scenario 5 is a known data-sparsity gap, not a
  regression). This is the gate for O1, which is a real behaviour change
  (the agent no longer sees every raw field during the loop).
- The real success measure for O1–O3: the logged token count for a single
  `recommend` call dropping well below the ~5,000 previously measured, and
  the proportion of runs returning `plan_source: "llm"` rising above
  ~never. This requires running the stack against real provider keys to
  observe — not verifiable from a static code review alone.

## Deferred (nice-to-have, not implemented this round)

- Wire up Google Calendar's orphaned OAuth flow.
- `qwen/qwen3.6-27b` on a separate Groq TPM pool (blocked on a schema
  validation failure against the real payload).
- Re-run the Booking.com sweep for the 16 districts that hit RapidAPI 429s.
- Register or retire `wikidata_enrich` / `foursquare_enrich`.
- Admin auth on `/api/admin/sync/*`.
- Rate limiting on `/trip-plan` (D15), `X-Forwarded-For` handling.
- Restaurant image placeholders (frontend, not backend).
- Put `scripts/e2e_check.py` in CI.
- Doc corrections: `Readme.md`'s stale test count and broken doc links,
  `AWS_DEPLOYMENT_PLAN.md`'s stale session-file description,
  `app/scheduler.py`'s stale foursquare docstring claim.
