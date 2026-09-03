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

**Next things to try:** re-run a clean end-to-end pass (and `scripts/e2e_check.py`'s full 12 scenarios)
with fresh Gemini quota to get an unconfounded read on how often `plan_source` actually comes back
`"llm"` now. If Gemini keeps failing but Groq now reliably picks up the slack, that's a legitimately
good outcome already - the fallback chain doing its job. The model-swap/schema-simplify ideas for
Gemini specifically are still on the table if Gemini's own failure rate still matters after that.

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
