# Data Platform — Docker, Schema, Ingestion, and Killing the Mocks

Companion to [`PROJECT_MASTER_PLAN.md`](PROJECT_MASTER_PLAN.md). Implements concerns **#1, #3, #12**
and the "all data from the internal database" requirement.

Everything the agents know about places comes from here. If it isn't in a table, the system does not
say it.

---

## 1. Docker

`backend/docker-compose.yml` — extends the existing file rather than replacing it (the PostGIS image
and healthcheck already there are correct).

```yaml
services:
  db:
    image: postgis/postgis:16-3.4
    container_name: smartjourney_postgres
    environment:
      POSTGRES_USER: ${POSTGRES_USER}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
      POSTGRES_DB: ${POSTGRES_DB}
    ports: ["${POSTGRES_PORT:-5432}:5432"]
    volumes:
      - db_data:/var/lib/postgresql/data
      - ./db/init:/docker-entrypoint-initdb.d:ro     # extensions only; migrations run separately
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${POSTGRES_USER} -d ${POSTGRES_DB}"]
      interval: 5s
      timeout: 5s
      retries: 10
    restart: unless-stopped

  redis:
    image: redis:7-alpine
    container_name: smartjourney_redis
    ports: ["${REDIS_PORT:-6379}:6379"]
    command: ["redis-server", "--save", "", "--appendonly", "no"]   # pure cache, no persistence
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 3s
      retries: 10
    restart: unless-stopped

volumes:
  db_data:
```

`db/init/00_extensions.sql`:

```sql
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS pg_trgm;      -- fuzzy place-name matching
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
```

Redis is added because `app/utils/cache.py` already expects it and currently no-ops without it —
which silently turns off weather/disaster caching and multiplies external API calls.

> **Known gotcha, already hit on this project** (recorded in `backend/docs/BACKEND_PLAN.md` §7): Postgres
> only runs its init scripts on an **empty** data directory. If `db_data` survives from an earlier
> run, new credentials and `db/init` are ignored. Either `docker compose down -v`, or create the
> role by hand inside the existing cluster.

---

## 2. Schema

Canonical DDL lives in `backend/db/migrations/`. Plain SQL, applied by `backend/db/migrate.py`,
tracked in `schema_migration`. NestJS later runs `prisma db pull` against this — it introspects, it
does not own (decision D13).

### `0000_meta.sql`

```sql
CREATE TABLE IF NOT EXISTS schema_migration (
    filename    text PRIMARY KEY,
    applied_at  timestamptz NOT NULL DEFAULT now(),
    checksum    text NOT NULL
);
```

### `0001_core.sql` — everything the AI backend needs

```sql
-- ─────────────────────────── reference ───────────────────────────

CREATE TABLE district (
    id            uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
    name          text NOT NULL UNIQUE,              -- "Kandy"
    province      text NOT NULL,
    osm_relation_id bigint UNIQUE,                   -- provenance; NOT a hardcoded list
    center        geography(Point,4326) NOT NULL,
    boundary      geometry(MultiPolygon,4326),       -- geometry (not geography) for ST_Contains
    source        text NOT NULL DEFAULT 'osm',
    updated_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX district_boundary_gix ON district USING gist (boundary);
CREATE INDEX district_center_gix   ON district USING gist (center);
CREATE INDEX district_name_trgm    ON district USING gin (name gin_trgm_ops);

CREATE TABLE category (
    id    uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
    name  text NOT NULL UNIQUE                       -- hotel | restaurant | attraction
);

-- canonical interest tags + how raw OSM/Foursquare tags map onto them
CREATE TABLE tag_vocabulary (
    tag         text PRIMARY KEY,                    -- "culture", "beach", "hike", "food"
    label       text NOT NULL,
    is_outdoor  boolean NOT NULL DEFAULT false       -- drives weather filtering
);

CREATE TABLE tag_mapping (
    source        text NOT NULL,                     -- osm | foursquare | wikidata
    source_key    text NOT NULL,                     -- "tourism=viewpoint", "amenity=restaurant"
    tag           text NOT NULL REFERENCES tag_vocabulary(tag),
    PRIMARY KEY (source, source_key, tag)
);

-- ─────────────────────────── content ───────────────────────────

CREATE TABLE travel_listing (
    id                   uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
    district_id          uuid NOT NULL REFERENCES district(id),
    category_id          uuid NOT NULL REFERENCES category(id),
    name                 text NOT NULL,
    description          text,
    location             geography(Point,4326) NOT NULL,
    latitude             double precision GENERATED ALWAYS AS (ST_Y(location::geometry)) STORED,
    longitude            double precision GENERATED ALWAYS AS (ST_X(location::geometry)) STORED,
    tags                 text[] NOT NULL DEFAULT '{}',
    price_level          smallint CHECK (price_level BETWEEN 1 AND 4),
    price_per_night      numeric(12,2),              -- hotels, real price when known
    currency             char(3) NOT NULL DEFAULT 'LKR',
    rating               numeric(2,1) CHECK (rating BETWEEN 1.0 AND 5.0),
    rating_count         integer NOT NULL DEFAULT 0,
    opening_hours        jsonb,                      -- OSM opening_hours, parsed
    photo_url            text,
    has_public_transit   boolean NOT NULL DEFAULT false,
    nearest_transit_stop text,
    source               text NOT NULL,              -- osm | foursquare | booking | admin
    external_ref         text NOT NULL,
    is_verified          boolean NOT NULL DEFAULT false,
    is_active            boolean NOT NULL DEFAULT true,
    last_seen_at         timestamptz NOT NULL DEFAULT now(),
    created_at           timestamptz NOT NULL DEFAULT now(),
    updated_at           timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source, external_ref)
);
CREATE INDEX listing_location_gix ON travel_listing USING gist (location);
CREATE INDEX listing_tags_gin     ON travel_listing USING gin (tags);
CREATE INDEX listing_lookup       ON travel_listing (district_id, category_id, is_verified, is_active);
CREATE INDEX listing_name_trgm    ON travel_listing USING gin (name gin_trgm_ops);

CREATE TABLE listing_image (
    id          uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
    listing_id  uuid NOT NULL REFERENCES travel_listing(id) ON DELETE CASCADE,
    url         text NOT NULL,
    caption     text,
    attribution text                                 -- Wikimedia/OSM licence line
);
CREATE INDEX listing_image_listing ON listing_image (listing_id);

CREATE TABLE local_event (
    id             uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
    district_id    uuid NOT NULL REFERENCES district(id),
    name           text NOT NULL,
    description    text,
    start_datetime timestamptz NOT NULL,
    end_datetime   timestamptz,
    venue_name     text,
    location       geography(Point,4326),
    latitude       double precision GENERATED ALWAYS AS (ST_Y(location::geometry)) STORED,
    longitude      double precision GENERATED ALWAYS AS (ST_X(location::geometry)) STORED,
    tags           text[] NOT NULL DEFAULT '{}',
    price_min      numeric(12,2),
    price_max      numeric(12,2),
    currency       char(3) NOT NULL DEFAULT 'LKR',
    source         text NOT NULL,                    -- ticketmaster | admin
    external_ref   text NOT NULL,
    is_verified    boolean NOT NULL DEFAULT false,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source, external_ref)
);
CREATE INDEX event_window   ON local_event (district_id, start_datetime, end_datetime);
CREATE INDEX event_location ON local_event USING gist (location);

-- ─────────────────────────── cost model ───────────────────────────

CREATE TABLE cost_reference (
    id           uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
    district_id  uuid REFERENCES district(id),       -- NULL = national fallback
    category     text NOT NULL,                      -- hotel|restaurant|attraction|transport
    price_level  smallint NOT NULL CHECK (price_level BETWEEN 1 AND 4),
    unit         text NOT NULL,                      -- per_night|per_meal|per_entry|per_km
    typical_cost numeric(12,2) NOT NULL,
    currency     char(3) NOT NULL DEFAULT 'LKR',
    source_note  text,
    updated_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (district_id, category, price_level, unit)
);

-- ─────────────────────────── geo caches ───────────────────────────

-- Replaces app/data/sri_lanka_districts.py's hardcoded lookup entirely.
CREATE TABLE geo_resolution (
    query_norm    text PRIMARY KEY,                  -- lower(trim(input))
    display_name  text NOT NULL,
    location      geography(Point,4326) NOT NULL,
    district_id   uuid REFERENCES district(id),
    confidence    text NOT NULL,                     -- high|medium|low
    provider      text NOT NULL,                     -- nominatim|google|district_table
    resolved_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE travel_time (
    origin_key   text NOT NULL,                      -- "6.9271,79.8612" rounded to 4dp
    dest_key     text NOT NULL,
    mode         text NOT NULL DEFAULT 'drive',
    minutes      double precision NOT NULL,
    km           double precision NOT NULL,
    provider     text NOT NULL,                      -- ors|haversine
    fetched_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (origin_key, dest_key, mode)
);

-- ─────────────────────────── AI runtime ───────────────────────────

CREATE TABLE ai_session (
    session_id   uuid PRIMARY KEY,
    user_id      uuid,                               -- FK added in 0002
    state        jsonb NOT NULL,                     -- the 13 carry-over fields only
    react_trace  jsonb,                              -- last turn's traces, debug/report material
    turn_count   integer NOT NULL DEFAULT 1,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    expires_at   timestamptz NOT NULL DEFAULT now() + interval '7 days'
);
CREATE INDEX ai_session_expiry ON ai_session (expires_at);
CREATE INDEX ai_session_user   ON ai_session (user_id);

CREATE TABLE google_oauth_tokens (
    user_id       uuid PRIMARY KEY,
    access_token  text NOT NULL,
    refresh_token text,
    token_expiry  timestamptz,
    scope         text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);

-- ─────────────────────────── pipeline audit ───────────────────────────

CREATE TABLE data_source (
    name           text PRIMARY KEY,                 -- osm_listings, booking_prices, …
    display_name   text NOT NULL,
    cadence        text NOT NULL,                    -- daily|weekly|monthly|quarterly|manual
    is_enabled     boolean NOT NULL DEFAULT true,
    requires_key   boolean NOT NULL DEFAULT false,
    last_success_at timestamptz
);

CREATE TABLE data_source_run (
    id            uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
    source        text NOT NULL REFERENCES data_source(name),
    district_id   uuid REFERENCES district(id),
    started_at    timestamptz NOT NULL DEFAULT now(),
    finished_at   timestamptz,
    status        text NOT NULL DEFAULT 'running',   -- running|success|partial|failed
    rows_fetched  integer NOT NULL DEFAULT 0,
    rows_upserted integer NOT NULL DEFAULT 0,
    error         text
);
CREATE INDEX dsr_recent ON data_source_run (source, started_at DESC);
```

### `0002_identity_planning.sql` — NestJS's domain, created here so the AI backend can read profiles

`app_user`, `traveler_profile`, `admin_profile`, `refresh_token`, `chat_session`, `chat_message`,
`itinerary`, `itinerary_day`, `itinerary_item`, `expense`, `activity_log` — as specified in
`backend/docs/BACKEND_PLAN.md` §4.1, plus the deferred FKs from `ai_session.user_id` and
`google_oauth_tokens.user_id` to `app_user(id)`.

The AI backend reads exactly one of these: `traveler_profile` (interests, style, default budget,
home location). It writes none.

> `user` is a reserved word in SQL. Use `app_user` and map it in Prisma with `@@map("app_user")` —
> cheaper than quoting `"user"` in every hand-written query.

### Ownership

| Table | Writer | AI backend |
|---|---|---|
| `district`, `category`, `tag_vocabulary`, `tag_mapping` | AI ingest (seed) + admin | reads |
| `travel_listing`, `local_event`, `listing_image` | **both** — AI ingest writes `is_verified=false`; NestJS admin flips to `true` | reads verified only |
| `cost_reference` | admin (seeded by AI) | reads |
| `geo_resolution`, `travel_time`, `ai_session`, `google_oauth_tokens`, `data_source*` | **AI backend only** | owns |
| `app_user`, `traveler_profile`, chat/itinerary/expense | NestJS only | reads `traveler_profile` |

---

## 3. Migration runner

`backend/db/migrate.py` — ~60 lines, no framework:

```
1. connect (psycopg2, DATABASE_URL)
2. ensure schema_migration exists
3. for each backend/db/migrations/*.sql in filename order:
     - skip if filename already applied AND checksum matches
     - abort loudly if applied but checksum DIFFERS (someone edited a shipped migration)
     - else: execute in one transaction, record filename + sha256
```

Usage: `python backend/db/migrate.py` · `--dry-run` · `--status`.
Idempotent, safe to run on every deploy, and the checksum check catches the single most common
team-project failure: someone edits `0001` after it's been applied on another machine.

---

## 4. Districts and place resolution — replacing the hardcoded file

Concern **#1**. `app/data/sri_lanka_districts.py` gets deleted. Three mechanisms replace it.

### 4.1 Seeding districts from OSM (once, then quarterly)

`app/data/seed_districts.py` — Overpass query for Sri Lanka's admin_level=5 relations, with geometry:

```overpassql
[out:json][timeout:300];
area["ISO3166-1"="LK"][admin_level=2]->.lk;
relation["admin_level"="5"]["boundary"="administrative"](area.lk);
out geom;
```

For each relation: name, the `is_in:province` / parent lookup for province, `ST_Centroid` for
`center`, and the assembled multipolygon (simplified with `ST_SimplifyPreserveTopology(geom, 0.0005)`
≈ 50 m) for `boundary`. Upsert on `osm_relation_id`.

**This is not "hardcoding by another route."** The list is *derived from an authoritative external
source, stored as data, refreshable, and carries real geometry* — which the Python literal never
could. The `assert len(DISTRICTS) == 25` disappears with it: if Sri Lanka's administrative divisions
change, a re-run picks it up.

### 4.2 Point → district: local, exact, ~1 ms

```sql
SELECT id, name, province
FROM district
WHERE ST_Contains(boundary, ST_SetSRID(ST_Point($1, $2), 4326))
LIMIT 1;
```

Falls back to nearest-centroid within 30 km if a point lands in a boundary gap (coastal/islet cases):

```sql
SELECT id, name, province, ST_Distance(center, ST_MakePoint($1,$2)::geography) AS d
FROM district
ORDER BY center <-> ST_MakePoint($1,$2)::geography
LIMIT 1;
```

**This is the query that proves #1 is fixed** — "Ella" resolves to Badulla because of geometry, not
because someone typed a dictionary entry for Ella. The current code cannot do that at all: it has a
`_MOCK_TOWN_COORDS = {"ella": {...}}` hack precisely because Ella isn't a district.

### 4.3 Name → point: `app/tools/geo_tool.py`

```python
async def resolve_place(name: str) -> PlaceResolution | ToolError:
    """1. geo_resolution cache (permanent — place names don't move)
       2. exact/trigram match against district.name
       3. exact/trigram match against travel_listing.name in-country
       4. Nominatim (q=f"{name}, Sri Lanka", limit=1, 1 req/s, UA header)
       5. Google Geocoding — only if google_maps_api_key is set (optional, needs billing)
       Then: resolve_district(lat, lon) → district_id; write through to geo_resolution."""
```

Steps 2 and 3 mean common destinations never leave the database. Nominatim's usage policy (1 req/s,
attribution) is respected because the cache makes repeats free — and the cache is permanent, since
"Ella, Sri Lanka" will not move.

---

## 5. Ingestion

### 5.1 The connector framework

`app/data/connectors/base.py`:

```python
class Connector(Protocol):
    name: str                                    # matches data_source.name
    cadence: Literal["daily","weekly","monthly","quarterly","manual"]
    requires_key: bool
    scope: Literal["per_district", "global"]

    async def fetch(self, district: District | None) -> list[dict]: ...
    def normalize(self, raw: list[dict], district: District | None) -> list[CanonicalRow]: ...
    def upsert(self, rows: list[CanonicalRow]) -> int: ...
```

`app/data/pipeline.py` drives it:

```
for connector in enabled_connectors(cadence_due_today):
    for district in (districts if connector.scope == "per_district" else [None]):
        run = start_run(connector.name, district)         # data_source_run row
        try:
            raw    = await connector.fetch(district)
            rows   = connector.normalize(raw, district)
            count  = connector.upsert(rows)
            finish_run(run, "success", len(raw), count)
        except Exception as e:
            finish_run(run, "failed", error=str(e))       # one district failing never aborts the rest
        await polite_sleep(connector)
```

CLI: `python -m app.data.pipeline --source osm_listings --district Kandy --dry-run`.
That `--district` flag alone will save you hours: today, testing the ingest means waiting through
all 25 districts.

**Idempotency:** every upsert is `ON CONFLICT (source, external_ref) DO UPDATE`, and sets
`last_seen_at = now()`. Rows not seen in 3 consecutive successful runs get `is_active = false`
(soft delete) — never hard-deleted, because an admin may have verified them.

### 5.2 Source registry

| Connector | Source | Key? | Scope | Cadence | Writes |
|---|---|---|---|---|---|
| `seed_districts` | Overpass (admin_level=5 + geometry) | no | global | quarterly | `district` |
| `osm_listings` | Overpass (`tourism=hotel/attraction`, `amenity=restaurant`, `historic=*`, `natural=beach/waterfall`) | no | per_district | weekly | `travel_listing` |
| `osm_transit` | Overpass (`bus_station`, `railway=station`) | no | per_district | monthly | `travel_listing.has_public_transit` |
| `wikidata_enrich` | Wikidata SPARQL + Wikipedia REST summary | no | per_district | monthly | `description`, `listing_image` |
| `foursquare_enrich` | Foursquare Places API | yes | per_district | **lazy — see below** | `rating`, `rating_count`, `price_level`, `opening_hours` |
| `booking_prices` | Booking.com via RapidAPI | yes | per_district | weekly | `price_per_night`, `currency` |
| `ticketmaster_events` | Ticketmaster Discovery | yes | per_district | daily | `local_event` |
| `cost_seed` | CSV in repo | no | global | manual | `cost_reference` |

**Overpass must rotate mirrors.** Verified 2026-09-02: the primary instance returned `504 Gateway
Timeout` on a trivial query — routine for a free shared service, and fatal for a nightly job that
depends on it for *all* listing data. `osm_listings` and `seed_districts` try each mirror in turn
before failing a district:

```python
OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]
# rotate on 429/502/503/504 and on connection errors; keep the existing
# 15 s back-off and 10 s inter-district sleep on whichever mirror answers.
```

**Rotation alone isn't enough — retry the whole rotation.** Observed 2026-09-02: two checks minutes
apart went `all 3 mirrors unavailable` (504 / ReadTimeout / ConnectionError) then `OK` on immediate
retry. So a district must not be marked failed on one bad pass:

```
for attempt in 1..3:
    for mirror in OVERPASS_MIRRORS:  try it
    sleep(30 * attempt)          # 30 s, 60 s
mark district failed -> data_source_run.status = 'partial', continue to next district
```

A `partial` run is resumable: the next night's pass picks up the districts that missed, because
upserts are keyed on `(source, external_ref)` and `last_seen_at` tracks what's stale. Never abort
the whole pipeline for one district.

`scripts/check_apis.py` probes the same way, so the pre-flight reflects what ingestion will actually
experience.

**Foursquare is lazy, not bulk.** Its free allowance dropped to **500 Pro calls/month** (June 2026),
so bulk-enriching ~3,000 listings is impossible. Instead: when a district is planned for the first
time, enrich only its **top 20 candidates** by the non-rating factors, cache permanently, skip
anything enriched within 90 days, and hard-stop at `FOURSQUARE_MONTHLY_BUDGET` (default 400) with a
log line. ~25 calls per newly-visited district, spent on exactly the listings a user is about to be
shown. Free bulk substitute: `wikidata_enrich`'s Wikipedia **sitelink count as a popularity prior**,
unmetered and covering every notable attraction in the country.

**Sources deliberately not used:** Yelp Fusion (effectively no Sri Lanka coverage — drop the setting),
Eventbrite (public event search API discontinued in 2019; already confirmed dead in this repo's own
notes), Amadeus/Duffel (booking is out of scope, and both want a card).

### 5.3 Normalization — OSM tags → canonical tags

The `tag_mapping` table replaces the ad-hoc `if tags.get("tourism") == "hotel"` chain in
`overpass_ingest.py`. Seeded from `app/data/tag_mapping.csv`:

```csv
source,source_key,tag
osm,tourism=hotel,stay
osm,tourism=attraction,sightseeing
osm,tourism=viewpoint,views
osm,natural=beach,beach
osm,natural=waterfall,nature
osm,historic=ruins,history
osm,historic=monument,history
osm,amenity=place_of_worship,culture
osm,amenity=restaurant,food
osm,cuisine=sri_lankan,local_food
osm,sport=hiking,hike
osm,leisure=park,nature
```

and `tag_vocabulary` carries `is_outdoor`, which is what makes "no outdoor items on a rainy day"
(validator rule `weather_respect`) a data-driven check rather than a keyword guess.

### 5.4 Scheduler

`app/scheduler.py`, keeping APScheduler:

| Job | Trigger | Notes |
|---|---|---|
| `pipeline_daily` | 02:30 Asia/Colombo | runs every connector whose cadence is due |
| `session_gc` | 04:00 daily | `DELETE FROM ai_session WHERE expires_at < now()` |
| `travel_time_gc` | 04:10 weekly | prune entries older than 90 days |

One job, not three ingest jobs, because the pipeline already knows each source's cadence from the
`data_source` table. Keep the existing `_run_safely` wrapper — a failing job must log, not kill the
scheduler.

Same caveat as today, worth restating: APScheduler is in-process, so jobs only fire while the server
runs. Acceptable at this scale; if it matters later, the same `pipeline.py` entry point runs fine
from cron or a container `command:`.

---

## 6. Cost reference and currency

Decision D14: **base currency LKR**, stored explicitly everywhere.

`app/data/cost_reference.csv` — seed values (indicative 2026 LKR; replace with your own research and
record the source in `source_note`):

```csv
district,category,price_level,unit,typical_cost,source_note
,hotel,1,per_night,4500,national guesthouse baseline
,hotel,2,per_night,12000,national mid-range
,hotel,3,per_night,28000,national upper-mid
,hotel,4,per_night,65000,national luxury
,restaurant,1,per_meal,600,local rice & curry
,restaurant,2,per_meal,1800,casual sit-down
,restaurant,3,per_meal,4500,upscale
,restaurant,4,per_meal,9000,fine dining
,attraction,1,per_entry,0,free/public
,attraction,2,per_entry,1500,local ticket
,attraction,3,per_entry,5000,foreign-visitor ticket
,attraction,4,per_entry,12000,premium/heritage site
,transport,2,per_km,60,tuk-tuk / rideshare
Colombo,hotel,2,per_night,16000,capital premium
Colombo,restaurant,2,per_meal,2500,capital premium
Nuwara Eliya,hotel,2,per_night,14000,hill-country season premium
```

Blank `district` = national fallback. District rows override. Every cost the API returns carries a
`basis` field (`exact` | `reference` | `national`) so the UI can be honest about precision.

Hotel prices from Booking arrive in USD → converted at ingest with `settings.usd_lkr_rate` and
stored as LKR with `source_note = 'booking@<rate>'` so the rate used is auditable.

---

## 7. Travel time

`app/tools/routing_tool.py`, backing the `travel_matrix` tool:

```
key = (round(lat,4), round(lon,4)) pairs
1. travel_time table (90-day TTL)
2. ORS  POST {ORS_BASE_URL}/v2/matrix/driving-car
        ORS_BASE_URL = https://api.heigit.org/openrouteservice
        (api.openrouteservice.org is deprecated + throttled since Aug 2026)
3. haversine_km * 1.35 / _avg_kmh(straight_km)   (always succeeds; provider="haversine")
→ write result back to travel_time
```

**Quota shapes the call pattern.** Matrix V2 is **500 requests/day, 40/min** on the free tier — a
different, much smaller budget than Directions V2's 2,000/day. So `travel_matrix` must issue **one
many-to-many request per district per planning session**, covering every candidate at once (ORS
matrix accepts many origins × many destinations in a single call). A trip plan then costs ~1–2 ORS
calls. A per-pair implementation would burn the daily quota within an hour — treat that as a
correctness requirement, not an optimization.

Rounding keys to 4 dp (~11 m) is what makes the cache actually hit: without it, GPS jitter produces
a unique key every request. A district's listing set converges to a warm matrix after a few plans.

**Calibrate the haversine fallback in Phase 4.** Measured 2026-09-02, Colombo→Kandy: ORS says
**121 km / 128 min**; the original `×1.35 / 32 km/h` fallback predicts **127.6 km / 239 min**. The
distance factor is fine, the speed constant is ~2× too pessimistic — see
[`API_SETUP.md §3.1.1`](API_SETUP.md) for the banded replacement and the calibration task.

---

## 8. Bringing it up, in order

```powershell
cd backend
docker compose up -d --wait                      # db + redis healthy
python db/migrate.py                             # 0000, 0001, 0002

cd ../ai-backend
python -m app.data.seed_districts                # 25 districts + boundaries from OSM
python -m app.data.pipeline --source cost_seed
python -m app.data.pipeline --source osm_listings --district Kandy   # smoke test, ~2 min
python -m app.data.pipeline                      # full run, expect 30–60 min
```

Verification queries, in order:

```sql
SELECT count(*) FROM district;                                        -- 25
SELECT name FROM district
 WHERE ST_Contains(boundary, ST_SetSRID(ST_Point(81.0467, 6.8658),4326));   -- Badulla  ← Ella
SELECT c.name, count(*) FROM travel_listing l
  JOIN category c ON c.id = l.category_id GROUP BY 1;                 -- non-zero for all 3
SELECT source, status, sum(rows_upserted) FROM data_source_run
 WHERE started_at > now() - interval '1 day' GROUP BY 1,2;            -- all 'success'
```

---

## 9. Mock-data removal checklist

Concern **#3**. Do this **only after §8 passes** — removing mocks before the database has data
leaves the system with nothing to plan from. Every site, so nothing is missed:

| # | File | What goes |
|---|---|---|
| 1 | `app/tools/db_tool.py` | `_MOCK_HOTELS`, `_MOCK_RESTAURANTS`, `_MOCK_ATTRACTIONS`, `_MOCK_EVENTS` (~90 lines) |
| 2 | `app/tools/db_tool.py` | `_MOCK_TOWN_COORDS`, `_get_mock_data()` |
| 3 | `app/tools/db_tool.py` | the `except → "using mock data"` branch in `_get_listings` |
| 4 | `app/tools/db_tool.py` | the same branch in `get_events` |
| 5 | `app/tools/db_tool.py` | `get_user_profile`'s silent empty-dict default → explicit `ProfileUnavailable` |
| 6 | `app/tools/db_tool.py` | `get_transit_info()` (returns a constant), `check_pickme_coverage()` (returns `True`) |
| 7 | `app/workflows/recommendation_agent.py` | hardcoded event window `"2026-08-20"`–`"2026-08-23"`; the comment "Fetch raw mock candidates" |
| 8 | `app/data/sri_lanka_districts.py` | whole file (→ `district` table) |
| 9 | `app/utils/db_pool.py` | docstring's "callers fall back to their mock data" contract — pool failure now propagates as `DataUnavailable` |
| 10 | `app/utils/session_store.py` | `trip_sessions.json` + the file itself in the repo root |
| 11 | `app/tools/calendar_tool.py` | `calendar_tokens.json` local-file fallback (DB only) |
| 12 | `tests/` | every fixture asserting mock behaviour → seeded test DB fixtures (`test_db_tool.py`, `test_recommendation_agent.py`, parts of `test_orchestrator.py`) |

**Replacement contract for `db_tool`:**

```python
class DataUnavailable(Exception):
    """The database could not answer. Never swallowed, never substituted."""

async def search_listings(...) -> SearchResult:
    """Returns SearchResult(items=[], total=0) when the query legitimately matches nothing.
       Raises DataUnavailable when the database itself is unreachable.
       These are different situations and must never again produce the same output."""
```

Conflating "no results" with "database is down" is the root of the current design's worst property:
a plan built from mock data is indistinguishable, to the user, from a real one.

**Verification:** `grep -ril "mock" app/` returns nothing, and `grep -rn "fallback" app/tools/` returns
only the two intentional ones (haversine routing, template narration).
