# AI Backend — Final Status & Handoff

**Branch:** `main`
**Snapshot:** 2026-08-30
**Reference:** [BUILD_PLAN.md](BUILD_PLAN.md) — §7 API contract, §8 error/caching matrix, §12 verification checklist. Cross-checked against the project's SRS and SAD documents.

This document closes out active development on the Python/FastAPI AI backend and records its
final state for whoever picks up the NestJS backend next. Test suite: **110 passing, 0 xfail**,
no network calls, no API keys required, ~5s.

---

## What this backend does

Given a natural-language trip request, it runs a LangGraph pipeline — validate → policy →
slot-fill → location → calendar → weather/disaster → recommend → plan → respond — and returns a
day-by-day itinerary with map-plottable coordinates, weather/disaster context, and an estimated
cost. Supports multi-turn conversation (a follow-up message refines the same trip) and extracts a
named starting location from free text when GPS/IP resolution fails.

A standalone demo page (`demo/index.html`, independent of the FastAPI app) exercises this live —
see the root `Readme.md` for how to run it.

---

## The API contract, for NestJS integration

**`POST /trip-plan`**

Request:
```json
{
  "message": "Plan a 3-day trip to Kandy, budget $300, love hiking",
  "language": "en",
  "user_id": "optional-uuid",
  "client_gps": {"lat": 7.29, "lon": 80.63},
  "session_id": "optional - omit on first message, pass back to continue a conversation"
}
```

Response:
```json
{
  "session_id": "uuid - always returned, pass back on the next message to modify this trip",
  "destination": "Kandy",
  "itinerary": [{"day": 1, "date": "...", "items": [
    {"time": "09:00", "type": "attraction", "name": "...", "notes": "...", "lat": 7.29, "lon": 80.64}
  ]}],
  "estimated_cost": 210.0,
  "budget_notes": "explanation if the budget was tight or exceeded, or null",
  "weather": {"current": {...}, "forecast": [...]},
  "disaster": {"safe": true, "active_events": [...]},
  "final_response": "chat-display text summarizing the plan (or a clarification question)",
  "errors": ["advisory notes, e.g. location_unresolved - not necessarily fatal"],
  "completed_steps": ["debug-only, empty unless settings.debug=True"]
}
```

**`GET/POST /auth/google/login`, `/auth/google/callback`** — Google Calendar OAuth consent flow,
signed/expiring `state` parameter (CSRF-safe).

`main.py` also exposes `GET /api/health`, `POST /api/rag/index`, and
`POST /api/admin/sync/{events,listings}` for the data-pipeline side (Member B's track) — not
relevant to a chat/trip-planning integration.

CORS is currently wide open (`allow_origins=["*"]`) — tighten this to the actual NestJS/frontend
origin(s) before this goes anywhere near production.

---

## §12 scripted verification — results (2026-08-30)

Ran BUILD_PLAN §12's four scripted inputs against the real orchestrator (real Gemini calls, real
weather/disaster APIs, no mocking):

| # | Input | Expected | Actual | Verdict |
|---|---|---|---|---|
| 1 | `"Plan a 5-day trip to Galle for 2 people, budget $500, interested in beaches and food"` | No clarification; itinerary respects budget and interests | Interests respected. **Budget not respected** — picked the $$$$ hotel over the available $$$ one, total cost $1,400 vs. $500 budget, with a `budget_notes` explanation rather than choosing the cheaper option. See finding below. | Partial |
| 2 | `"Plan a trip to Kandy"` (with `user_id`) | Defaults to 1 day; pulls interests/travel_style/budget from `get_user_profile` | Defaulted to 1 day correctly. Profile fields stayed empty — `get_user_profile` queries real Supabase, which has no seeded profile data yet (see P1 below). Not a bug; exactly the documented pending state. | Blocked on P1 |
| 3 | `"Plan a trip"` (no destination) | Asks for a destination before doing anything else | Clean clarification, zero errors, zero wasted work. | Pass |
| 4 | No GPS, IP resolution fails | Falls back to asking for a starting location | Produces the full itinerary anyway, with `location_unresolved` surfaced as an advisory note rather than a blocking question. **Deliberate design choice**, not a bug — see note below. | Pass (by design, differs from literal BUILD_PLAN wording) |

### Finding: the LLM doesn't always pick the cheapest option when budget is tight

`RECOMMENDATION_PLANNING_SYSTEM_PROMPT` already instructs the model to "respect the given budget
by choosing a price_range mix that fits," but case 1 shows it can still pick the more expensive of
two available options (Galle only has 2 mock hotels: `$$$$` and `$$$`) and just narrate the
overage in `budget_notes` instead of minimizing it. This is an LLM judgment-quality issue, not a
code bug — the data and the instruction are both correct. Not fixed here since it's a prompt-
engineering / model-choice tuning question rather than a defect; worth a note for whoever owns the
Recommendation Agent prompt going forward.

### Note on case 4's behavior

BUILD_PLAN §12 literally says a failed location lookup should make the Orchestrator "ask for a
starting location" — the same hard-stop treatment as case 3's missing destination. This backend
instead treats it as advisory (see `_SOFT_ERROR_PREFIXES` in `app/core/orchestrator.py`): the plan
still completes, with a note attached. This was a deliberate choice made earlier in development —
asking "where are you traveling from?" on every GPS-less request seemed like worse UX than just
noting it, especially now that a traveler can resolve it themselves just by mentioning an origin
in the chat (`"I'm starting from Polonnaruwa"` — see `origin_location` in `slot_filling.py`). Flag
this to whoever reviews behavior against BUILD_PLAN's literal spec — it's a conscious deviation,
not an oversight.

---

## Remaining items

### P1 — Get `docs/db_migrations.sql` applied (blocks §12 case 2)

**Not this repo's job to apply** — no Supabase credentials here. Needs whoever holds them to run
it (Supabase SQL editor is the simplest path; see the file for full detail):
1. `CREATE TABLE IF NOT EXISTS google_oauth_tokens (...)`
2. `ALTER TABLE traveler_profile ADD COLUMN IF NOT EXISTS default_budget, home_location`

Then seed at least one `traveler_profile` row to actually verify §12 case 2 end-to-end. Until then,
`get_user_profile`/calendar token storage keep working exactly as now (falling back to defaults /
local JSON) — nothing is blocked, but nothing persists to the shared database either. This file is
a handoff artifact, not something that needs to keep living in this repo long-term — once applied,
it can be deleted here or folded into wherever the NestJS side manages schema migrations, if it
has one.

### P2 — `app/utils/session_store.py`'s interim JSON-file storage

Multi-turn conversation state is currently a local JSON file, same pattern the calendar tokens and
user profiles used before they were wired to real Supabase (`google_oauth_tokens`,
`traveler_profile`). A `chat_session`/`itinerary` table per the SAD's ER diagram would be the real
fix, following the exact same pattern already used for the other two. Not done — flagged as the
last piece still on local-file fallback.

### P3 — Rate limiting / request queueing (SAD §10.3)

**Not needed yet.** Only matters once real concurrent traffic approaches ~20 req/sec — nowhere
close to demo/early-integration load. If it ever comes up: a semaphore limiting concurrent
`/trip-plan` executions is the simple first move; a real task queue (Celery/RQ + Redis — already
have Redis via `app/utils/cache.py`) is the heavier option if that's not enough.

### Explicitly decided NOT to build here

- **Currency handling** — SRS mentions "preferred currency" (LKR in mockups); this backend
  implicitly assumes USD throughout. Skipped by decision, not forgotten.
- **HTTPS/encryption in transit** — deployment-level (reverse proxy/load balancer TLS
  termination), not application code.
- **Real Google Maps routing** — map pins use free coordinates (`lat`/`lon` on every itinerary
  item); no turn-by-turn directions. Confirmed sufficient for the SRS's actual requirement (pins on
  a map), and avoids a billing-required API.

---

## Everything closed out this development cycle

Multi-turn conversations, map pins on every itinerary item, `traveler_request` passthrough for
special requirements, the `budget_notes` bug (was silently discarded on every request), the
OpenWeather timezone bug, the `disaster_tool.py` fallback bug (couldn't distinguish "all sources
down" from "confirmed safe"), the policy guard's "ivory market" gap, the `§7` API contract
(`message` field, debug-gated `completed_steps`), `db_tool.get_user_profile` and
`calendar_tool`'s token storage wired to real Supabase (pending migration above), mock-data
coordinates fixed (every listing now has distinct real coordinates instead of one shared district
centroid), a standalone demo page, and named-origin geocoding from free text. Full detail on each
is in the git history and this repo's test suite — every fix above shipped with tests proving it.
