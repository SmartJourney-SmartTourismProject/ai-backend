# Next Steps — post-merge plan

**Branch:** `main` (after `temp-shaluka` merged in at `bfd1805`)
**Last updated:** 2026-08-29
**Reference:** [BUILD_PLAN.md](BUILD_PLAN.md) — §7 API contract, §8 error/caching matrix, §10 phases, §12 verification checklist. Also cross-checked against the project's SRS and SAD documents.

**Scope note:** this repo is the Python/FastAPI multi-agent AI system only. The NestJS backend and
the web/mobile UI are separate repos and out of scope here — anything that's really their job
(JWT auth, budget tracker persistence, subscriptions, admin panel, notification delivery, HTTPS
termination) is noted as such below, not built here.

Test suite: **101 passing, 0 xfail**, no network calls, no API keys, ~5s.

---

## Done since the merge

- **Multi-turn conversations** (SRS §3.1.6/§4.1.5 "Modify Trip Plan"). `session_id` in/out of
  `/trip-plan`, `app/utils/session_store.py` carries a trip's state between turns, `slot_filling.py`
  overwrites fields on a follow-up instead of only filling gaps, `RecommendationAgent` refines the
  existing itinerary instead of rebuilding from scratch.
- **Map pins** — every itinerary item carries `lat`/`lon` (confirmed no real Google Maps routing
  needed, just pins).
- **`traveler_request` passthrough** — the raw message reaches `RecommendationAgent` on every call,
  not just follow-ups, so special requirements that don't map to a structured slot get used.
- **`budget_notes` bug fixed** — was silently discarded on every request (overwritten by
  `_respond_node` immediately after being set). Now its own field, surfaced in the response.
- **OpenWeather timezone bug** fixed (UTC `dt_txt` instead of server-local time).
- **`datetime.utcnow()` deprecation** cleared.
- **Policy guard decision made and shipped** — substring blocklist kept as a deliberate, documented
  trade-off; "ivory market" gap closed with specific phrases (not a bare "ivory" keyword, which
  would've false-positived on "Ivory Coast").
- **§7 API contract settled (P1, was open — now done):** `TripPlanRequest.message` replaces
  `user_input` at the API boundary (internal `TripState.user_input` name unchanged - only the
  external contract moved). `completed_steps` now gated behind `settings.debug` instead of always
  public. Added `tests/test_trip_api.py` — the endpoint itself had zero test coverage before this.
- **`db_tool.get_user_profile` implemented for real** (was a P3 "blocked on Member B" stub) — now
  queries Supabase's `traveler_profile` table first, falls back to empty defaults, same pattern as
  every other lookup in that file. Needs two new columns Supabase doesn't have yet
  (`default_budget`, `home_location`) — see `docs/db_migrations.sql`.
- **`google_oauth_tokens` table wired in** (was the other P3 item) — `calendar_tool.py` now tries
  Supabase first (including `token_expiry`/`scope`, both threaded through from the OAuth callback
  and the token-refresh path), falls back to the local JSON file if Supabase isn't configured or
  errors. Table DDL is in `docs/db_migrations.sql` for Member B to review/run — this repo can't
  apply it directly.
- Test coverage added that didn't exist before: `tests/test_recommendation_agent.py` (8 tests),
  `tests/test_session_store.py`, `tests/test_trip_api.py` (6 tests), extended
  `tests/test_db_tool.py` and `tests/test_calendar_token_store.py` for the real-Supabase paths,
  plus follow-up-turn cases in `tests/test_slot_filling.py`.

**Explicitly decided NOT to do here**, per discussion:
- **Currency handling** (SRS mentions "preferred currency," mockups show LKR) — skipped.
- **HTTPS/encryption in transit** (SRS §3.4.6) — not application code; TLS termination at whatever
  reverse proxy/load balancer sits in front of this service at deploy time.
- **Rate limiting/queueing** (SAD §10.3) — deferred, see P4 below.

---

## Where we actually are

| Phase | Status |
|---|---|
| Phase 1 — Foundations & contracts | Complete |
| Phase 2 — Parallel build | Complete |
| Phase 3 — Data pipeline (Member B) | On `main` — Overpass/events ingest + scheduler present |
| Phase 4 — Integration | Complete |
| Phase 5 — API layer + testing | Complete |
| Phase 6 — Error matrix, caching, demo | Caching done. Error-matrix walkthrough + demo script outstanding |
| Phase 7 — NestJS + Admin Panel | Separate repo, not started here (correctly gated on Phase 6) |

---

## P1 — Phase 6: walk the §8 error matrix

Go tool by tool through [BUILD_PLAN.md §8](BUILD_PLAN.md) and confirm each failure mode degrades
rather than crashes. Most already have a failure-mode test; the gaps:

| Source | Failure test exists? |
|---|---|
| OpenWeather | Yes |
| EONET / USGS / GDACS | Partly — see the finding below |
| Google Calendar | Yes |
| Location / IP lookup | Yes |
| Supabase — listings (`get_hotels`/etc.) | No — falls back to mock data on exception, untested |
| Supabase — `traveler_profile` / `google_oauth_tokens` | Yes — `test_get_user_profile_supabase_error_falls_back_to_defaults`, `test_supabase_error_falls_back_to_local_file` |
| Gemini (slot filling) | Yes |
| Gemini (Recommendation/Planner) | Yes |

### The disaster-tool finding (still open)

**File:** [app/tools/disaster_tool.py](../app/tools/disaster_tool.py)

`get_disaster_info`'s "all three sources failed" fallback is unreachable in practice — each
`_fetch_*` catches its own exceptions internally and returns `[]`, so "all three APIs are down" and
"confirmed zero disasters nearby" produce identical output today. Documented (not fixed) in
[tests/test_disaster_tool.py](../tests/test_disaster_tool.py).

**Fix:** have each `_fetch_*` return `None` on failure vs. `[]` on confirmed-empty, then check
`all(r is None for r in results)`.

---

## P2 — Get the DB migration reviewed and applied

**File:** [docs/db_migrations.sql](db_migrations.sql)

Written up but **not run against the real Supabase project** — this repo has no direct DB access.
Needs Member B (or whoever holds Supabase credentials) to review and apply:
1. `CREATE TABLE IF NOT EXISTS google_oauth_tokens (...)`
2. `ALTER TABLE traveler_profile ADD COLUMN IF NOT EXISTS default_budget, home_location`

Until this runs, `get_user_profile`/calendar token storage keep working exactly as before (falling
back to defaults / the local JSON file) — nothing is blocked, but nothing actually persists to the
shared database either.

---

## P3 — Phase 6 finish: demo

1. Run BUILD_PLAN §12's scripted inputs end to end and record actual outcomes — all four should
   now be checkable including the `get_user_profile` one, once P2's migration lands.
2. Bare chat page or a Postman/CLI flow hitting `POST /trip-plan`.
3. Demo script / walkthrough for the mentor.

---

## P4 — Rate limiting / request queueing (SAD §10.3) — after everything else works end to end

**Not now.** Only matters once real concurrent traffic gets close to the ~20 req/sec figure SAD
names — nowhere close to capstone-demo load. Revisit **only if there's time left after P1–P3 and
the whole pipeline is solid end to end.** If it comes up: a semaphore limiting concurrent
`/trip-plan` executions is the simple first move; a real task queue (Celery/RQ + Redis — already
have Redis via `app/utils/cache.py`) is the heavier option if that's not enough.

---

## P5 — Phase 7 (separate repo, after Phase 6 is solid here)

NestJS backend + Admin Panel — not this repo. The §7 contract this depends on is now settled (P1
from the previous snapshot, done).

---

## Suggested order

1. **P2** (get the migration reviewed/applied) — send it to Member B now so it's not a last-minute
   blocker before the demo.
2. **P1** (error matrix / disaster_tool fix) — the "is this actually solid" pass.
3. **P3** (demo).
4. **P4** (rate limiting) — only if time remains after the above.
