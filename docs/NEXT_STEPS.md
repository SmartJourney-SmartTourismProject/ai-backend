# Next Steps — post-merge plan

**Branch:** `main` (after `temp-shaluka` merged in at `bfd1805`)
**Snapshot:** 2026-08-29
**Reference:** [BUILD_PLAN.md](BUILD_PLAN.md) — §7 API contract, §8 error/caching matrix, §10 phases, §12 verification checklist

Both tracks are now on one branch and the full graph runs end to end against Member B's real
agents. Test suite: **68 passing, 1 xfail**, no network calls, no API keys, ~5s.

This document is what's left, in the order worth doing it.

---

## Where we actually are

| Phase | Status |
|---|---|
| Phase 1 — Foundations & contracts | Complete |
| Phase 2 — Parallel build | Complete (`disaster_tool.py` was the last missing file; it exists now) |
| Phase 3 — Data pipeline (Member B) | On `main` — Overpass/events ingest + scheduler present |
| Phase 4 — Integration | **Complete.** `_recommend_node`/`_plan_node` call the real `RecommendationAgent`/`PlanningAgent` |
| Phase 5 — API layer + testing | Mostly done. `POST /trip-plan` + OAuth live, CORS in `main.py`, real pytest suite. **§7 contract mismatch outstanding — see P1 below** |
| Phase 6 — Error matrix, caching, demo | Caching done. Error-matrix walkthrough and demo script outstanding |
| Phase 7 — NestJS + Admin Panel | Correctly not started (gated on Phase 6) |

---

## P0 — Small, self-contained, do these first

### 1. OpenWeather forecast timezone bug

**File:** [app/tools/weather_tool.py:69](../app/tools/weather_tool.py#L69)

```python
slice_date = datetime.fromtimestamp(slice_["dt"]).date().isoformat()
```

`fromtimestamp()` with no timezone uses the **server's local timezone** to decide which calendar
date a 3-hour forecast slice belongs to. The `dates` list it filters against is built from UTC
dates, so on a server outside Sri Lanka's timezone the buckets shift and legitimate forecast days
get dropped. This is not theoretical — during Step 3 testing, Galle returned
`{"current": {...}, "forecast": []}`: current conditions fine, forecast silently empty.

**Fix** — use OpenWeather's own `dt_txt` field (already UTC, already a date string):

```python
slice_date = slice_["dt_txt"].split(" ")[0]
```

or equivalently `datetime.fromtimestamp(slice_["dt"], tz=timezone.utc).date().isoformat()`
(needs `timezone` added to the `datetime` import).

**Verify:** [tests/test_weather_tool.py](../tests/test_weather_tool.py)'s `FORECAST_RESPONSE`
fixture currently has no `dt_txt` field and its `dt` values don't assert on bucketing — add a
test that pins two slices to a known UTC date and asserts they land in that date's bucket
regardless of the machine's timezone.

### 2. `datetime.utcnow()` deprecation

**File:** [app/core/orchestrator.py:98](../app/core/orchestrator.py#L98)

Emits a `DeprecationWarning` on every test run and is scheduled for removal in a future Python.
Same fix shape as above:

```python
today = datetime.now(timezone.utc).date()
```

### 3. Policy guard — the known gap

**File:** [app/utils/policy_guard.py:24](../app/utils/policy_guard.py#L24)

The blocklist has `"ivory trade"` but not `"ivory market"`, so
`"is there an ivory market I can visit"` passes. Currently tracked as an `xfail` in
[tests/test_policy_guard.py](../tests/test_policy_guard.py) so it's visible rather than silently
missing.

Two options — **pick one and write the decision down**, don't leave it drifting:

- **Ship the blocklist as-is for the capstone demo**, add `"ivory"` as a bare term (accepting that
  it may over-block a legitimate "ivory tower" style phrase), flip the `xfail` to a normal test,
  and note in the file that a substring blocklist is inherently bypassable.
- **Replace the substring approach** with something less trivially evaded. Bigger job, and
  probably not worth it before the demo.

Whichever way it goes, the `############ NOT FINALIZED ############` banner at the top of the file
should stop saying "this is just a testing file" once a decision is made.

---

## P1 — §7 API contract: decide and align

**File:** [app/api/trip.py](../app/api/trip.py) vs [BUILD_PLAN.md §7](BUILD_PLAN.md)

The endpoint and the written contract still disagree. This matters more now than it did before,
because **Phase 7's NestJS backend gets built against whichever one is written down**, and
changing it later means changing two codebases.

| §7 says | Code has |
|---|---|
| request key `message` | `user_input` |
| request `session_id` | absent |
| response `weather` | absent |
| response `disaster_warnings` | absent |
| — | extra: `destination`, `completed_steps` (a debug field, exposed publicly) |

Note the internal inconsistency worth resolving while you're in there: the file's own design notes
claim `completed_steps` is hidden as "internal-only", but `TripPlanResponse` exposes it.

**Recommendation:** amend the code to match §7 rather than the reverse — `weather` and
`disaster_warnings` are genuinely useful to a chat UI, and both are already populated in state by
`_context_node`, so surfacing them is a few lines. Drop `completed_steps` from the public shape
(or gate it behind `settings.debug`).

---

## P2 — Phase 6: walk the §8 error matrix

Now that everything is wired together for the first time, go tool by tool through
[BUILD_PLAN.md §8](BUILD_PLAN.md) and confirm each failure mode degrades rather than crashes.
Most already have a failure-mode test; the gaps are worth closing:

| Source | Failure test exists? |
|---|---|
| OpenWeather | Yes — `test_get_weather_api_failure_returns_none` |
| EONET / USGS / GDACS | Partly — see the finding below |
| Google Calendar | Yes — `test_get_free_days_google_api_error_returns_empty_not_raises` |
| Location / IP lookup | Yes — `test_ip_lookup_http_failure_returns_none_not_raises` |
| Supabase (`db_tool`) | **No** — falls back to mock data on exception, untested |
| Gemini (slot filling) | Yes — `test_llm_failure_degrades_without_raising` |
| Gemini (Recommendation/Planner) | **No** — Member B's side |

### The disaster-tool finding

**File:** [app/tools/disaster_tool.py](../app/tools/disaster_tool.py)

`get_disaster_info`'s "all three sources failed" fallback — the
`{"safe": True, "active_events": [], "note": "disaster data unavailable"}` branch §8 explicitly
asks for — **is unreachable in practice.** Each `_fetch_*` function catches its own exceptions
internally and returns `[]`, so from `get_disaster_info`'s perspective every result is a valid
list and `all_failed` never becomes `True`. "All three APIs are down" and "confirmed zero
disasters nearby" produce identical output today.

This is documented in
[tests/test_disaster_tool.py](../tests/test_disaster_tool.py) —
`test_all_sources_returning_http_errors_still_reports_safe` pins the current behavior, and
`test_exception_escaping_a_fetcher_triggers_note_fallback` proves the fallback logic itself works
when an exception genuinely reaches `asyncio.gather`.

**Fix:** have each `_fetch_*` return `None` on failure and `[]` on a confirmed-empty result, then
treat `all(r is None for r in results)` as the all-failed case. Update the two tests above once
changed.

Whether this matters for the demo is a judgment call — arguably telling a user "we couldn't check
for disasters" vs "no disasters found" is exactly the distinction §8 wanted, and it's a small fix.

---

## P3 — Blocked on Member B (raise these, don't wait silently)

### `db_tool.get_user_profile` is an unimplemented stub

**File:** [app/tools/db_tool.py:327](../app/tools/db_tool.py#L327)

```python
async def get_user_profile(user_id: str) -> dict:
    default = {"interests": [], "travel_style": None, "budget": None, "home_location": None}
    return default
```

It ignores `user_id` entirely and always returns empty defaults. The §2 defaulting logic in
[app/utils/slot_filling.py](../app/utils/slot_filling.py) that pulls
`interests`/`travel_style`/`budget` from the user's profile is written and correct, but currently
has nothing to read — so BUILD_PLAN §12's second scripted case (`"Plan a trip to Kandy"` → pulls
preferences from `get_user_profile`) **cannot pass end to end today**, no matter what the
Orchestrator does.

Tracked in [tests/test_db_tool.py](../tests/test_db_tool.py)
(`test_get_user_profile_currently_always_returns_defaults`) so this doesn't get forgotten.

### `google_oauth_tokens` table

Calendar tokens currently persist to a local `calendar_tokens.json` file
([app/tools/calendar_tool.py](../app/tools/calendar_tool.py)) — deliberately interim. It survives
a restart, which the old in-memory dict didn't, but it won't work across multiple server
instances and isn't encrypted at rest. The real fix is the Supabase table already requested in
[member_B.md](member_B.md): `user_id`, `access_token`, `refresh_token`, `token_expiry`, `scope`.

The function signatures (`get_stored_credentials` / `save_credentials`) won't change when it
lands, so nothing else needs touching.

---

## P4 — Phase 6 finish: demo

1. Run BUILD_PLAN §12's four scripted inputs end to end against the merged app and record the
   actual outcomes. Two of them (`"Plan a trip"` → asks for destination; no-GPS → asks for start
   location) should already pass; the `get_user_profile` one can't until P3 lands.
2. Bare chat page or a Postman/CLI flow hitting `POST /trip-plan` and rendering the response.
3. Demo script / walkthrough for the mentor.

---

## P5 — Phase 7 (after Phase 6 is solid)

Per §10's suggested split, Member A's half: NestJS backend (JWT auth, user/trip endpoints proxying
to `POST /trip-plan`, admin endpoints) and the Admin Panel's pending-listings approve/reject view
— the UI for the `is_verified` flag the data pipeline sets to `false`.

Don't start this before P1 is settled — NestJS calls §7's contract, so the contract needs to be
final first.

---

## Suggested order

1. **P0** (weather timezone, `utcnow`, policy guard decision) — small, no dependencies, and the
   timezone one is a real user-visible bug.
2. **P1** (§7 contract) — settle it before anything is built against it.
3. **P3 messages to Member B** — send these early so they're unblocked in parallel while you work.
4. **P2** (error matrix) — the natural "is this actually solid?" pass.
5. **P4** (demo), then **P5** (Phase 7).
