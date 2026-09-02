# API Setup — Phase 0a

Do this **before writing any code**. Every row below ends with a green line in
`python scripts/check_apis.py`. Nothing here requires a credit card.

Limits verified 2026-09-02. Providers change these; re-run the checker if something starts failing.

---

## 1. At a glance

| # | Service | Need | Card? | Free limit | Env var | Breaks if missing |
|---|---|---|---|---|---|---|
| 1 | **Google AI Studio (Gemini)** | 🔴 required | no | ~15 RPM / ~1,500 RPD | `GEMINI_API_KEY` | Everything. No LLM at all. | 
| 2 | **PostgreSQL + PostGIS** (local Docker) | 🔴 required | no | — | `DATABASE_URL` | Everything. 503 by design. |
| 3 | **Redis** (local Docker) | 🔴 required | no | — | `REDIS_URL` | Weather/disaster caching off → external APIs hammered |
| 4 | **OpenWeather** | 🔴 required | **no**¹ | 1,000 calls/day | `OPENWEATHER_API_KEY` | No weather → no rain-aware planning |
| 5 | **Overpass (OSM)** | 🔴 required | no | keyless, be polite | — | No listings at all — this is your main data source |
| 6 | **Nominatim (OSM)** | 🔴 required | no | keyless, 1 req/s | — | No place→coordinate resolution |
| 7 | **EONET · USGS · GDACS** | 🔴 required | no | keyless | — | No disaster awareness |
| 8 | **ip-api.com** | 🟡 auto | no | keyless, 45/min | — | IP fallback for start location |
| 9 | **Google Calendar OAuth** | 🟡 planned | no | — | `GOOGLE_CALENDAR_CLIENT_ID` / `_SECRET` | No free-day detection |
| 10 | **OpenRouteService** | 🟢 recommended | no | 2,500/day · 40,000/month | `ORS_API_KEY` | Distances fall back to haversine ×1.35 |
| 11 | **Groq** | 🟢 recommended | no | 30 RPM · 1,000 RPD · 8K TPM | `GROQ_API_KEY` | No LLM failover when Gemini hits quota |
| 12 | **Foursquare Places** | 🟢 optional | no² | **500 Pro calls/month** | `FOURSQUARE_API_KEY` | Ratings sparse → `rating_score` ≈ neutral prior |
| 13 | **Booking.com (RapidAPI)** | 🟢 optional | no² | varies by plan | `BOOKING_RAPIDAPI_KEY` | Hotel prices come from `cost_reference` instead of real prices — ✅ configured & verified live 2026-09-02 |
| 14 | **Ticketmaster** | ⚪ low value | no | 5,000/day | `TICKETMASTER_API_KEY` | Events — but coverage for Sri Lanka is ~zero anyway |
| — | ~~Yelp Fusion~~ | ❌ dropped | | | | No Sri Lanka coverage |
| — | ~~Eventbrite~~ | ❌ dropped | | | | Public event search API discontinued 2019 |
| — | ~~Google Maps~~ | ❌ not needed | | | | Requires billing; Nominatim + PostGIS cover our needs |

¹ Stay on the **free plan** endpoints `/data/2.5/weather` and `/data/2.5/forecast` — which is what `weather_tool.py` already calls. **One Call API 3.0 requires a card; do not switch to it.**
² No card to *start*; a card is only needed if you exceed the free allowance.

**Status — verified 2026-09-02 by `scripts/check_apis.py`: ALL 14 services green, Phase 0a fully
complete.** All 7 required + all 7 optional/low-value. Gemini confirms `gemini-3.5-flash-lite`
callable (38 models available); PostGIS 3.4 on PostgreSQL 16.4; Redis 7.4.11; OpenWeather live;
Overpass, Nominatim, all three disaster feeds reachable; Calendar OAuth round-tripped live with a
real refresh token (a genuine PKCE bug was found and fixed along the way — see
`app/api/google_oauth.py`'s comments); ORS returns real road times; Groq and Foursquare respond;
Booking.com and Ticketmaster both valid.

Three things were fixed to get there — worth knowing, because each would have cost an afternoon later:

1. **`DATABASE_URL` was empty** in `ai-backend/.env` while set in `backend/.env`. That single blank
   line is why every lookup silently served mock data. Copied across.
2. **`?schema=public` had to be stripped** from the DSN — see §2.2.
3. **Overpass returned `504`** on the first run. It's a free shared service; mirror rotation is now
   specified in [`DATA_PLATFORM.md §5.2`](DATA_PLATFORM.md) and implemented in the checker.

Redis was also added to `backend/docker-compose.yml` (it wasn't there).

---

## 2. Required — do these four

### 2.1 Gemini — already set ✅
Verify it can reach `gemini-3.5-flash-lite`: `python scripts/check_apis.py --llm`.
Then open [aistudio.google.com/rate-limit](https://aistudio.google.com/rate-limit) and record your real RPM/RPD in
[`PROJECT_MASTER_PLAN.md`](PROJECT_MASTER_PLAN.md) §3 D6 — Google no longer publishes them.

### 2.2 Database ✅
Both services must point at the same instance (BACKEND_PLAN §2).

```powershell
cd backend
docker compose up -d --wait      # db + redis, waits for both healthchecks
```

> **Gotcha — the two services need different DSN forms.** `backend/.env` uses Prisma's format,
> which ends in `?schema=public`. **psycopg2 and asyncpg reject that parameter** with
> `invalid dsn: invalid URI query parameter: "schema"`, so `ai-backend/.env` must carry the same
> URL *without* it. Same host, port, database, credentials — different query string. Copying the
> line verbatim between the two files will break the Python side, and the error names the DSN
> rather than the file, so it reads like a connection-string typo.

### 2.3 Redis
Added to `backend/docker-compose.yml` in Phase 1. Until then:
`docker run -d --name smartjourney_redis -p 6379:6379 redis:7-alpine`
Set `REDIS_URL=redis://localhost:6379/0`. `app/utils/cache.py` already expects it and currently
no-ops silently without it — meaning weather is re-fetched on every single request.

### 2.4 OpenWeather — already set ✅
Free plan, 1,000 calls/day. With the 45-minute cache in `weather_tool.py`, one district costs ~32
calls/day. Comfortable.

---

## 3. Recommended — two signups, ~5 minutes total

### 3.1 OpenRouteService — real road travel times ✅ configured 2026-09-02

The dashboard changed: **you no longer create a named token.** An account is auto-provisioned with a
**Basic Key**, shown at the top of [account.heigit.org/manage/key](https://account.heigit.org/manage/key) — copy it with the icon beside the masked value.

**Two things that will bite anyone following an older guide:**

1. **The host moved.** `api.openrouteservice.org` is deprecated and has been throttled since
   August 2026. Use **`https://api.heigit.org/openrouteservice`** — so
   `…/v2/matrix/driving-car`, not `api.openrouteservice.org/v2/matrix/driving-car`.
   Configured as `ORS_BASE_URL` so it's one env change if it moves again.
2. **Matrix has its own, much smaller quota.** The 2,000/day figure is *Directions V2*.
   **Matrix V2 is 500/day at 40/min** — and Matrix is the endpoint we actually use.

500/day is ample **because of how we call it**: one many-to-many matrix request per district per
planning session covers every candidate at once, so a trip plan costs ~1–2 calls, not one per pair.
Combined with the `travel_time` cache (4 dp keys, 90-day TTL) a district goes cold→warm after a
couple of plans. Do **not** write per-pair lookups; that pattern would exhaust the quota in an hour.

> **Verified:** Colombo→Kandy returns **128 min / 121 km** by road.

### 3.1.1 Calibration finding — the haversine fallback is badly tuned

That live number exposes a real flaw in the fallback in
[`DETERMINISM_AND_VALIDATION.md §6.2`](DETERMINISM_AND_VALIDATION.md), which assumed
`haversine_km × 1.35 / 32 km/h`:

| | Distance | Time |
|---|---|---|
| Straight line (haversine) | 94.5 km | — |
| Fallback estimate | 127.6 km (×1.35) | **239 min** (@32 km/h) |
| **ORS actual** | **121 km** | **128 min** (≈57 km/h) |

The road-distance factor of **1.35 is good** (127.6 vs 121). The **speed constant is nearly 2× too
pessimistic** for inter-city routes — which would systematically push `prox()` toward 0 and make the
scorer over-prefer whatever is nearest, exactly the bias the factor exists to avoid.

**Action for Phase 4:** replace the single constant with a distance-banded one, calibrated against
~20 real ORS pairs across flat and hill-country routes:

```python
# provisional - calibrate in Phase 4 against real ORS samples
def _avg_kmh(straight_km: float) -> float:
    if straight_km < 5:    return 18.0    # urban, congested
    if straight_km < 30:   return 35.0    # suburban / secondary roads
    return 50.0                           # inter-city A-roads
```

Worth doing even with ORS working, since the fallback is what runs when the daily quota is spent.

### 3.1.2 Google Calendar OAuth — console configuration

The credentials in `.env` are valid, but the consent flow needs console setup that is **not** done
yet. Everything below is free; the Calendar API needs **no billing** — ignore the $300 trial banner.

**The client belongs to project number `329520155601`.** If your console shows a different project
(e.g. "Medicare") with an empty Credentials page, you're looking at the wrong one — **do not create
new credentials there**, or you'll end up with a second client that doesn't match `.env`.
Switch first: project dropdown → **ALL** tab → paste `329520155601` into the search.

**Steps, in order:**

> **The console UI moved.** The single "OAuth consent screen" page is gone; it's now split across
> the **Google Auth Platform** section. Older guides (including Google's own) still say
> "OAuth consent screen", so translate:
>
> | Old name | Now under *Google Auth Platform* |
> |---|---|
> | OAuth consent screen → app info | **Branding** |
> | User type + publishing status + test users | **Audience** |
> | Scopes | **Data Access** |
> | Credentials → OAuth 2.0 Client IDs | **Clients** |

1. **Enable the API.** APIs & Services → **Library** → "Google Calendar API" → **Enable**.
   Without this, the token exchange still succeeds and then every `freebusy` call returns 403 —
   which looks like a credentials problem and isn't. **Do this first**: the scope in step 3 won't
   appear in the picker until its API is enabled.
2. **Audience** → confirm User type **External** and Publishing status **Testing**.
   Do *not* click "Publish app" — see §3.1.3 for why Production isn't worth it yet.
   Fill app name / support email / developer contact under **Branding** if prompted.
3. **Data Access** → **Add or remove scopes** → filter for `freebusy` → tick
   `https://www.googleapis.com/auth/calendar.freebusy` → **Update** → **Save**.
   Nothing else — that's the minimum for free/busy reads, and broader calendar scopes pull you into
   heavier verification for no benefit.
4. **Audience → Test users** → **+ Add users** → your own Gmail and every teammate who will demo
   it. In Testing mode only listed users can consent (up to 100). An unlisted account gets
   `access_denied` at the consent screen with no further explanation.
5. **Clients → `smart-tourism-backen` → Authorized redirect URIs** → **+ Add URI**:
   `http://localhost:8000/auth/google/callback`
   Exact match, character for character: `http` not `https` (localhost is exempt), port `8000`,
   no trailing slash. A mismatch gives `redirect_uri_mismatch` and nothing else.
6. **Do not reuse this client for NestJS sign-in.** That's a separate client with its own redirect
   (`http://localhost:3000/api/v1/auth/google/callback`) and different scopes — see
   `backend/docs/BACKEND_PLAN.md` §2. Sharing one client means revoking calendar access also breaks
   login.

**Confirmed for this project (2026-09-02):** project `smart-tourism-assistant`
(number `329520155601`), client `smart-tourism-backen`, type *Web application* — matches the
`GOOGLE_CALENDAR_CLIENT_ID` in `.env`.

**Verify end to end** (not just that the keys parse):

```powershell
python -m uvicorn main:app --reload
# then open in a browser:
#   http://localhost:8000/auth/google/login?user_id=11111111-1111-1111-1111-111111111111
```

Consent, then confirm you land on `{"status": "connected", …}`. Until the `google_oauth_tokens`
table exists (Phase 1), the token is written to `calendar_tokens.json` in the repo root — that file
appearing is your proof the round trip worked.

### 3.1.3 ⚠️ Refresh tokens die after 7 days in Testing — click "Publish app" to fix it

An **External** app whose publishing status is **Testing** has its refresh tokens **revoked by
Google after 7 days**. The exemption is narrow — it covers apps requesting *only* `openid` /
`email` / `profile` — so `calendar.freebusy` doesn't qualify, and the grant lapses weekly.

**But `calendar.freebusy` is classified NON-SENSITIVE** (the console confirms this: it appears under
*"Your non-sensitive scopes"*). That matters a lot, because **apps using only non-sensitive scopes
can be published to Production without going through app verification.** No security review, no
weeks of waiting — one button.

**So: publish to Production.** *Google Auth Platform → Audience → **Publish app***.

| | Testing | **Production** |
|---|---|---|
| Refresh tokens | **revoked after 7 days** | don't expire on a timer |
| Who can consent | only listed test users (max 100) | any Google account |
| Verification needed | none | **none** — non-sensitive scopes only |
| "Unverified app" warning | n/a | not shown for non-sensitive scopes |

The only thing verification would buy you is showing a custom **app name and logo** on the consent
screen — that's *brand verification*, lightweight and purely cosmetic. Skip it for now.

Two caveats, neither blocking:
- Production means anyone with a Google account can consent. Fine here — and better for demos than
  maintaining a test-user list.
- Google's policy prefers separate projects for dev and production. Overkill at this stage; note it
  if this ever ships commercially.

**Still worth building the detection** (small, Phase 6): a user can revoke access at any time, and
`calendar_tool.get_free_days()` currently catches every exception and returns `[]` — so a revoked
grant is reported as "no calendar connected", indistinguishable from a user who never connected one.
Distinguish `invalid_grant`, flag `calendar_reconnect_required`, and prompt to reconnect. With
Production publishing this drops from a weekly certainty to an occasional edge case, but it's still
the difference between a one-click fix and a silent dead feature.

### 3.2 Groq — LLM failover (decision D6b)
1. [console.groq.com](https://console.groq.com) — GitHub/Google sign-in, no card.
2. API Keys → create → `GROQ_API_KEY=…`
3. `pip install langchain-groq`

Free: 30 RPM, **1,000 RPD**, 8K TPM, 200K TPD on `openai/gpt-oss-120b`. The 8K tokens/minute ceiling
makes it a poor primary — one recommendation call is 4–6K tokens — but a perfectly good catch for
"Gemini quota exhausted mid-demo".

---

## 4. Optional — and one design change you should know about

### 4.1 Foursquare — rating/price are Premium-only, even on the free tier ⚠️ corrected 2026-09-02

**Verified live against a real key and a real subscribed org:** `rating`, `price`, and `stats` all
return `429` — *"Your account has no API credits remaining... Purchasing credits is required if
you are trying to make Premium calls"* — while `tel`/`website`/basic fields return `200` on the
identical request. This isn't a quota issue; it's a **hard tier gate**. The free "Pro" allowance
never included ratings at all, contradicting the plan's original framing below.

**Given the project's "completely free" constraint, `foursquare_enrich` does not request
rating/price/stats.** An unattended nightly connector must never be one config change away from a
paid call. What it still legitimately adds for free: confirming a listing's category/address
against a second source, and `tel`/`website` contact fields for display. Real value, just smaller
than originally planned.

**The actual free rating source turned out to be Booking.com, already flowing through
`booking_prices.py` at no extra cost** — its hotel search response carries `reviewScore` (0–10),
`reviewCount`, and a real photo URL in the *same* payload already being fetched for pricing. No
second call, no new key. `booking_prices.py` now converts and writes `rating`/`rating_count`/
`listing_image` alongside price. Verified live: 13 Kandy hotels landed with ratings from 4.4–5.0 and
review counts from 4–147.

That covers **hotels**. Restaurants and attractions still have no free rating source — for those,
`wikidata_enrich`'s `langlinkscount` (§ below) is the real, primary popularity substitute, not a
secondary nice-to-have as originally framed.

*Original plan text, kept for context on what changed:*

Foursquare cut the free allowance to **500 Pro calls/month** from June 2026. Bulk-enriching ~3,000
listings across 25 districts would need six years of allowance, so the weekly-bulk plan in
[`DATA_PLATFORM.md §5.2`](DATA_PLATFORM.md) **is changed to lazy, on-demand enrichment**:

> When a district is planned for the first time, enrich only its **top 20 candidates** (ranked by
> the non-rating factors), cache the result **permanently**, and skip anything enriched in the last
> 90 days. Budget-guarded: the connector stops at 400 calls/month and logs it.

That is ~25 calls per newly-visited district. Realistically you'll cover every district you actually
demo, well inside 500/month — and the calls land on exactly the listings a real user is about to be
shown, which is where a rating is worth having.

Sign up at [docs.foursquare.com](https://docs.foursquare.com) if you want it. **If you skip it:**
`rate()` returns the 0.45 unknown-prior for most listings, so ranking runs on preference, proximity
and cost. That's a 20% weight going neutral — degraded but not broken.

**Free bulk substitute, worth doing either way:** the `wikidata_enrich` connector already planned
gives descriptions and images for free and unmetered, plus **Wikipedia sitelink count as a
popularity prior** — a decent deterministic stand-in for "is this place notable" that costs nothing
and covers every major attraction in the country.

*(Unverified alternative, if you want to evaluate it: OpenTripMap exposes an importance/popularity
`rate` field on a free key. I haven't confirmed its 2026 status or Sri Lanka coverage — treat as a
lead, not a recommendation.)*

### 4.2 Booking.com via RapidAPI — real hotel prices ✅ configured & verified live 2026-09-02

**Correction to an earlier note in this file:** it previously said `booking-com15.p.rapidapi.com`
"no longer exists on RapidAPI" based on it not surfacing in a marketplace search. That was wrong —
`scripts/check_apis.py` now confirms it live: `booking-com15.p.rapidapi.com: 4 Sri Lanka match(es)
for 'Kandy'`. The original host and endpoints (`/api/v1/hotels/searchDestination`,
`/api/v1/hotels/searchHotels`) work exactly as `overpass_ingest.py` already expects — **no
connector changes needed.** If it ever does 404/401 for you, the description-lineage forks below are
still a reasonable fallback path, kept for that case:

| Candidate | Stats | Note |
|---|---|---|
| **Booking COM** by *DataCrawler* | 9.9 · 100% · 3.4 s | Verbatim booking-com15 description. |
| Booking COM by *Things4u* | 9.9 · 100% · 2.5 s | Same description lineage. |
| Booking com by *Tipsters CO* | 9.9 · 100% · 0.74 s | Fastest, freshest, but a different description — endpoint shape may differ. |
| ~~Booking COM – cheaper version by *Api-city*~~ | 2.1 · 16% | Avoid — low success rate. |

If switching: subscribe on the **Pricing** tab (free plan), confirm the endpoint paths on
**Endpoints**, then set `BOOKING_RAPIDAPI_HOST` to that provider's host.
`python scripts/check_apis.py` reports precisely which failure you'd hit: `401/403` not subscribed ·
`404` wrong endpoint path · `429` quota spent.

### 4.3 Ticketmaster — low value, ~2 minutes
[developer.ticketmaster.com](https://developer.ticketmaster.com), free, 5,000/day. This repo's own
notes already confirm it returns **zero events for Sri Lanka** (checked Colombo/Kandy/Galle at
100 km). Add it so the connector isn't dead code, but plan on admin-entered events being the real
source. See the risk row in [`PROJECT_MASTER_PLAN.md`](PROJECT_MASTER_PLAN.md) §7.

---

## 5. Keyless services — nothing to configure, but respect the rules

| Service | Obligation |
|---|---|
| **Nominatim** | Max **1 req/s**, real `User-Agent` identifying the app (already set), attribution in the UI. The permanent `geo_resolution` cache is what keeps us compliant — a repeat destination never hits it. |
| **Overpass** | Public instance is shared and free. Keep the existing 10 s inter-district sleep and 429 back-off. Nightly, not per-request. |
| **GDACS / EONET / USGS** | No key. Cache 1 h (already implemented). |
| **ip-api.com** | 45 req/min, **HTTP only** on the free tier. Already noted in `location_tool.py`. |

---

## 6. Verify

```powershell
cd ai-backend
python scripts/check_apis.py            # everything
python scripts/check_apis.py --llm      # Gemini/Groq only, lists callable models
python scripts/check_apis.py --quick    # key presence only, no network
```

Exit code is non-zero if any **required** service fails, so it works as a CI/pre-flight gate.

**Phase 0a is done when every 🔴 row is green.** 🟢 rows can stay red — the system degrades exactly
as the "Breaks if missing" column says, and each one is a documented, tested fallback path rather
than an error.
