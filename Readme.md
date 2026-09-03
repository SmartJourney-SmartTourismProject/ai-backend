# SmartJourney AI Backend

AI Backend for the SmartJourney platform — a multi-agent trip-planning system. Given a
natural-language request ("Plan a 3-day trip to Kandy, budget $300, love hiking"), it runs a
LangGraph pipeline (validate → policy → slot-fill → location → calendar → weather/disaster →
recommend → plan → respond) and returns a day-by-day itinerary with map-plottable coordinates,
weather/disaster context, and an estimated cost. Supports multi-turn conversation — a follow-up
message ("make it cheaper") refines the same trip instead of starting over.

**Scope:** this repo is the Python/FastAPI AI service only. The NestJS backend and the web/mobile
UI are separate repositories.

## Technology Stack

- FastAPI
- LangGraph
- Gemini (via `langchain-google-genai`)
- PostgreSQL + PostGIS (shared with the NestJS backend; falls back to mock data if not configured)
- Python

---

## Setup

1. **Create and activate a virtual environment:**

   ```powershell
   python -m venv .venv
   .venv\Scripts\Activate.ps1
   ```

   If PowerShell blocks the activation script, run once:
   ```powershell
   Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
   ```

2. **Install dependencies:**

   ```powershell
   pip install -r requirements.txt
   ```

3. **Configure environment variables** — copy `.env.example` to `.env` and fill in at least:

   | Variable | Required for |
   |---|---|
   | `GEMINI_API_KEY` | Slot-filling and itinerary generation (core feature — required) |
   | `OPENWEATHER_API_KEY` | Weather forecasts (optional — degrades to no weather data without it) |
   | `DATABASE_URL` | Real listings/user/profile data and calendar-token storage (optional — falls back to built-in mock data for Kandy/Ella/Colombo/Galle and a local JSON token file without it). Points at the same database the NestJS backend owns; start it with `docker compose up -d` in `backend/`. |
   | `GOOGLE_CALENDAR_CLIENT_ID` / `_SECRET` | Google Calendar OAuth (optional — calendar features fall back to "no calendar connected") |
   | `REDIS_URL` | Weather/disaster caching (optional — cache fails open, always a live fetch without it) |

   Everything else in `.env.example` is optional; the app runs and degrades gracefully without it.
   No key at all is needed for `GEMINI_API_KEY` to run tests — only for actually calling the API.

---

## Running the server

**Always run through the virtual environment**, not a global/system Python install — the classic
symptom of forgetting this is `ModuleNotFoundError: No module named 'dotenv'` on startup.

```powershell
.venv\Scripts\Activate.ps1
python -m uvicorn main:app --reload
```

or, without activating first:
```powershell
.venv\Scripts\uvicorn.exe main:app --reload
```

Server runs at `http://localhost:8000`. Check `GET /` for a health check, and `GET /docs` for the
interactive Swagger UI (includes `POST /trip-plan`, the Google Calendar OAuth routes, and the RAG
indexing/data-sync admin endpoints).

**If port 8000 is already in use** and nothing shows up in `netstat`/Task Manager holding it,
that's a known odd Windows quirk this project has hit before — just run on a different port:
```powershell
python -m uvicorn main:app --port 8001
```

---

## Running the tests

```powershell
pytest
```

120+ tests, no network calls, no API keys needed, runs in a few seconds. All external services
(Gemini, OpenWeather, EONET/USGS/GDACS, PostgreSQL, Google Calendar, Nominatim) are mocked.

---

## Running the demo UI

`demo/index.html` is a small, self-contained, independent demo page — a text box for trip
requests, the generated itinerary, and a map (Leaflet + OpenStreetMap, no API key needed) plotting
the route with a pin per stop. It's not wired into the FastAPI app at all, so it can be deleted
without touching any app code.

1. **Start the API server first** (see above).

2. **Serve the demo folder over HTTP** — don't just double-click `index.html` to open it as a
   `file://` page; some browsers block the page's requests to the API from a `file://` origin with
   no visible error. Instead:

   ```powershell
   cd demo
   python -m http.server 5500
   ```

   Then open **`http://127.0.0.1:5500/index.html`** in your browser.

3. In the page header, make sure **"API base URL"** matches wherever the server is actually
   running (`http://localhost:8000` by default, or whatever port you used).

4. Type a trip request and hit Send. Try a follow-up message afterwards (e.g. "make it cheaper")
   to see multi-turn modification in action — it reuses the same `session_id` automatically.

### Example prompts to try

Matched to the built-in mock data, so they'll produce a real itinerary:

- `Plan a 3-day trip to Kandy, budget $300, interested in culture and history`
- `Plan a 2-day trip to Ella, budget $200, I love hiking and nature`
- `Plan a 3-day trip to Galle, budget $400, interested in beach and history`
- `Plan a weekend trip to Colombo, budget $250, interested in culture and museums`

Special requirements (shows the traveler's raw message reaching the AI, not just structured fields):
- `Plan a 2-day trip to Kandy, budget $300, I'm vegetarian and can't walk long distances`

No destination (shows the clarification/ask-back flow):
- `Plan me a trip somewhere nice`

Follow-ups (send after any of the above completes):
- `Actually make the budget $600 instead`
- `Swap the temple visit for something more relaxing`

A deliberately unmatched combo, to see graceful failure rather than a fake result:
- `Plan a 2-day trip to Kandy, budget $300, love hiking` — Kandy's mock data has no "hiking" tag
  (only Ella does), so this correctly reports "no verified listings found" instead of hallucinating one.

### Common gotcha: Gemini's free-tier daily quota

The free Gemini API tier allows **20 requests per day per model**. Heavy demo/test usage can
exhaust this — symptoms are requests that hang for a long time and eventually fail with
`429 RESOURCE_EXHAUSTED`. Fixes: wait for the daily reset, use a different/paid API key, or (for
automated testing) rely on the mocked test suite instead of live calls.

---

## Documentation

- [`docs/BUILD_PLAN.md`](docs/BUILD_PLAN.md) — the original architecture/contract plan
- [`docs/NEXT_STEPS.md`](docs/NEXT_STEPS.md) — current status and what's left to do
- [`docs/POSTGRES_MIGRATION_PLAN.md`](docs/POSTGRES_MIGRATION_PLAN.md) — the Supabase → PostgreSQL migration (done)
- [`../backend/docs/BACKEND_PLAN.md`](../backend/docs/BACKEND_PLAN.md) — the NestJS backend plan, including the shared database schema
- [`docs/member_B.md`](docs/member_B.md) — cross-track handoff notes
