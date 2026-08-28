# Next Steps for `temp-shaluka` (Member A)

This is the detailed, code-level follow-up to the merge just done on `thisuri-merge-temp`. It has
two parts:

- **Part 1** — five concrete fixes, found by actually running the merged orchestrator end-to-end.
  Each one includes the exact code to change, on `temp-shaluka` (not just the merged branch), so
  the same bug doesn't come back on the next merge.
- **Part 2** — what to build next, once Part 1 is done, to finish the Orchestrator track per
  BUILD_PLAN.md.

All line numbers refer to the files as they exist on `thisuri-merge-temp` right now; adjust for
wherever they currently sit on `temp-shaluka`.

---

## Part 1 — Fixes

### 1. `_respond_node` turns a working itinerary into an error message

**File:** `app/core/orchestrator.py`
**Found by:** calling `POST /trip-plan` for a real trip and getting back a fully-built 2-day
itinerary with a cost estimate — but `final_response` said *"Sorry, I ran into an issue:
location_unresolved: need to ask user for start location"* and the API response's top-level
`errors` list wasn't empty, even though the plan itself was fine.

**Why it happens:** `_respond_node` currently does:

```python
async def _respond_node(state: TripState) -> TripState:
    if state.errors:
        state.final_response = "Sorry, I ran into an issue: " + "; ".join(state.errors)
    else:
        state.final_response = (
            f"Here's your trip plan for {state.destination or 'your destination'}: "
            f"{len(state.itinerary)} day(s) planned, "
            f"estimated cost {state.estimated_cost}."
        )
    state.completed_steps.append("respond")
    return state
```

Any single entry in `state.errors` — even an advisory one like "couldn't resolve your location, so
travel-time estimates are rough" — makes the whole response look like a failure, even when
`state.itinerary` has real content.

**Fix:** split `state.errors` into things that actually block a plan and things that are just
notes, and only treat the first kind as a hard failure:

```python
# Prefixes that mark an error as advisory (degrade gracefully, don't hide
# a real result behind them) rather than a reason the whole plan failed.
# Add to this list as more soft-failure paths are identified (see BUILD_PLAN.md §8).
_SOFT_ERROR_PREFIXES = ("location_unresolved",)


async def _respond_node(state: TripState) -> TripState:
    hard_errors = [e for e in state.errors if not e.startswith(_SOFT_ERROR_PREFIXES)]
    soft_notes = [e for e in state.errors if e.startswith(_SOFT_ERROR_PREFIXES)]

    if hard_errors and not state.itinerary:
        state.final_response = "Sorry, I ran into an issue: " + "; ".join(hard_errors)
    else:
        state.final_response = (
            f"Here's your trip plan for {state.destination or 'your destination'}: "
            f"{len(state.itinerary)} day(s) planned, "
            f"estimated cost {state.estimated_cost}."
        )
        if soft_notes:
            state.final_response += "\n\nNote: " + "; ".join(soft_notes)

    state.completed_steps.append("respond")
    return state
```

This is the same "advisory vs. hard failure" split your own MEMBER_A_REPORT.md already called out
as missing (§3.11, §6.4) — this is the minimal version of it. The real version (asking the user a
follow-up question instead of just noting it) is in Part 2.

---

### 2. `_context_node` skips weather/disaster on the most common path, and uses the wrong coordinates

**File:** `app/core/orchestrator.py`
**Already flagged in your own report** (§6, Priority 0, items 1–2) — still present after the
merge, confirmed by testing: a request with no `user_id` (so no calendar, so `trip_dates` stays
`None`) never fetches weather, because of the `and state.trip_dates` condition below. Also, when it
*does* run, it uses `state.start_location` (where the traveler currently is), not the destination.

Current code:

```python
async def _context_node(state: TripState) -> TripState:
    # Weather + disaster would run concurrently here via asyncio.gather.
    # disaster_tool.py is deprioritized for now — swap in gather() once
    # it exists:
    #   weather, disaster = await asyncio.gather(
    #       get_weather(lat, lon, dates), get_disaster_info(lat, lon)
    #   )
    if state.start_location and state.trip_dates:
        lat = state.start_location["lat"]
        lon = state.start_location["lon"]
        dates = [d["start_date"] for d in state.trip_dates]
        state.weather = await get_weather(lat, lon, dates)
    state.completed_steps.append("context")
    return state
```

**Fix** — key off `state.destination` instead (falls back to today+N days when there's no
calendar), resolve its coordinates via Member B's district lookup (already in the merged repo at
`app/data/sri_lanka_districts.py` — 25 districts with centroid lat/lon, exactly the same list her
`db_tool.py` and data pipeline use, so weather/disaster/recommendations all agree on where "Kandy"
is), and fetch weather + disaster together now that `disaster_tool.py` exists (Part 2, item A,
below):

```python
import asyncio
from datetime import datetime, timedelta

from app.data.sri_lanka_districts import get_district
from app.tools.disaster_tool import get_disaster_info

async def _context_node(state: TripState) -> TripState:
    if not state.destination:
        state.completed_steps.append("context")
        return state

    district = get_district(state.destination)
    if not district:
        # Destination isn't one of the 25 districts we have coordinates
        # for - degrade gracefully rather than guessing, same as every
        # other tool's failure mode in BUILD_PLAN.md §8.
        state.completed_steps.append("context")
        return state

    lat, lon = district["lat"], district["lon"]

    if state.trip_dates:
        dates = [d["start_date"] for d in state.trip_dates]
    else:
        # No calendar connected - default to today + duration_days (or 1
        # day) so weather/disaster still get checked for *some* dates
        # instead of being skipped entirely on the most common path.
        span = state.duration_days or 1
        today = datetime.utcnow().date()
        dates = [(today + timedelta(days=i)).isoformat() for i in range(span)]

    state.weather, state.disaster = await asyncio.gather(
        get_weather(lat, lon, dates),
        get_disaster_info(lat, lon),
    )
    state.completed_steps.append("context")
    return state
```

Note: `app/data/sri_lanka_districts.py` is Member B's file, but it's a plain lookup table with no
dependency on her agents or the database — safe to import directly from the Orchestrator track,
and it means "Kandy" resolves to the exact same coordinates everywhere in the app.

---

### 3. Stray docstring in `app/config/settings.py` — move it to where it belongs

**File:** `app/config/settings.py` (on `temp-shaluka`)
**What's there:** a docstring block at the bottom of `settings.py` that isn't about settings at
all — it explains `location_tool.py`'s design choices (the `ipapi.co` vs `ip-api.com` tradeoff).
It's already flagged as stale in your own report (§3.2, issue #2). During this merge it caused a
real conflict with Member B's version of the file and had to be dropped, since it didn't belong
there in the first place.

**Fix:** delete that block from `settings.py`, and if it isn't already there, put an equivalent
note in `app/tools/location_tool.py`'s own docstring (it already has very similar content — see
the comment block above the `client_ip` handling — so this may just mean deleting the duplicate
from `settings.py` and not adding anything new).

---

### 4. `slot_filling.py`'s extracted interests don't match `db_tool.py`'s tags

**File:** `app/utils/slot_filling.py`
**Found by:** a real end-to-end test — `"...interested in beaches"` extracted `interests:
["beaches"]`, but `db_tool.py`'s mock listings tag things with singular `"beach"`. Exact-string
matching in `db_tool.py` then returned **zero** hotels/restaurants/attractions for a destination
that had matching data, and `RecommendationAgent` failed with "No verified listings found." This
looks like a Recommendation Agent bug but is really a tag-format mismatch coming from this file's
output.

**Fix (on your side — the cheaper one):** tell the LLM to normalize to singular, lowercase tags,
since `db_tool.py`'s side is Member B's contract (§4) and the fewer places that need to agree on a
tag vocabulary, the better:

```python
interests: List[str] = Field(
    default_factory=list,
    description=(
        "List of travel interests/activity types mentioned, as short, "
        "singular, lowercase tags (e.g. 'beach' not 'beaches', 'hike' not "
        "'hiking trips'). Empty list if none mentioned."
    ),
)
```

Also add one sentence to `_SYSTEM_PROMPT` reinforcing it:

```
When listing interests, use short singular lowercase tags (e.g. "beach", "hike", "culture") -
not plurals or full phrases.
```

This won't catch every mismatch (synonyms like "swimming" vs. "beach" still won't match), but it
fixes the exact case found, and it's worth mentioning to Member B too — a fuzzy/substring match on
her side would be a more complete fix, but that's her file to change, not yours.

**Also noticed while in this file:** `fill_slots()` hardcodes `model="gemini-3.6-flash"` instead of
reading `settings.llm_model`, same issue Thisuri's `modification_thisuri.md` flagged and fixed for
`recommendation_agent.py`/`planning_agent.py`. Small, but worth matching:

```python
llm = ChatGoogleGenerativeAI(
    model=settings.llm_model,
    google_api_key=settings.gemini_api_key,
    temperature=0,
)
```

---

### 5. `_location_node` always passes `client_gps=None, client_ip=None`

**File:** `app/core/orchestrator.py`
**Not a bug**, but worth a comment so nobody "fixes" it into an actual bug later:

```python
async def _location_node(state: TripState) -> TripState:
    # If the API layer already resolved this (it has direct access to the
    # real HTTP request's IP/GPS, which TripState doesn't carry as fields),
    # don't re-resolve.
    if state.start_location:
        state.completed_steps.append("location")
        return state

    # Deliberately None/None here: this node is a fallback for callers that
    # invoke the orchestrator directly (tests, scripts) without going
    # through app/api/trip.py, which already resolves start_location from
    # the real request before building TripState. In the normal HTTP path,
    # this branch is never reached because state.start_location is already
    # set - see the early return above.
    location = await resolve_start_location(client_gps=None, client_ip=None)
    ...
```

Without this, `resolve_start_location(None, None)` always returns `None` when called this way,
which is exactly what happened in every direct test run during the merge — expected, not a bug,
but confusing without the comment.

---

## Part 2 — What to build next

Once Part 1's fixes are in, here's what's left to finish the Orchestrator track, in priority order
(matches your own MEMBER_A_REPORT.md roadmap, updated for what the merge changed):

### A. `app/tools/disaster_tool.py` — still the one missing file

Per BUILD_PLAN.md §4/§1, and now easier to wire in since `_context_node` (fix #2 above) already
calls `get_disaster_info(lat, lon)` via `asyncio.gather` alongside weather. Suggested
implementation — same three free, keyless, global sources your report already scoped, written as
a plain async function matching your other tools' style (`httpx`, not `aiohttp`):

```python
# app/tools/disaster_tool.py
import asyncio
import math
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import httpx

EONET_URL = "https://eonet.gsfc.nasa.gov/api/v3/events"
USGS_URL = "https://earthquake.usgs.gov/fdsnws/event/1/query"
GDACS_URL = "https://www.gdacs.org/xml/rss.xml"


def _distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(a))


async def _fetch_eonet(lat: float, lon: float, radius_km: int) -> list[dict]:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(EONET_URL, params={"status": "open", "days": 20})
            resp.raise_for_status()
            data = resp.json()
    except Exception:
        return []

    events = []
    for event in data.get("events", []):
        geometries = event.get("geometries") or []
        if not geometries:
            continue
        coords = geometries[0].get("coordinates")
        if not coords or len(coords) < 2:
            continue
        e_lon, e_lat = coords[0], coords[1]
        distance = _distance_km(lat, lon, e_lat, e_lon)
        if distance > radius_km:
            continue
        category = (event.get("categories") or [{}])[0].get("id", "").lower()
        severity = (
            "red" if category in {"volcanoes", "wildfires", "severe_storms"}
            else "orange" if category in {"floods", "tropical_cyclones", "droughts"}
            else "green"
        )
        events.append({
            "type": category or "eonet",
            "severity": severity,
            "title": event.get("title"),
            "source": "EONET",
            "distance_km": round(distance, 1),
        })
    return events


async def _fetch_usgs(lat: float, lon: float, radius_km: int) -> list[dict]:
    try:
        start_time = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(USGS_URL, params={
                "format": "geojson", "latitude": lat, "longitude": lon,
                "maxradiuskm": radius_km, "minmagnitude": 4, "starttime": start_time,
            })
            resp.raise_for_status()
            data = resp.json()
    except Exception:
        return []

    events = []
    for feature in data.get("features", []):
        props = feature.get("properties", {})
        mag = props.get("mag") or 0
        coords = feature.get("geometry", {}).get("coordinates", [None, None])
        distance = _distance_km(lat, lon, coords[1], coords[0]) if coords[0] is not None else None
        severity = "red" if mag >= 7 else "orange" if mag >= 6 else "green"
        events.append({
            "type": "earthquake",
            "severity": severity,
            "title": f"Earthquake M{mag} - {props.get('place', 'Unknown location')}",
            "source": "USGS",
            "distance_km": round(distance, 1) if distance is not None else None,
        })
    return events


async def _fetch_gdacs(lat: float, lon: float, radius_km: int) -> list[dict]:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(GDACS_URL)
            resp.raise_for_status()
            xml_content = resp.text
    except Exception:
        return []

    events = []
    try:
        root = ET.fromstring(xml_content)
        ns = {"georss": "http://www.georss.org/georss"}
        for item in root.findall(".//item"):
            point_elem = item.find("georss:point", ns)
            if point_elem is None or not point_elem.text:
                continue
            try:
                g_lat, g_lon = map(float, point_elem.text.split())
            except ValueError:
                continue
            distance = _distance_km(lat, lon, g_lat, g_lon)
            if distance > radius_km:
                continue
            title = item.findtext("title", "") or ""
            severity = (
                "red" if "red" in title.lower()
                else "orange" if "orange" in title.lower()
                else "green"
            )
            events.append({
                "type": "gdacs_alert",
                "severity": severity,
                "title": title,
                "source": "GDACS",
                "distance_km": round(distance, 1),
            })
    except ET.ParseError:
        return []
    return events


async def get_disaster_info(lat: float, lon: float, radius_km: int = 300) -> dict:
    """
    Checks EONET (wildfires/storms/volcanoes), USGS (earthquakes), and GDACS
    (floods/cyclones/tsunamis) concurrently, filters each to `radius_km` of
    (lat, lon), and merges into one severity-ranked list.

    Per BUILD_PLAN.md §8: if one source fails, the other two still merge in.
    If all three fail, returns {"safe": True, "active_events": [],
    "note": "disaster data unavailable"} - never blocks the trip plan.
    """
    results = await asyncio.gather(
        _fetch_eonet(lat, lon, radius_km),
        _fetch_usgs(lat, lon, radius_km),
        _fetch_gdacs(lat, lon, radius_km),
        return_exceptions=True,
    )

    active_events: list[dict] = []
    all_failed = True
    for r in results:
        if isinstance(r, list):
            active_events.extend(r)
            all_failed = False

    if all_failed:
        return {"safe": True, "active_events": [], "note": "disaster data unavailable"}

    severity_rank = {"red": 0, "orange": 1, "green": 2}
    active_events.sort(key=lambda e: severity_rank.get(e["severity"], 3))

    return {"safe": len(active_events) == 0, "active_events": active_events}
```

A reference implementation of the same three sources (as a class method rather than standalone
functions) also exists in git history from before this cleanup, if useful to cross-check against:
`git show ce5eb4f:app/workflows/context_agent.py`.

### B. The clarification/ask-back branch (the real fix behind Part 1, item 1)

Right now, anything that should ask the user a follow-up question (no destination, no start
location after GPS+IP both fail) either silently defaults or shows up as an "error" in
`final_response`. BUILD_PLAN.md §2 and §12's third scripted test case (`"Plan a trip"` with no
destination → the Orchestrator should ask for one, not fail) need a real distinction between:

- a **hard failure** (something broke, nothing usable was produced),
- a **clarification needed** (the Orchestrator should ask the user something specific and stop), and
- an **advisory note** (the plan is fine, but something degraded gracefully — Part 1, item 1's
  `_SOFT_ERROR_PREFIXES` is a stopgap for this, not the full design).

Suggested approach: add a `clarification_needed: Optional[str] = None` field to `TripState` (needs
agreement with Member B since `state.py` is shared — Phase 1's "both, together" file), have
`_location_node` and the "no destination" path in slot-filling set it instead of appending to
`state.errors`, add a conditional edge that routes straight to `respond` when it's set, and have
`_respond_node` return the question in `final_response` as-is instead of wrapping it in "Sorry, I
ran into an issue."

### C. Slot-filling defaulting rules (BUILD_PLAN.md §2, still not implemented)

- **Destination-only request** → default to 1 day of activities + travel time, and pull
  `interests`/`travel_style`/`budget` from `db_tool.get_user_profile(state.user_id)` (already
  implemented and exposed by Member B, just never called from anywhere in the Orchestrator).
- **No destination at all** → this is exactly the clarification case in item B above.

### D. Google OAuth CSRF hole

`app/api/google_oauth.py`'s `state=user_id` in the consent URL is exactly the parameter CSRF
protection is supposed to use — right now it lets anyone bind their Google account to someone
else's `user_id` by hitting `/callback` with an arbitrary `state`. Needs a signed or opaque,
single-use state token mapped server-side before this goes anywhere public-facing.

### E. Persist calendar OAuth tokens

`get_stored_credentials()`/`save_credentials()` are backed by an in-memory dict that resets on
every restart. A `google_oauth_tokens` table (user_id, access_token, refresh_token, expiry, scope)
is the real fix — this was already asked of Member B in `member_B.md`; check whether her Supabase
schema has it yet now that both branches are merged.

### F. Caching for weather/disaster (BUILD_PLAN.md §8)

30–60 min TTL per `(lat, lon)` rounded to 2 decimals, for both `get_weather` and (once built)
`get_disaster_info`. Member B already built a working, fail-open Redis cache wrapper for exactly
this purpose — `app/utils/cache.py` (`cache_get`/`cache_set`), currently unused now that her old
`context_agent.py` (the only thing that called it) was removed in the cleanup. It's a drop-in fit:

```python
from app.utils.cache import cache_get, cache_set

WEATHER_CACHE_TTL_SECONDS = 45 * 60

async def get_weather(lat: float, lon: float, dates: list[str]) -> dict | None:
    cache_key = f"weather:{lat:.2f}:{lon:.2f}"
    cached = await cache_get(cache_key)
    if cached:
        return cached
    ...
    result = {"current": current, "forecast": forecast}
    await cache_set(cache_key, result, WEATHER_CACHE_TTL_SECONDS)
    return result
```

### G. Real tests

Convert the `test_*.py` print-scripts to actual `pytest` tests with assertions (per BUILD_PLAN.md
§12), and mock the HTTP layer (`respx` or `httpx.MockTransport`, since all your tools already use
`httpx`) so the suite doesn't need live API keys or network access to run in CI. Add one
failure-mode test per tool matching the §8 error matrix (e.g. OpenWeather times out →
`get_weather` returns `None`, the plan still completes without weather).
