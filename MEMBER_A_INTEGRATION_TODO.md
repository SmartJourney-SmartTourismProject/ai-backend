# Integration TODO — for Member A (Shaluka)

**Context:** this branch (`thisuri-copy`, Member B/Thisuri's work) used to contain its own
duplicate implementation of your Orchestrator track under `app/workflows/` — `orchestrator.py`,
`policy_agent.py`, `calendar_agent.py`, `context_agent.py` — plus a duplicate
`app/utils/slot_filling.py`. Those have been removed (see [CLEANUP_PLAN.md](CLEANUP_PLAN.md)) so
this branch only carries Member B's real scope: `db_tool.py`, `recommendation_agent.py`,
`planning_agent.py`, the `app/data/*` ingestion pipeline, `app/rag/*`, `app/prompts/*`,
`app/scheduler.py`. This file is what your `temp-shaluka` branch needs to bring over — and match —
for the two branches to merge cleanly.

## 1. Files this branch now expects from you, at these exact paths

Per your own [MEMBER_A_REPORT.md](MEMBER_A_REPORT.md), you already build these at the paths
Member B's code expects — nothing to rename on your side:

| Path | Status on your branch | Needed by |
|---|---|---|
| `app/core/orchestrator.py` | Built (§3.11) | `main.py`'s `/api/plan-trip` should call this once it's here — see §2 below |
| `app/utils/policy_guard.py` | Draft (§3.5) | orchestrator wiring |
| `app/tools/location_tool.py` | Built (§3.6) | orchestrator wiring |
| `app/tools/calendar_tool.py` | Built (§3.7) | orchestrator wiring |
| `app/tools/weather_tool.py` | Built (§3.8) | orchestrator wiring — sets `state.weather` |
| `app/tools/disaster_tool.py` | **Not built yet** (§3.9) | orchestrator wiring — sets `state.disaster` |
| `app/utils/slot_filling.py` | Built (§3.10) | orchestrator wiring |
| `app/api/trip.py`, `app/api/google_oauth.py` | Built (§3.12–§3.13) | Would replace `main.py`'s current `/api/plan-trip` — see §2 |

## 2. What `main.py` on this branch does *without* your orchestrator

`main.py`'s `/api/plan-trip` currently calls `RecommendationAgent` directly — no
validate/policy/location/calendar/weather/disaster steps run. It requires `destination` in the
request body and does nothing if it's missing (just a 422), where your real flow is supposed to
*ask* the user for it (BUILD_PLAN §2). `state.weather`/`state.disaster` stay `None` unless the
caller passes them explicitly. This is a temporary stand-in for testing Member B's own agents, not
a real endpoint. Once `app/core/orchestrator.py` is available on the merged branch, whoever does
the merge should either:
- swap `main.py`'s `/api/plan-trip` handler back to calling your compiled graph, or
- adopt your `app/api/trip.py` wholesale (it already does this correctly, including GPS/IP
  location resolution before building `TripState` — see your report §3.12) and retire the
  version in `main.py`.

Either way, **`recommendation_agent.py`/`planning_agent.py` don't need to change** — they already
consume `TripState` fields (`destination`, `weather`, `disaster`, `trip_dates`, etc.) exactly as
your tools populate them; the seam is `app/core/orchestrator.py` calling
`RecommendationAgent().execute(state)` as its `recommend` node (your report §3.11 already shapes
the stub nodes this way).

## 3. Things that will conflict at merge time — reconcile, don't silently pick one

- **`app/config/settings.py`** has diverged. Yours has `gemini_api_key`,
  `google_calendar_redirect_uri`, `eventbrite_api_key`; this branch's has `google_api_key` (no
  `eventbrite_api_key`, no calendar redirect URI, but adds `supabase_url`/`supabase_key`,
  `redis_url`, `booking_rapidapi_key/host`, RAG/vector-store settings, security settings). Both
  sets of keys are genuinely needed once merged — this isn't a duplicate to delete, it's two people
  extending the same file. Needs a manual field-by-field merge, not an overwrite in either
  direction.
- **`.env`** on this branch is actually a copy of *your* `.env` (matches your key list exactly:
  `GEMINI_API_KEY`, `DATABASE_URL`, `OPENWEATHER_API_KEY`, `GOOGLE_MAPS_API_KEY`,
  `TICKETMASTER_API_KEY`, `EVENTBRITE_API_KEY`, `YELP_FUSION_API_KEY`,
  `GOOGLE_CALENDAR_CLIENT_ID/SECRET/REDIRECT_URI`) — it has no `GOOGLE_API_KEY`, `SUPABASE_URL`,
  `SUPABASE_KEY`, or `REDIS_URL`, all of which Member B's `settings.py` reads. Anyone running this
  branch's LLM-backed tests locally needs to add those before `recommendation_agent.py`/
  `planning_agent.py` will authenticate.
- **Gemini model name**: you've already flagged `gemini-3.6-flash` (not `gemini-2.5-flash`, which
  404s on new keys) to Member B per your report — confirmed, this branch's
  `settings.py.llm_model` already uses `gemini-3.6-flash`. No action needed, just noting it's in
  sync.

## 4. Where your not-yet-built `disaster_tool.py` matters to Member B's code

`recommendation_agent.py` and `planning_agent.py` both already read `state.disaster` (they pass it
straight into the LLM payload as `disaster_warnings`, per BUILD_PLAN's Planner prompt in §5) and
degrade fine when it's `None`/missing — no code change needed on Member B's side once you ship the
real tool. Just make sure `get_disaster_info()`'s return shape matches BUILD_PLAN §4 exactly
(`{"safe": bool, "active_events": [...]}`) since that's the shape the recommendation/planning
prompts implicitly expect via `state.disaster`.

One implementation reference you may find useful: the (now-deleted) `context_agent.py` on this
branch had a working EONET + USGS + GDACS fetch — same endpoints, params, and severity mapping
BUILD_PLAN §1/§4 call for, just organized as a class method instead of your `weather_tool.py`/
`disaster_tool.py` function-per-file split. Recoverable from git history at the commit before this
cleanup (`git show <pre-cleanup-sha>:app/workflows/context_agent.py`) if it saves you time,
particularly the GDACS GeoRSS-XML parsing (`_fetch_gdacs_alerts`) and the haversine distance filter
(`_distance_km`) — the rest of the class won't apply since your split is per-file. The commit right
before this cleanup is `ce5eb4f` (`git show ce5eb4f:app/workflows/context_agent.py`).

## 5. Suggested merge order

1. Bring your `app/core/orchestrator.py`, `app/tools/*.py`, `app/utils/policy_guard.py`,
   `app/utils/slot_filling.py`, `app/api/*.py` onto this branch (or vice versa) — no path
   conflicts, since this branch's duplicates of them are already gone.
2. Reconcile `app/config/settings.py` and `.env` per §3.
3. Build `app/tools/disaster_tool.py` per §4 above and BUILD_PLAN §4/§1.
4. Wire `app/core/orchestrator.py`'s `recommend`/`plan` nodes to
   `app.workflows.recommendation_agent.RecommendationAgent` /
   `app.workflows.planning_agent.PlanningAgent` (already shaped as `BaseAgent` subclasses on this
   branch — no interface change needed on either side).
5. Retire `main.py`'s temporary direct-call endpoint (§2) in favor of your `app/api/trip.py`.
6. Re-run this branch's `test_phase2_agents.py` end-to-end through the real graph instead of
   calling `RecommendationAgent` directly, and BUILD_PLAN §12's scripted inputs.
