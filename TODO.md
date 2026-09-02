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
