# TODO

Tracked follow-up work, deliberately deferred rather than fixed inline. Not a full backlog —
just things worth not forgetting.

---

## Recommend/plan ReAct agents unreliable against free-tier models (Phase 6)

**Filed:** 2026-09-02

**What's wrong:** `RecommendationAgent`/`PlannerAgent`'s structured-output calls
(`RecommendationOutput`/`PlannerOutput`) fail against `gemini-3.5-flash-lite` (the configured
primary) more often than not — meaning almost every real `/trip-plan` request currently ends up
on the deterministic **fallback** planner (`plan_source: "fallback"`) instead of the real ReAct
"llm" path. The system doesn't break when this happens (the fallback plan is complete, valid, and
budget-checked — see `AGENT_ARCHITECTURE.md §6`'s degradation matrix), but the LLM path this whole
phase built is effectively unused in practice today.

**What's already ruled out** (live-verified against real Gemini + Groq calls, not guesses):
- Not a payload-size threshold — a request with 3 candidate items succeeded, but the *same shape*
  request with 1 item or 6+ items both failed. Non-monotonic, so it isn't "too big."
- Not primary-model-specific — the identical failing payload sent directly to `gemini-3.6-flash`
  (the first fallback in the chain) failed the same way.
- Not a missing-fallback bug — `RunnableWithFallbacks.ainvoke()` (read directly from
  `langchain_core`'s source) does try every provider in the chain in order and only raises when
  all of them fail; it just raises the *first* provider's error, which is why every failure message
  names `gemini-3.5-flash-lite` even when the other two were also tried and also failed.
- Two real, distinct bugs in `app/core/react.py`'s finalization step *were* found and fixed along
  the way (see `PROJECT_MASTER_PLAN.md`'s Phase 6 section for the full writeup): the finalize
  message list ending on an assistant turn ("model prefilling" rejected outright), and reusing the
  tool-mandating system prompt for the toolless finalize call (the model would attempt an actual
  tool call and get rejected). Both are real fixes and stay. They just weren't the whole story.

**Current best hypothesis:** a genuine capability limit of these specific free-tier models for
`RecommendationOutput`/`PlannerOutput`'s schema complexity (nested lists of pattern-constrained
objects) under Gemini's schema-constrained decoding — this project is deliberately scoped to
free-tier models only (decision D6b), and the smallest/cheapest ones in the chain may simply not
be reliable enough for this specific job.

**Next things to try** (not yet attempted — this needs a fresh pass with its own budget, not more
ad-hoc live bisection against a rate-limited free tier):
1. Try a different/larger free-tier model specifically for the `recommend`/`plan` purposes
   (`get_llm()` already supports per-purpose model selection — see `LLM_PROVIDER_CHAIN` /
   `llm_model_orchestrator` in `app/config/settings.py`; a `llm_model_recommend`/`llm_model_plan`
   override could be added the same way).
2. Simplify `RecommendationOutput`/`PlannerOutput` — e.g. drop the UUID `pattern=` regex constraint
   on `listing_id` (validate the shape in `output_validator.py`'s L1 check instead, which already
   exists) in case Gemini's structured-output schema translation handles plain `str` fields more
   reliably than pattern-constrained ones.
3. Once either of the above is tried, re-run the same live test as a clean A/B: `Plan a 2-day trip
   to Ella for 2 people, budget 40000 LKR, I like nature and hiking` through the real
   `app.core.orchestrator.orchestrator` graph, and check `result["plan_source"] == "llm"` instead
   of `"fallback"`.

**Update (Phase 8, 2026-09-02):** the golden-scenario run confirmed this exactly as predicted — 11/12
scenarios passed live, almost entirely via `plan_source: "fallback"`. Two things changed the picture,
though: (1) the fallback plan is now genuinely good (see the `_fallback_node` fix in the next entry
below — it used to be silently item-less most of the time, which was arguably *masking* how bad this
gap looked), and (2) `plan_source: "llm"` was still never actually observed in any of ~15 live runs
this session. Still open; still worth the model-swap/schema-simplify attempt above with fresh quota.

**Update (2026-09-03) — real, partial progress; Gemini's root cause finally isolated as provider-specific:**

- **Groq's failure mode is now genuinely fixed, verified live with realistic data.** The actual root
  cause (not fully understood until now): `with_structured_output()` for BOTH Gemini and Groq is
  implemented via tool/function calling under the hood, and the finalization call was reusing each
  agent's LOOP system prompt — which describes real tools and says "you MUST call X" (by design, for
  the loop). A trailing "no tools here, don't try" nudge (the earlier fix) wasn't enough once real
  data gave the model something to reason about; Groq's error was explicit -
  `"attempted to call tool 'score_candidates'/'json' which was not in request.tools"`. The real fix:
  `app/core/react.py`'s `run_react()` now takes an optional `finalize_system` param that REPLACES the
  loop's system prompt entirely for the finalization call with a genuinely tool-free variant (no TOOLS
  section, no "you MUST call" language) — one per agent
  (`ORCHESTRATOR_FINALIZE_SYSTEM`/`RECOMMENDATION_FINALIZE_SYSTEM`/`PLANNER_FINALIZE_SYSTEM`/
  `build_repair_finalize_system()`), wired into all four `run_react()` call sites. Live-verified
  against Groq with a realistic 3-category, 40-item payload: **clean success** - 3 hotels, 2
  restaurants, 3 attractions, real ids, real ranks, real reasons, zero phantom tool calls.
- **Gemini's failure is a genuinely SEPARATE, still-unresolved issue.** The exact same tool-free
  `finalize_system` prompt, sent directly to `gemini-3.5-flash-lite`, still gets the same bare `400
  INVALID_ARGUMENT` with no further detail - proving the tool-mandate theory was never the whole
  story for Gemini specifically (it explained Groq's failure mode, not Gemini's). Gemini's own root
  cause is still whatever was isolated in the original investigation above (non-monotonic with size,
  affects `gemini-3.6-flash` identically) - a real, distinct, still-open problem.
- **The full fallback chain still didn't produce `plan_source: "llm"` in a live end-to-end re-test**,
  despite Groq succeeding in isolation - the error surfaced still named only `gemini-3.5-flash-lite`
  (consistent with `RunnableWithFallbacks` raising the *first* error when ALL providers fail, so this
  doesn't prove Groq failed too, but doesn't rule it out either - a real, larger-scale payload from
  inside the actual ReAct loop could differ from the clean synthetic test enough to matter, or Groq's
  own rate limits (8000 TPM, hit live during this session) could be the real blocker in a chain that
  reaches it third). **Not yet disambiguated** - needs a clean live re-test with fresh quota on both
  providers, ideally instrumented to show every provider's individual error, not just the first.
- All 421 tests still pass; the `finalize_system` mechanism itself has dedicated unit coverage in
  `tests/test_react.py` (verifies it's used verbatim, and that omitting it preserves old behavior).

**Update (2026-09-03, later same day) — disambiguated, and a second real fix landed:**

Built `scripts/check_llm_chain_reliability.py` (new, reusable diagnostic — tries the finalization call
against every provider in `LLM_PROVIDER_CHAIN` individually, instead of going through
`RunnableWithFallbacks`, which only ever surfaces the *first* provider's error). Result: **Gemini fails,
`gemini-3.6-flash` fails (transient 503 that run), Groq succeeds** — so Groq genuinely was reachable
and capable, contradicting the "maybe Groq fails too at real scale" open question above.

Re-running the real end-to-end orchestrator, though, still showed `plan_source: "fallback"` — so
something in the REAL loop-generated payload differed from the clean synthetic diagnostic. Captured
the actual finalize payload from a live run and sent it directly to Groq: **`413 Request too large` —
8510 tokens requested against Groq's free-tier cap of 8000 tokens/minute.** A real, hard, measurable
limit, not vague unreliability — real listing rows (every field `db_tool.py`'s `_row_to_listing_dict`
returns: `description`, `photo_url`, `opening_hours`, transit fields, etc.) are far bulkier than the
thin synthetic fixtures earlier testing used, and pushed a realistic 3-category/15-item payload just
over the ceiling.

**Fixed:** `app/core/react.py`'s observation trimming now does two things instead of one -
`_MAX_OBSERVATION_ITEMS` lowered 15→10, and a new `_STRIP_FIELDS` denylist drops
`description`/`photo_url`/`opening_hours`/`has_public_transit`/`nearest_transit_stop` from every item
in the finalization summary (no agent's finalization RULES reference any of them - id/name/tags/lat/
lon/rating/price all stay). 422 tests pass; dedicated regression coverage added.

**Live-verified this actually works**: in a real end-to-end orchestrator run post-fix, `RecommendationAgent`
succeeded with NO failure logged at all (first time this session) — genuine confirmation the fix closes
the gap it targeted. Further clean re-runs to confirm `plan_source: "llm"` consistently were blocked by
**Gemini's own 15 req/min free-tier quota**, fully exhausted by this session's testing (a real,
separate, unavoidable rate limit — not a code issue, and not something more fixes will change; it
just needs fresh quota, e.g. a different hour/day).

**Update (2026-09-03, third pass) — Gemini's 400 confirmed key-independent; two more real fixes:**

The user swapped in a completely different Gemini account's API key mid-session specifically to rule
out "bad/exhausted key" as the explanation. **Result: identical.** `gemini-3.5-flash-lite` and
`gemini-3.6-flash` both still fail with the same bare `400 INVALID_ARGUMENT`, reproduced fresh via
`scripts/check_llm_chain_reliability.py` on a brand-new key/account. This conclusively rules out
key/quota as Gemini's explanation - it is a genuine, deterministic request-shape rejection specific to
this schema, independent of account. Groq succeeded on the same run once its own TPM had a moment to
recover (transient rate limit only, not the same kind of failure).

Two more real, verified fixes landed from this round:
1. **`llm_provider_chain_groq_first_purposes` setting** (`app/config/settings.py`, default
   `"recommend,plan"`) - `get_llm()` now tries Groq FIRST for these two purposes specifically, since
   Gemini is evidence-confirmed to reliably fail them regardless of key, while `orchestrator`'s simpler
   TripContext schema keeps Gemini first (live-verified working separately). Trying Gemini first for
   recommend/plan was pure waste: two guaranteed-failed calls (and real Gemini quota) before ever
   reaching the provider with an actual chance.
2. **Groq's phantom-tool-call failure fully explained and fixed.** Inspecting a real failed generation
   showed the MODEL'S content was already perfect - real listing ids, correct ranks, sensible reasons,
   honest coverage_notes - it was wrapped in a synthetic `{"name": "json", "arguments": {...}}` tool-call
   envelope the API then rejected. Root cause: `with_structured_output()`'s default `method` for Groq is
   `"function_calling"` (tool-call-based delivery); Gemini's default is already `"json_schema"` (native,
   no tool-call envelope). `run_react()` now explicitly passes `method="json_schema"` to
   `with_structured_output()` — a no-op for Gemini, a real fix for Groq. Verified live: clean success
   immediately after the change, on the first non-rate-limited attempt.

**Current picture:** with both fixes in place, a real end-to-end run got as far as
`RecommendationAgent` succeeding outright (once), and other runs hit Groq's shared 8000 TPM budget -
expected, since reordering Groq to PRIMARY means it now carries the full ReAct loop's token cost (every
turn, not just the lightweight finalize call it used to only see as a last-resort fallback), and this
session's own rapid-fire testing has been hammering that same per-minute budget. Real single-user
traffic (one request at a time, not back-to-back diagnostic runs) should have materially more headroom
than what this session's testing pattern shows.

**Update (2026-09-03, fourth pass) — ran the full 12 scenarios, found and fixed a genuinely new bug:**

Ran `scripts/e2e_check.py` (all 12) right after the Groq-first reorder + `method="json_schema"` fixes
landed. Result: **10/12** — below target, but NOT because of the fixes above; the Groq-TPM pressure
predicted in the previous update turned out real (heavy `429` noise throughout this run, from
back-to-back scenarios all now hitting Groq first). Scenario 5 failed as already known and documented
(data sparsity, not new). **Scenario 8 (active red disaster) failed for a genuinely new, real reason.**

Traced it directly: `trip_context.disaster` correctly showed the mocked red-severity event every time
(the orchestrator's tool call worked fine) — but `trip_context.safety_notes` came back **empty**. The
LLM was fetching the hazard data correctly but not reliably writing it into the free-text field
`ORCHESTRATOR_SYSTEM_PROMPT` rule 5 asks it to fill ("do not silently omit it") - so the warning
silently never reached the user, exactly the failure mode that rule exists to prevent.

**Fixed properly, not with another prompt tweak:** `OrchestratorAgent.execute()`
(`app/agents/orchestrator_agent.py`) now derives `safety_notes` deterministically from
`ctx.disaster.active_events` whenever a `severity == "red"` event is present, instead of trusting the
LLM's free-text field alone (it still keeps whatever the LLM DID write, just adds the guaranteed note
if the LLM's own list came back without one). Matches this codebase's standing rule: never let LLM
unreliability hide a safety-relevant signal derivable from structured data already in hand — the same
principle `app/core/scoring.py`/`budget.py`/`itinerary.py` already apply to numbers. 434 tests pass, 3
new regression tests added. **Live-verified**: scenario 8 now passes even while Groq is mid-rate-limit
(the fix is deterministic, entirely independent of any LLM call succeeding) — confirming both that the
fix works and that it isn't masking anything by coincidence.

**Honest current tally: 11/12** (scenario 5 is the one remaining, already-understood, data-driven gap).

**Update (2026-09-03, fifth pass) — realistic-pacing conclusion: Groq's TPM is a real capacity
ceiling, not a testing artifact, and model-swapping within this account doesn't fix it:**

Ran isolated, individually-spaced single requests (real elapsed time between each, genuine review work
done in between rather than back-to-back calls) instead of the tight 12-scenario loop. **Still hit
Groq's 8000 TPM limit on an isolated single recommend call** - the error messages showed the request
itself needing ~5000 tokens even before any residual usage was counted. A single recommend call's OWN
requirement is close to two-thirds of the ENTIRE per-minute budget by itself - meaning a full
request (recommend + plan, both Groq-first) can plausibly need close to or over 8000 tokens on its
own, independent of how many other requests happened recently. This is a genuine, structural capacity
limit of `openai/gpt-oss-120b`'s free tier for this workload, not a rapid-fire artifact.

**Investigated model-swapping as a fix - real findings, no clean win yet:**
- Web search (Groq's 2026 published limits) confirmed `openai/gpt-oss-120b` and `openai/gpt-oss-20b`
  are both capped at 8000 TPM. Checked live via response headers (`x-ratelimit-limit-tokens`) that
  these two models **share one depleted pool**, not two separate ones - switching between them buys
  nothing.
- `qwen/qwen3.6-27b` (Groq's currently-available model list, checked live via `client.models.list()` -
  the earlier web search's `llama-3.3-70b-versatile`/`gemma-2-9b` results were stale; those models are
  no longer served on this account) has its OWN separate, fresh 8000 TPM pool - confirmed live via
  headers. This means splitting recommend/plan across `gpt-oss-120b` and `qwen/qwen3.6-27b` would
  genuinely double the combined budget for one end-to-end request. **But** qwen failed outright on the
  same realistic RecommendationOutput payload (`400 json_validate_failed`, empty generation) - it would
  need real prompt-compatibility work (possibly a simpler schema, or a different `method`) before it's
  usable, not a drop-in swap. Not pursued further this session - flagged here as the most promising
  concrete lever if this becomes a priority again.

**Conclusion for now:** the fallback path is the correct, working answer to this capacity ceiling as it
stands - every request still produces a complete, valid plan either way, and `plan_source` honestly
reports which path produced it. Chasing 100% `"llm"` further would mean either (a) real prompt-tuning
work to get `qwen/qwen3.6-27b` (or another separate-pool model) working reliably, or (b) upgrading past
Groq's free tier. Neither is warranted unless "how often is it the LLM path" becomes a real product
requirement rather than a nice-to-have - today's fixes already took it from "essentially never" to
"works when the budget allows," which is the honest, defensible place to leave it.

---

## Follow-up modification handling — ✅ built 2026-09-03, one residual data caveat

**Filed:** 2026-09-02. **Built and live-verified:** 2026-09-03.

**What was wrong:** `docs/master_plan/AGENT_ARCHITECTURE.md §5` documents that a follow-up turn which
only changes plan *shape* (e.g. `"make day 2 cheaper"`) should skip the recommendation agent entirely
and go straight to a targeted re-plan. That classifier and targeted-replan path didn't exist — every
follow-up re-ran the full pipeline from scratch, so golden scenario 5 failed.

**What's now built** (`app/core/followup.py` + `app/core/followup_replan.py`, wired into
`orchestrator.py` as a new `targeted_replan` graph node between `slot_fill` and `verify`):
- A deterministic classifier (no LLM) over this turn's raw `ExtractedSlots` — if the message didn't
  actually change destination/dates/interests/budget/must_avoid/pace/origin, and doesn't use an
  "I want something different" phrase, it's `shape_only`; otherwise `full` (the existing pipeline).
- The targeted rebuild is **entirely deterministic** (no LLM at all) — reuses the same
  `app/core/scoring.rank()` / `app/core/itinerary.build_day_plan()` every other deterministic path
  uses, scoped to only the day(s) named (or every day, if none named). Every day NOT targeted is
  copied verbatim from the carried session itinerary, which is what actually guarantees byte-identical
  days — a deliberate choice given the real, tracked LLM-reliability gap above; a feature whose own
  test requires byte-for-byte reproducibility has no business depending on that unreliable path.
- Live-verified end to end: `completed_steps` for a "make day 2 cheaper" follow-up is exactly
  `[validate, policy, slot_fill, targeted_replan, verify, respond]` — zero LLM calls, zero tool
  calls beyond a plain DB read. Days 1 and 3 come back **byte-identical** to the previous turn.

**Two real bugs found and fixed along the way:**
1. `OrchestratorAgent`'s human message never told the LLM what today's actual date is — it was
   inventing dates like `2025-05-18` (a full year wrong) for `date_window` defaults. That stale
   carried `trip_dates` then failed `validate_trip_state`'s past-date check on the very next turn,
   silently short-circuiting the ENTIRE graph before `slot_fill` even ran. Fixed by adding a real
   `"today"` field to the human message and telling the prompt to use it, never assume one.
2. The targeted rebuild originally excluded hotels from the "make it cheaper" price ceiling (on the
   theory that "the hotel itself isn't what cheaper swaps") — backwards: hotels are the ONE category
   with reliable real price data (Booking.com) in this dataset; restaurants/attractions mostly don't
   carry `price_level` at all (OSM doesn't have it). Excluding hotels defeated the point for most real
   requests. Fixed to include hotels in the ceiling like everything else.

**Residual gap — real data sparsity, not a logic bug:** golden scenario 5's literal "day 2 cost
strictly lower" check still fails against real Kandy data, because the specific restaurant/attraction
candidates for that day have no price data at all (`est_cost: 0.0` before AND after — nothing to
differentiate). Verified this is genuinely a data limitation, not a bug: re-running against day 1
(which has a real-priced hotel) shows the SAME hotel gets re-selected because it's *already* the
cheapest available within the price ceiling — correct behavior, since there's nothing cheaper to
swap to. This is the same "Restaurant/attraction pricing is not in OSM" risk
`PROJECT_MASTER_PLAN.md §7` already documents, not a new problem. Nothing further to build here
without better source pricing data.

---

## Listing images and events — three connector bugs fixed, two real dead ends (2026-09-04)

**Filed:** 2026-09-04. Triggered by mapping the Figma UI (`frontend-web/figma/`) onto real data:
Explore is an image-card grid, and the database had **0 listings with a `photo_url`** and 13
`listing_image` rows against 6,572 verified listings.

**Root cause:** `wikidata_enrich` had effectively never successfully run. The 86 rows that had a
non-NULL `description` carried OSM `description` tags (e.g. `"chinese"`, `"Hot Dish & Rooms"`),
not Wikipedia extracts — so nothing had ever populated `photo_url`. The Wikipedia API itself was
verified working live (Temple of the Tooth → a real `upload.wikimedia.org` URL), so this was
never an API problem.

**Three real bugs fixed:**
1. `wikidata_enrich.fetch()` swept **every** category. Wikipedia's 150 m geosearch essentially
   never matches a hotel or restaurant (they have no article), so a full sweep would spend ~2 API
   calls each on the ~5,800 listings that structurally cannot match — roughly 2.2 hours for almost
   no yield. Added a `--category` filter; attractions are the only category worth sweeping.
2. `booking_prices.upsert()` inserted into `listing_image` but **never set
   `travel_listing.photo_url`**, unlike `wikidata_enrich` which sets both. All 13 pre-existing
   Booking images were therefore invisible to any caller reading the single-image field. Fixed to
   mirror the insert with `COALESCE(photo_url, %s)`; the 13 stuck rows were backfilled.
3. `booking_prices.fetch()` had `page_number` hardcoded to `"1"` — one page (~20 properties) per
   district. **This was the actual reason nationwide coverage was 13.** Added `SEARCH_PAGES = 3`.
   Kandy alone went from a handful to 158 priced hotels with photos.

**Result:** `listing_image` 13 → 1,172 rows. Hotels 0 → 1,120 with both a photo and a real price.
Attractions 0 → 92 (full 25-district sweep, complete).

**Known incomplete — Booking sweep was rate-limited.** 16 of 25 districts returned 0 because
RapidAPI started 429ing partway through; the 1,120 hotels came from just 9 districts. The
connector is idempotent (`COALESCE` on `photo_url`, `ON CONFLICT DO NOTHING` on `listing_image`),
so **re-running it after the quota resets is safe and should substantially increase coverage.**
Worth doing before any demo.

**Dead end 1 — events.** `local_event` has 0 rows and Ticketmaster returns **zero events for
Sri Lanka**, re-verified live on 2026-09-04 against Colombo. This is unchanged from the gap
`ticketmaster_events.py`'s own docstring already documents. Decision (2026-09-04): Explore's
"Local Events & Cultural Festivals" rail ships **empty**, rather than seeding invented data. Real
coverage would have to come from admin-entered events (NestJS Phase 7).

**Dead end 2 — restaurant images.** 2,592 restaurants have no free image source at all: Booking is
hotels-only, and Foursquare's photo/rating fields are Premium-only even on the free Pro tier
(already verified 2026-09-02, `API_SETUP.md` §4.1). These need a **category placeholder in the
frontend**, not a backend fix. Note the Figma mockup itself repeats one placeholder image across
every card, so the design already assumes this.
