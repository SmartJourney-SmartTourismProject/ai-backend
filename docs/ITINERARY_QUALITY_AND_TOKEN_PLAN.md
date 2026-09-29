# Itinerary Quality & Token Reduction — What Changed and Why

Written 2026-09-24, at the end of the pass. Builds on
`docs/AI_BACKEND_OPTIMIZATION_PLAN.md` (the first optimization pass); this one
starts from a different, more concrete problem: three real bugs in generated
itineraries, plus the fact that `plan_source: "llm"` had been observed
**exactly once, ever** — almost every real request was served by the
deterministic fallback planner, not the multi-agent system this project is
actually about demonstrating.

## The three reported defects

1. **Incoherent routes** — the largest hop of a trip sat between two
   consecutive stops, and the road between them passed stops scheduled
   *later* in the itinerary.
2. **Infeasible days** — too many places scheduled in one day to be
   physically possible; travel time, dwell time, and opening hours were
   effectively ignored.
3. **Follow-ups didn't stick** — "reduce the destinations per day" changed
   nothing, on this turn or any later one.

All three turned out to be real, root-cause-fixable bugs in deterministic
Python, not model unreliability — but fixing them only matters if the
multi-agent path actually runs, which required fixing a separate, deeper
problem first.

## Part 1 — Making the agentic path actually run

**Root cause of the fallback-only behavior, found by live-testing against
real Gemini, not guessed:** `RecommendationOutput`/`PlannerOutput`'s schemas
carried UUID/date/time regex patterns and several array `max_length` bounds.
Gemini's structured-output decoder reliably rejected them with a bare `400
INVALID_ARGUMENT` — reproduced on a fresh account with a different key, so
not a quota issue. That forced the two heaviest calls onto Groq's free tier
(8,000 tokens/minute) instead of Gemini's (~1,000,000), and a single
`recommend` call alone needed ~5,000 tokens — hence the near-permanent
fallback.

**A1 — schema simplification.** Dropped the pattern constraints and array
bounds from `app/models/schemas.py`. Nothing was lost: `output_validator.py`'s
L1 `validate_referential` already proves every id came from a real
observation (stronger than a regex), and the business-rule bounds moved to
L2 (Part 4). **Live-verified the fix directly**: re-ran
`scripts/check_llm_chain_reliability.py` against real Gemini — the 400 was
gone. Reverted `llm_provider_chain_groq_first_purposes` to empty so
`recommend`/`plan` go to Gemini first again.

**A real second bug, found only by running the live checkpoint after A1:**
even with the schema fixed, most requests still fell back. The model
reliably wrote `plan_source: "tool_observations"` or `"tool_plan"` instead of
the schema's required literal `"llm"` — a `pydantic.literal_error` that
failed structured output on the original call *and* the one repair attempt,
every time. Grepped every usage: `output.plan_source` is **never read
anywhere** — `planner_agent.py` always hardcodes `state.plan_source = "llm"`
itself, exactly like the fallback planner hardcodes `"fallback"`. The field
was pure dead weight the model could never reliably satisfy. **Deleted it
from `PlannerOutput`/`RepairedPlannerOutput` entirely**, not patched with a
prompt tweak. This — not the schema fix alone — is what actually got
`plan_source: "llm"` returning from the real `/trip-plan` endpoint.

**B1 — id-based tool arguments.** `score_candidates`, `estimate_costs`, and
`build_day_plan` used to force the model to re-emit entire candidate rows as
output tokens just to say which ones it meant — up to 15 rows × ~15 fields,
once per category. They now take `listing_ids`, resolved server-side against
a per-request item store. `build_day_plan` also stopped taking
`outdoor_tags`/`cost_lookup` as model-supplied arguments — both are computed
deterministically now, closing a real correctness gap (a mis-transcribed
`cost_lookup` used to silently break the `cost_recomputes` validator).

**B2/B3 — drop `travel_matrix`, trim the repair payload.** `travel_matrix`'s
result was never converted into anything usable (`TravelMatrix.from_matrix_result`
had zero real callers), so it was a tool the model could waste a whole
ReAct step calling for nothing. The repair call no longer resends the entire
previous itinerary — trimmed to just what identifies an item and what a
repair might check.

**C1 — `react_max_steps`, decided with evidence, not guessed.** The plan's
original hypothesis was "if agents typically finish in 2 turns, cut the
cap to 2." A live run against real Gemini stopped via `stopped_by:
"max_steps"`, not `"answer"` — the model was still actively working when the
cap hit. Left at 3.

**C2 — the orchestrator became deterministic.** Its six tools
(`resolve_place`, `resolve_district`, `resolve_start_location`,
`get_calendar_free_days`, `get_weather`, `get_disaster_info`) were
deterministic lookups with no real reasoning between them — the "agent" only
sequenced them, and had already been caught inventing dates a full year
wrong until a `today` field was bolted on. Replaced with
`app/core/context_resolver.py`, a plain deterministic function. Removes ~4
of a fresh plan's ~12 LLM calls and the entire `CONTEXT_TOOLS` schema
payload. Recommendation (selecting and justifying) and Planning (shaping the
trip) remain real ReAct agents — this wasn't a retreat from the multi-agent
design, it was the same principle the codebase already applies to costs and
safety notes: use the model only where the work is genuinely probabilistic.

**C3 — the request cache `D6c` had always claimed but never built.** Added:
a fresh (non-follow-up) request identical to one seen recently is now served
from Redis instead of re-running the graph, with a fresh `session_id` minted
per hit so follow-ups still behave correctly. Caught a real test bug while
building it — the tests weren't mocking the cache, so they hung ~40s per
file against a real, unreachable Redis in CI.

## Part 2 — Itinerary routing & feasibility

Applies to **both** paths — the planner agent calls the same
`build_day_plan` the fallback planner does.

- **2-opt** after nearest-neighbour construction (`app/core/itinerary.py`),
  both minimizing travel *minutes* now, not raw distance — the direct fix
  for "biggest hop between consecutive stops." Verified with a dedicated
  test that a known crossing route gets uncrossed.
- **Geographic day clustering** (`app/core/clustering.py`) — attractions are
  partitioned by proximity, not just top-N-by-score, wired into the
  fallback planner and the follow-up rebuild. The candidate pool feeding it
  was later widened 3× after a live run showed day 1 greedily claiming the
  tightest cluster and leaving day 2 with geographically incoherent
  leftovers by construction.
- **`DAY_END` feasibility simulation** — the actual fix for "too many
  places in a day." Nothing previously bounded total elapsed time including
  dwell; a day could run past midnight and still validate. Now simulated
  before emission, with a reorder-after-drop pass so survivors aren't left
  in an ordering optimized for a set that no longer exists.
- **Tag-based dwell times** — a 90-minute flat assumption for every
  attraction (a 20-minute viewpoint and a half-day hike counted the same)
  is now looked up from the item's tags.
- **Clock-driven meal placement**, replacing an index-midpoint splice that
  could put "lunch" at 09:40 or "dinner" at 14:20.
- **Restaurant-locality fix (a live-discovered extension of Part 2).**
  After the above shipped, a live run put a Badulla restaurant on an
  all-Ella day, ~13km from everything else. Root cause: the planner model
  chose which `restaurant_ids` went with which day, with zero geographic
  signal to do it well. Unlike attractions, a restaurant has no real
  "which day" judgment content — `build_day_plan` no longer takes
  `restaurant_ids` at all; restaurants are resolved server-side by real
  proximity from a pool shared across every day the request builds (with
  cross-day dedup). **Known, deliberate scope boundary**: the same gap
  still exists for *attractions* on the agentic path specifically —
  `clustering.py` only feeds the deterministic paths; the model still
  assigns `attraction_ids` to days itself, with within-day ordering and the
  `DAY_END` budget enforced regardless. Flagged, not silently extended,
  since attractions have real day-assignment judgment content restaurants
  don't.

**A live-found regression, unrelated to Part 2 itself but caught while
testing it:** a repair call was observed producing `items: []` with the
previous attempt's `day_cost` carried over unchanged, and it passed every
existing check. Cause: A1 had dropped `ItineraryDay.items`'s schema-level
`min_length=1` (correctly, per A1's own reasoning — business rules belong in
L2), but nothing in L1/L2 had ever required a day to have at least one item.
Added a `days_have_items` L2 rule.

## Part 3 — Follow-up memory ("reduce the destinations per day")

`ExtractedSlots` gained `items_per_day` (exact count) and
`items_per_day_delta` (relative change) — `pace`'s 3-value enum could never
express "one fewer than before." `TripState.items_per_day` carries across
turns via `session_store.py`.

**A real design improvement over the original plan, found during
implementation:** the delta is resolved directly in `slot_filling.py`
(`state.items_per_day = clamp(resolve_items_per_day(state) + delta)`), not
deferred to `followup_replan.py` as originally sketched. Deferring it would
have silently dropped the request whenever a follow-up also changes
something else and routes to a full re-plan instead of the targeted
rebuild — `followup_replan.py` never runs on that path at all. Resolving it
in `slot_filling.py` makes it take effect uniformly, since both the
fallback planner and the agentic planner's own prompt payload already read
the shared resolver (`app/core/planner_shared.py:resolve_items_per_day`).
A consequence: `app/core/followup.py` needed zero changes — density was
deliberately kept out of `changed_a_real_field`, so a plain "fewer places"
message already routes to the cheap targeted rebuild.

Verified with two end-to-end integration tests chaining the *actual*
`fill_slots()` into the *actual* `rebuild_targeted_days()` — not mocking
either side — proving both that the count drops and that a second "even
fewer" compounds (2→1) rather than resetting to the original pace-derived
count each time, which was the literal reported bug.

## Part 4 — Guardrails

Three new L2 rules in `output_validator.py`, each degrading to a pass when
its context field is absent (the existing `_cost_recomputes` precedent):

- `day_ends_by_curfew` — the LLM planner's own output is now checked
  against the same `DAY_END` the deterministic path enforces by
  construction.
- `no_absurd_hop` — same check for `max_single_hop_minutes`.
- `items_per_day_respected` — `<=`, not `==`, since a legitimate
  weather/price/feasibility drop should never read as a violation.

Both constants (`DAY_END`, `DEFAULT_MAX_SINGLE_HOP_MINUTES`) are exported
from `itinerary.py` so the guardrail checks the LLM against the exact same
numbers the deterministic path enforces, not a second, driftable copy.

## Part 6 — Deletions & de-duplication

Before deleting anything, each candidate was re-verified against git
history rather than trusted at face value from the original triage — two of
four turned out to be wrong calls:

- **`wikidata_enrich.py` — kept.** Has real, recent commits including the
  exact bug-fix pass `TODO.md` documents as producing 92 real attraction
  photos. A deliberately-manual admin tool (not automated on purpose, since
  a full sweep is too slow/API-heavy to run unattended), not dead code.
- **`demo/index.html` — deleted** (explicit call, overriding the
  recommendation to keep it) despite also having real recent commits
  (multi-day route rendering). `Readme.md`'s demo section rewritten to show
  calling `/trip-plan` directly.
- **`foursquare_enrich.py` — deleted**, along with its test and the
  now-orphaned `foursquare_api_key`/`foursquare_monthly_budget` settings.
  Thin usage history (a single "adjustments" commit) compared to
  `wikidata_enrich.py`.
- `postgres_writer.get_category_id_map()` deleted (confirmed zero real
  callers).
- `_cost_lookup_for` (byte-identical in `fallback.py`/`followup_replan.py`,
  no comment justifying the duplication) unified into
  `app/core/budget.py:cost_lookup_for()`.
- The connector-layer haversine (`osm_listings.py`/`booking_prices.py`)
  unified into `app/data/connectors/base.py:haversine_km()`.

**Left alone (duplication is deliberate and documented):** `_fetch_cost_table`
×2 in `planner_agent.py`/`followup_replan.py`, and the
`scoring.py` ↔ `routing_tool.py` haversine copy (`scoring.py` cannot import
an I/O module).

## Verification

- **456 → 482 tests**, all passing throughout every step of this pass.
- **A1 and the `plan_source` fix were each live-verified directly** against
  real Gemini, not just unit-tested.
- **Two full live end-to-end runs** of `POST /trip-plan` against the real
  server (real Postgres, real Redis, real Gemini) returned
  `plan_source: "llm"` with zero errors and a genuine multi-day itinerary —
  the actual definition-of-done gate this whole pass was built around.
- The restaurant-locality fix was verified both by 4 dedicated unit tests
  and by two further live runs, both showing restaurants correctly tracking
  wherever each day's actual attraction cluster ended up.

## Known limitations / deferred

- **Attractions still aren't geographically clustered on the agentic
  path** — only the deterministic fallback/follow-up paths use
  `clustering.py`. The planner model assigns `attraction_ids` to days
  itself; within-day ordering (2-opt) and the `DAY_END` budget are enforced
  regardless, but cross-day attraction placement on a real `llm`-sourced
  plan is still up to the model's own geographic judgment.
- **Booking.com hotel price/photo coverage remains incomplete.** 16 of 25
  districts were already unpriced from an earlier rate-limited sweep; a
  full 25-district re-run this session confirmed the RapidAPI free tier is
  still quota-exhausted (a clean `429` on every district). Scraping
  Booking.com directly to bypass this was considered and declined — their
  terms of service explicitly prohibit it, and this is a different
  situation from the sanctioned RapidAPI integration already in place. The
  system already degrades gracefully without it (`cost_reference` table
  estimates fill in for missing real prices), so this only affects hotel
  cost realism, not whether the demo works. Re-run
  `python -m app.data.connectors.booking_prices` once the quota resets
  (typically daily or monthly, plan-dependent) — the connector is
  idempotent, so this is always safe to retry.
- A2 (per-purpose model overrides) and A4 (a second Groq TPM pool via
  `qwen/qwen3.6-27b`) from the original plan were not implemented — A1
  removed the problem they were meant to work around.
- Deferred, unchanged from the first pass: wiring up Google Calendar's
  orphaned OAuth flow, admin auth on `/api/admin/sync/*`, rate limiting on
  `/trip-plan`.

## References

See `docs/AI_BACKEND_OPTIMIZATION_PLAN.md`'s own reference list for the
token-optimization and context-engineering sources this pass continued
from. Itinerary-specific:

- [Tourist trip design problem with time windows](https://www.sciencedirect.com/science/article/pii/S156849462400173X)
- [2-opt neighbourhood for improving TSP tours](https://phabe.ch/2024/08/27/how-to-improve-tsp-tours-applying-the-2-opt-neighborhood/)
- [Evaluating memory in LLM agents via incremental multi-turn interactions](https://arxiv.org/html/2507.05257v3) — agents remembering a preference yet failing to act on it, the exact failure mode Part 3 fixes
