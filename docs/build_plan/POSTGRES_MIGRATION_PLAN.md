# AI Backend — Supabase → PostgreSQL migration plan

**Status: ✅ COMPLETE — implemented and verified against a live PostGIS container on 2026-08-30.**
Kept as the record of what changed and why. See §7 for the verification results.

**Written:** 2026-08-30
**Driver:** the project is moving off Supabase to a Dockerised PostgreSQL (PostGIS) shared with the NestJS backend. See `backend/docs/BACKEND_PLAN.md` §1 (decision 1) and §6.

**Prerequisite:** NestJS **Phase 1** (schema + seed) must land first — this migration needs real
tables to query. Until then, nothing here is blocked *by* the AI backend; it keeps running on its
mock-data fallbacks exactly as it does today.

---

## 1. What's actually changing

The AI backend currently talks to Supabase's **REST API** via the `supabase` Python SDK. A plain
Postgres container has no such REST layer — so every one of those calls would silently fail and
fall through to mock data forever. Six files need real changes.

The goal is a **behaviour-preserving swap**: every public function keeps its exact signature, and
all 110 existing tests keep passing. Nothing outside these files should need to change.

Worth noting this returns the code to what BUILD_PLAN originally asked for:

> *"Supabase is the DB for now but must stay swappable … as long as all DB access goes through
> `app/tools/db_tool.py` and plain SQL/SQLAlchemy rather than the Supabase client SDK directly."*

The current code drifted from that by using the SDK directly. This migration fixes the drift.

---

## 2. Driver choice

| Layer | Driver | Why |
|---|---|---|
| `db_tool.py`, `calendar_tool.py` (async, in the request path) | **`asyncpg`** | Native async, fast, no ORM overhead. Needs adding to `requirements.txt`. |
| `supabase_writer.py` → `postgres_writer.py` (sync, batch ingest) | **`psycopg2`** | These scripts run outside the event loop via APScheduler/`run_in_executor`. **`psycopg2-binary==2.9.12` is already in `requirements.txt`** — no new dependency. |

**Raw SQL, not an ORM.** Prisma (NestJS) owns the schema. Defining SQLAlchemy models here would
duplicate that definition in a second language, and the two would drift the first time someone
runs `prisma migrate` without updating Python. The AI backend's queries are few and read-mostly —
raw parameterised SQL is simpler and has one fewer thing to keep in sync.

---

## 3. File-by-file

### 3.1 `app/tools/db_tool.py` — the main one

**Delete `_parse_ewkb_point()` entirely.** It exists only to decode PostGIS EWKB hex that
Supabase's REST layer returned for `geography` columns. With raw SQL we just ask Postgres for the
numbers directly — either from the generated `latitude`/`longitude` columns
(`backend/docs/BACKEND_PLAN.md` §4.2) or with `ST_Y(...)`/`ST_X(...)` inline. Note those generated
columns exist mainly for *Prisma's* benefit; raw SQL here can call `ST_Y`/`ST_X` freely either way.

**Collapse `_resolve_id()` into a JOIN.** Today `_get_listings()` makes **three** round trips
(resolve district id → resolve category id → fetch listings). One query replaces all three:

```sql
SELECT l.id, l.name, l.description, l.price_range,
       l.latitude, l.longitude, l.rating, l.photo_url, l.opening_hours,
       l.has_public_transit, l.nearest_transit_stop, l.pickme_available
FROM travel_listing l
JOIN district d ON d.id = l.district_id
JOIN category c ON c.id = l.category_id
WHERE d.name ILIKE $1 AND c.name = $2 AND l.is_verified = true
```

Same for events:

```sql
SELECT e.id, e.name, e.description, e.start_datetime, e.end_datetime,
       e.venue_name, e.price_info
FROM local_event e
JOIN district d ON d.id = e.district_id
WHERE d.name ILIKE $1 AND e.is_verified = true
  AND e.start_datetime <= $3 AND e.end_datetime >= $2
```

And the profile:

```sql
SELECT travel_interests, travel_style, default_budget,
       ST_Y(home_location::geometry) AS home_lat,
       ST_X(home_location::geometry) AS home_lon
FROM traveler_profile
WHERE user_id = $1
```

**Replace `_get_client()` with `_get_pool()`** — a lazily-created `asyncpg` pool cached per
process, same shape as today:

```python
_pool: Optional[asyncpg.Pool] = None

async def _get_pool() -> Optional[asyncpg.Pool]:
    global _pool
    if not settings.database_url:
        return None
    if _pool is None:
        _pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=5)
    return _pool
```

Close it on shutdown from `main.py`'s existing `@app.on_event("shutdown")` handler.

**Keep every fallback.** The `if not configured → mock data` and `except → log + mock data` paths
are what let this service (and its test suite) run with no database at all. The gate changes from
`_SUPABASE_AVAILABLE and settings.supabase_url and settings.supabase_key` to simply whether
`settings.database_url` is set and the pool connects.

Unchanged: `get_hotels`, `get_restaurants`, `get_attractions`, `get_events`, `get_user_profile`
signatures; `get_transit_info` and `check_pickme_coverage` stay stubs; all mock data.

### 3.2 `app/tools/calendar_tool.py`

Same pool pattern, two queries against `google_oauth_tokens`:

```sql
-- get_stored_credentials
SELECT access_token, refresh_token, token_expiry, scope
FROM google_oauth_tokens WHERE user_id = $1

-- save_credentials
INSERT INTO google_oauth_tokens (user_id, access_token, refresh_token, token_expiry, scope)
VALUES ($1, $2, $3, $4, $5)
ON CONFLICT (user_id) DO UPDATE SET
  access_token  = EXCLUDED.access_token,
  refresh_token = EXCLUDED.refresh_token,
  token_expiry  = EXCLUDED.token_expiry,
  scope         = EXCLUDED.scope,
  updated_at    = now()
```

Keep the local-JSON-file fallback for when `DATABASE_URL` isn't set or the query fails.

Note `token_expiry` is currently written as an **ISO string** by `google_oauth.py`. Against a real
`timestamptz` column, either parse it to a `datetime` before binding or cast in SQL
(`$4::timestamptz`) — the SDK was lenient about this; asyncpg is not.

### 3.3 `app/data/supabase_writer.py` → rename to `postgres_writer.py`

Sync, `psycopg2`. Six functions:

| Function | Change |
|---|---|
| `get_client()` → `get_connection()` | `psycopg2.connect(settings.database_url)`. Same "return `None` if unconfigured, log a warning" behaviour so ingest still runs without a DB. |
| `get_district_id_map()` | `SELECT id, name FROM district` |
| `get_category_id_map()` | `SELECT id, name FROM category` |
| `to_point_wkt()` | **Unchanged** — already emits PostGIS WKT (`POINT(lon lat)`). |
| `has_column()` | Becomes a real query instead of a try/except probe: `SELECT 1 FROM information_schema.columns WHERE table_name=%s AND column_name=%s`. Cleaner and cheaper than today's version. |
| `upsert_rows()` | `INSERT … ON CONFLICT (col) DO UPDATE SET …`, built dynamically from the row dict's keys. Batch with `psycopg2.extras.execute_values`. |

**⚠️ One real API change — geography columns.** Both ingest jobs pass
`"location": to_point_wkt(lat, lon)` into `upsert_rows`. Supabase's REST layer implicitly parsed
that WKT string into a `geography`; raw SQL will not — it'll try to insert a plain string and
fail. `upsert_rows` needs to know which columns are geography and wrap them:

```python
def upsert_rows(table, rows, on_conflict, geo_columns=frozenset()) -> int:
    # columns in geo_columns bind as ST_GeogFromText(%s) instead of plain %s
```

Call sites to update (both currently pass a `location` key):
- `app/data/events_ingest.py:142` → `upsert_rows("local_event", rows, "external_ref", geo_columns={"location"})`
- `app/data/overpass_ingest.py:370` → same for `travel_listing`

Also update the two `from app.data.supabase_writer import …` lines
(`events_ingest.py:30`, `overpass_ingest.py:22`) to the new module name.

### 3.4 `app/config/settings.py`

Remove `supabase_url` and `supabase_key`. `database_url` already exists — it becomes the single
DB setting.

### 3.5 `requirements.txt`

Add `asyncpg`. Remove `supabase`. `psycopg2-binary` already present — leave it.

---

## 4. Tests

Both affected test files fake a **Supabase-shaped** client — a chainable
`.select().eq().limit().execute()` builder. Those fakes get reshaped to an **asyncpg-shaped** one
(a pool whose `fetch`/`fetchrow` return lists of `Record`-like dicts). Simpler than the current
fakes, since there's no builder chain to mimic.

| File | Work |
|---|---|
| `tests/test_db_tool.py` (9 tests) | Replace `_FakeQuery`/`_FakeSupabaseClient` with a fake pool. **Delete `_make_ewkb_point_hex()`** — no more EWKB. Assertions stay as-is; only the fake changes. |
| `tests/test_calendar_token_store.py` (9 tests) | Same. The `upsert` sink becomes a captured SQL-args list. |

**The 110-test suite must still pass with zero network and no database** — that property is worth
protecting; it's what makes this repo's tests fast and CI-safe.

Worth **adding** while in here: a test that `upsert_rows` wraps `geo_columns` in
`ST_GeogFromText` — that's the one genuinely new behaviour, and the easiest thing to get subtly
wrong.

---

## 5. Cleanup once this lands

- **Delete `docs/db_migrations.sql`.** Its two items (`google_oauth_tokens`, and
  `traveler_profile.default_budget` / `home_location`) are folded into the NestJS Prisma migration
  — see `backend/docs/BACKEND_PLAN.md` §4.1. Keeping a second, divergent source of schema truth is
  exactly the drift this migration is fixing.
- **`Readme.md`** — the env-var table lists `SUPABASE_URL`/`SUPABASE_KEY`; replace with
  `DATABASE_URL`.
- **`docs/NEXT_STEPS.md`** — references Supabase throughout; update the two P1/P2 items.
- **`docs/member_B.md`** — the `google_oauth_tokens` handoff note is now satisfied by the NestJS
  schema; mark it resolved.

---

## 6. Suggested order

1. `settings.py` + `requirements.txt` (add `asyncpg`, drop `supabase`) — smallest step, unblocks the rest.
2. `calendar_tool.py` — only two queries, and its tests are the simplest. Good place to establish the pool pattern and the fake shape.
3. `db_tool.py` — the biggest change, but it reuses the pattern from step 2.
4. `postgres_writer.py` + the two ingest call sites — sync/psycopg2 side, independent of 2–3.
5. Docs cleanup (§5).

Steps 2–3 and step 4 are independent and can be split between two people.

---

## 7. Verification results — 2026-08-30

Run against a live `postgis/postgis:16-3.4` container using a throwaway schema (dropped
afterwards, so Prisma still gets a clean slate for its first migration).

- [x] `pytest` — **127 tests pass** (up from 110), still with no network and no database.
- [x] With `DATABASE_URL` **unset**: mock data and the local JSON token file still serve
      everything. Fallbacks intact.
- [x] With `DATABASE_URL` **set**: `get_hotels("Kandy")` returned the seeded row with real
      coordinates (`7.2931, 80.6392`) read straight from the generated columns. Case-insensitive
      district matching (`ILIKE`) confirmed, and a deliberately `is_verified = false` row was
      correctly **excluded**.
- [x] `get_user_profile()` returned a seeded profile end-to-end:
      `{'interests': ['culture','food'], 'travel_style': 'budget', 'budget': 300.0,
      'home_location': {'lat': 6.9271, 'lon': 79.8612}}` — `text[]`, `numeric`→`float`, and
      `ST_Y`/`ST_X` all mapping correctly.
- [x] Calendar tokens round-tripped through `google_oauth_tokens`, including the ISO-string →
      `timestamptz` coercion.
- [x] `upsert_rows(..., geo_columns={"location"})` wrote a real geography: stored as
      `POINT(80.6413 7.2936)`, generated columns derived `7.2936 / 80.6413`, and a **second upsert
      of the same `external_ref` updated in place rather than duplicating** (1 row, not 2).
      `has_column()` correctly distinguished an existing column from a missing one.
- [x] `grep -ri supabase app/ tests/ requirements.txt` returns nothing.

### Two bugs the fakes could not have caught

Both were found only by running against a real database — worth remembering as an argument for
doing this kind of verification rather than trusting mocks alone.

1. **`get_events` passed ISO date strings straight to asyncpg**, which raised
   `invalid input for query argument $2: '2026-08-20' (expected a datetime.date or
   datetime.datetime instance, got 'str')`. The Supabase REST layer coerced these silently;
   asyncpg does not. Fixed with `_coerce_date()`, and pinned by
   `test_get_events_binds_dates_as_datetimes_not_strings`.
2. **Mock events keyed the title as `title` while real rows use `name`** (BUILD_PLAN §4's
   contract), so `RecommendationAgent` silently received a differently-shaped dict depending on
   whether the database was up. Mock data corrected to `name`; pinned by
   `test_mock_events_use_the_name_key_like_real_rows`.

### Still outstanding

- Running the real `overpass_ingest` / `events_ingest` jobs end-to-end against the live schema —
  the writer itself is verified, but a full ingest run needs the real Prisma-owned schema first.
- BUILD_PLAN §12's "Plan a trip to Kandy" scripted case still needs a seeded `traveler_profile`
  row in the real schema to pass end-to-end. The code path is now proven; only the data is missing.

---

## 8. Risks

| Risk | Mitigation |
|---|---|
| **Geography binding in `upsert_rows`** (§3.3) — the one behaviour Supabase was doing implicitly. | Explicit `geo_columns` param + a dedicated test. Highest-value thing to get right here. |
| **Two services writing `travel_listing`** — ingest jobs insert unverified rows, NestJS admin approves them. | Upsert on `external_ref` (already the `on_conflict` key). Never assume sole ownership; don't `DELETE` rows the other service may own. |
| **`token_expiry` type** — ISO string vs `timestamptz`; asyncpg is stricter than the SDK was. | Parse to `datetime` before binding, or cast in SQL. Covered in §3.2. |
| **Silent fallback masking a broken connection** — a misconfigured `DATABASE_URL` looks identical to "no DB configured": everything quietly serves mock data. | Log at `WARNING` (not `DEBUG`) when a *configured* connection fails, distinct from the "not configured" message. The verification checklist's third item catches this. |
| **Schema drift** — Prisma owns the schema; these raw queries name columns directly. | The verification checklist doubles as a smoke test after any future `prisma migrate`. |
