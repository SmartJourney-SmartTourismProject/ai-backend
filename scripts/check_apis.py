"""
Pre-flight check for every external service the AI backend depends on.

Run this before implementation work (Phase 0a) and any time something starts
failing for no obvious reason. It verifies two separate things per service:

  1. is a key configured at all, and
  2. does that key actually work against the live endpoint

Those fail differently and are usually confused with each other, which is how a
misconfigured key ends up looking like a code bug for an afternoon.

    python scripts/check_apis.py            # everything
    python scripts/check_apis.py --llm      # Gemini/Groq only, lists callable models
    python scripts/check_apis.py --quick    # key presence only, no network calls

Exit code is 1 if any REQUIRED service fails, so it works as a CI gate.
No secret value is ever printed - only key length and a masked prefix.
"""
from __future__ import annotations

import argparse
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# The default Windows console is cp1252 and raises UnicodeEncodeError on any
# non-latin-1 character (degree signs from OpenWeather, arrows in details).
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

try:
    from dotenv import load_dotenv
    load_dotenv(REPO_ROOT / ".env")
except ImportError:
    print("[!] python-dotenv not installed; reading os.environ only.")

try:
    import requests
except ImportError:
    sys.exit("[!] `requests` is required: pip install requests")

TIMEOUT = 10
UA = {"User-Agent": "SmartJourney-AI-Backend/2.0 (university project)"}

OK, FAIL, SKIP, WARN = "  OK  ", " FAIL ", " SKIP ", " WARN "


# --------------------------------------------------------------------------
# result plumbing
# --------------------------------------------------------------------------

@dataclass
class Check:
    name: str
    required: bool
    env_vars: list[str] = field(default_factory=list)
    probe: Optional[Callable[[], tuple[bool, str]]] = None
    breaks: str = ""
    setup_url: str = ""


@dataclass
class Result:
    check: Check
    status: str
    detail: str


def env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def mask(name: str) -> str:
    v = env(name)
    if not v:
        return "unset"
    return f"{v[:4]}…{v[-2:]} ({len(v)} chars)"


# --------------------------------------------------------------------------
# probes - each returns (ok, human-readable detail); never raises
# --------------------------------------------------------------------------

def probe_gemini() -> tuple[bool, str]:
    key = env("GEMINI_API_KEY")
    if not key:
        return False, "GEMINI_API_KEY unset"
    names: list[str] = []
    try:
        # Preferred: the current google-genai SDK. google.generativeai is
        # deprecated and prints a FutureWarning on import.
        from google import genai as new_genai
        client = new_genai.Client(api_key=key)
        names = [
            m.name.replace("models/", "")
            for m in client.models.list()
            if "generateContent" in (getattr(m, "supported_actions", None) or [])
        ]
    except Exception:
        try:
            import warnings
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                import google.generativeai as genai
            genai.configure(api_key=key)
            names = [
                m.name.replace("models/", "")
                for m in genai.list_models()
                if "generateContent" in getattr(m, "supported_generation_methods", [])
            ]
        except Exception as e:
            return False, f"list_models failed: {type(e).__name__}: {str(e)[:90]}"
    if not names:
        return False, "key reached the API but no generateContent models were listed"

    want = os.environ.get("LLM_MODEL", "gemini-3.5-flash-lite")
    if want in names:
        return True, f"{len(names)} models callable; '{want}' available"
    close = [n for n in names if "flash" in n][:4]
    return False, f"'{want}' NOT callable. flash models on this key: {close or names[:4]}"


def probe_groq() -> tuple[bool, str]:
    key = env("GROQ_API_KEY")
    if not key:
        return False, "GROQ_API_KEY unset (optional - LLM failover only)"
    try:
        r = requests.get(
            "https://api.groq.com/openai/v1/models",
            headers={"Authorization": f"Bearer {key}"}, timeout=TIMEOUT,
        )
        r.raise_for_status()
        ids = [m["id"] for m in r.json().get("data", [])]
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:90]}"
    want = "openai/gpt-oss-120b"
    return (want in ids), (f"'{want}' available ({len(ids)} models)" if want in ids
                           else f"'{want}' missing; have {ids[:4]}")


def probe_postgres() -> tuple[bool, str]:
    url = env("DATABASE_URL")
    if not url:
        return False, "DATABASE_URL is EMPTY - this is why everything falls back to mock data"
    try:
        import psycopg2
    except ImportError:
        return False, "psycopg2 not installed"
    try:
        with psycopg2.connect(url, connect_timeout=5) as conn, conn.cursor() as cur:
            cur.execute("SELECT version()")
            pg = cur.fetchone()[0].split(",")[0]
            try:
                cur.execute("SELECT PostGIS_Version()")
                gis = cur.fetchone()[0]
            except Exception:
                return False, f"{pg} connected, but PostGIS extension is MISSING"
            cur.execute("SELECT count(*) FROM information_schema.tables WHERE table_schema='public'")
            tables = cur.fetchone()[0]
        return True, f"{pg}, PostGIS {gis}, {tables} tables"
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:90]}"


def probe_redis() -> tuple[bool, str]:
    url = env("REDIS_URL")
    if not url:
        return False, "REDIS_URL unset - caching silently disabled"
    try:
        import redis
    except ImportError:
        return False, "redis not installed"
    try:
        c = redis.from_url(url, socket_connect_timeout=5)
        c.ping()
        return True, f"PONG ({c.info('server').get('redis_version', '?')})"
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:90]}"


def probe_openweather() -> tuple[bool, str]:
    key = env("OPENWEATHER_API_KEY")
    if not key:
        return False, "OPENWEATHER_API_KEY unset"
    try:
        r = requests.get(
            "https://api.openweathermap.org/data/2.5/weather",
            params={"lat": 6.9271, "lon": 79.8612, "appid": key, "units": "metric"},
            timeout=TIMEOUT,
        )
        if r.status_code == 401:
            return False, "401 - key invalid, or newly created (can take ~2h to activate)"
        r.raise_for_status()
        d = r.json()
        return True, f"Colombo {d['main']['temp']}°C, {d['weather'][0]['main']}"
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:90]}"


# The main Overpass instance is a free, shared, frequently-overloaded service -
# 429/504 are routine, not exceptional. Ingestion must rotate mirrors rather
# than fail a nightly run, so the probe checks the same way the connector will.
OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]


def probe_overpass() -> tuple[bool, str]:
    q = '[out:json][timeout:10];node(6.92,79.85,6.93,79.87)["amenity"="restaurant"];out 1;'
    problems = []
    for url in OVERPASS_MIRRORS:
        host = url.split("/")[2]
        try:
            r = requests.post(url, data=q, headers=UA, timeout=30)
            if r.status_code in (429, 502, 503, 504):
                problems.append(f"{host}:{r.status_code}")
                continue
            r.raise_for_status()
            n = len(r.json().get("elements", []))
            note = f" (primary busy: {', '.join(problems)})" if problems else ""
            return True, f"{host} OK, {n} element(s){note}"
        except Exception as e:
            problems.append(f"{host}:{type(e).__name__}")
    return False, f"all {len(OVERPASS_MIRRORS)} mirrors unavailable - {', '.join(problems)}"


def probe_nominatim() -> tuple[bool, str]:
    try:
        r = requests.get("https://nominatim.openstreetmap.org/search",
                         params={"q": "Ella, Sri Lanka", "format": "json", "limit": 1},
                         headers=UA, timeout=TIMEOUT)
        r.raise_for_status()
        d = r.json()
        if not d:
            return False, "no result for 'Ella, Sri Lanka'"
        return True, f"Ella → {float(d[0]['lat']):.4f}, {float(d[0]['lon']):.4f}"
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:90]}"


def probe_disaster() -> tuple[bool, str]:
    sources, up = {
        "EONET": ("https://eonet.gsfc.nasa.gov/api/v3/events", {"status": "open", "limit": 1}),
        "USGS": ("https://earthquake.usgs.gov/fdsnws/event/1/query", {"format": "geojson", "limit": 1}),
        "GDACS": ("https://www.gdacs.org/xml/rss.xml", {}),
    }, []
    for name, (url, params) in sources.items():
        try:
            r = requests.get(url, params=params, headers=UA, timeout=TIMEOUT)
            r.raise_for_status()
            up.append(name)
        except Exception:
            pass
    if len(up) == 3:
        return True, "EONET, USGS, GDACS all reachable"
    if up:
        return True, f"partial: {', '.join(up)} up (degrades gracefully)"
    return False, "all three sources unreachable"


def probe_ipapi() -> tuple[bool, str]:
    try:
        r = requests.get("http://ip-api.com/json/8.8.8.8", timeout=TIMEOUT)
        r.raise_for_status()
        d = r.json()
        return d.get("status") == "success", f"probe resolved to {d.get('country', '?')}"
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:90]}"


def probe_ors() -> tuple[bool, str]:
    key = env("ORS_API_KEY")
    if not key:
        return False, "ORS_API_KEY unset - distances fall back to haversine ×1.35"
    # api.openrouteservice.org is deprecated and has been throttled since
    # Aug 2026; the current host is api.heigit.org/openrouteservice.
    base = env("ORS_BASE_URL") or "https://api.heigit.org/openrouteservice"
    try:
        r = requests.post(
            f"{base}/v2/matrix/driving-car",
            json={"locations": [[79.8612, 6.9271], [80.6337, 7.2906]],
                  "metrics": ["duration", "distance"]},
            headers={"Authorization": key, "Content-Type": "application/json"},
            timeout=TIMEOUT,
        )
        if r.status_code in (401, 403):
            return False, f"{r.status_code} - key rejected"
        r.raise_for_status()
        d = r.json()
        mins = d["durations"][0][1] / 60
        km = d["distances"][0][1] / 1000
        return True, f"Colombo→Kandy {mins:.0f} min / {km:.0f} km by road"
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:90]}"


def probe_foursquare() -> tuple[bool, str]:
    key = env("FOURSQUARE_API_KEY")
    if not key:
        return False, "FOURSQUARE_API_KEY unset - ratings stay sparse (neutral prior)"
    attempts = [
        ("https://places-api.foursquare.com/places/search",
         {"Authorization": f"Bearer {key}", "X-Places-Api-Version": "2025-06-17", "accept": "application/json"}),
        ("https://api.foursquare.com/v3/places/search",
         {"Authorization": key, "accept": "application/json"}),
    ]
    last = ""
    for url, headers in attempts:
        try:
            r = requests.get(url, params={"ll": "7.2906,80.6337", "limit": 1},
                             headers=headers, timeout=TIMEOUT)
            if r.status_code in (401, 403):
                last = f"{r.status_code} at {url.split('/')[2]}"
                continue
            r.raise_for_status()
            n = len(r.json().get("results", []))
            return True, f"{url.split('/')[2]} OK, {n} result(s) near Kandy"
        except Exception as e:
            last = f"{type(e).__name__}: {str(e)[:60]}"
    return False, last or "both endpoints failed"


def probe_booking() -> tuple[bool, str]:
    key = env("BOOKING_RAPIDAPI_KEY")
    host = env("BOOKING_RAPIDAPI_HOST") or "booking-com15.p.rapidapi.com"
    if not key:
        return False, "BOOKING_RAPIDAPI_KEY unset - hotel costs use cost_reference, not real prices"
    try:
        r = requests.get(
            f"https://{host}/api/v1/hotels/searchDestination",
            params={"query": "Kandy"},
            headers={"X-RapidAPI-Key": key, "X-RapidAPI-Host": host}, timeout=TIMEOUT,
        )
        if r.status_code in (401, 403):
            return False, f"{r.status_code} - key rejected, or not subscribed to '{host}'"
        if r.status_code == 404:
            # The original booking-com15 provider is gone; its replacements
            # are forks with the same data but not always the same paths.
            return False, (f"404 - host '{host}' reachable but /api/v1/hotels/searchDestination "
                           f"doesn't exist there. Check the Endpoints tab for the equivalent "
                           f"destination-search path and tell me what it is.")
        if r.status_code == 429:
            return False, "429 - free plan quota exhausted for this period"
        r.raise_for_status()
        data = r.json().get("data", [])
        if not isinstance(data, list):
            return False, f"unexpected response shape: {str(r.json())[:80]}"
        lk = [c for c in data if c.get("country") == "Sri Lanka"]
        return bool(lk), f"{host}: {len(lk)} Sri Lanka match(es) for 'Kandy'"
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:90]}"


def probe_ticketmaster() -> tuple[bool, str]:
    key = env("TICKETMASTER_API_KEY")
    if not key:
        return False, "TICKETMASTER_API_KEY unset - low value, SL coverage is ~zero"
    try:
        r = requests.get("https://app.ticketmaster.com/discovery/v2/events.json",
                         params={"apikey": key, "countryCode": "LK", "size": 1}, timeout=TIMEOUT)
        if r.status_code == 401:
            return False, "401 - key invalid"
        r.raise_for_status()
        n = r.json().get("page", {}).get("totalElements", 0)
        return True, f"key valid; {n} event(s) listed for LK (expect 0 - known gap)"
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:90]}"


def probe_google_calendar() -> tuple[bool, str]:
    cid, secret = env("GOOGLE_CALENDAR_CLIENT_ID"), env("GOOGLE_CALENDAR_CLIENT_SECRET")
    uri = env("GOOGLE_CALENDAR_REDIRECT_URI")
    if not (cid and secret):
        return False, "client id/secret unset"
    if not cid.endswith(".apps.googleusercontent.com"):
        return False, "client id doesn't look like a Google OAuth client id"
    if not uri:
        return False, "GOOGLE_CALENDAR_REDIRECT_URI unset"
    return True, f"configured; redirect {uri} (must match the Google console exactly)"


# --------------------------------------------------------------------------
# registry
# --------------------------------------------------------------------------

CHECKS: list[Check] = [
    Check("Gemini (Google AI Studio)", True, ["GEMINI_API_KEY"], probe_gemini,
          "no LLM at all", "https://aistudio.google.com/apikey"),
    Check("PostgreSQL + PostGIS", True, ["DATABASE_URL"], probe_postgres,
          "no data; /trip-plan returns 503 by design", "backend/docker-compose.yml"),
    Check("Redis", True, ["REDIS_URL"], probe_redis,
          "weather/disaster caching off", "docker run -d -p 6379:6379 redis:7-alpine"),
    Check("OpenWeather", True, ["OPENWEATHER_API_KEY"], probe_openweather,
          "no rain-aware planning", "https://openweathermap.org/api"),
    Check("Overpass (OSM)", True, [], probe_overpass,
          "no listings - primary data source", "keyless"),
    Check("Nominatim (OSM)", True, [], probe_nominatim,
          "no place->coordinate resolution", "keyless"),
    Check("Disaster feeds", True, [], probe_disaster,
          "no disaster awareness", "keyless"),
    Check("ip-api.com", False, [], probe_ipapi,
          "IP fallback for start location", "keyless"),
    Check("Google Calendar OAuth", False,
          ["GOOGLE_CALENDAR_CLIENT_ID", "GOOGLE_CALENDAR_CLIENT_SECRET"], probe_google_calendar,
          "no free-day detection", "https://console.cloud.google.com/apis/credentials"),
    Check("OpenRouteService", False, ["ORS_API_KEY"], probe_ors,
          "haversine distances instead of road time", "https://openrouteservice.org/dev/#/signup"),
    Check("Groq (LLM failover)", False, ["GROQ_API_KEY"], probe_groq,
          "no failover when Gemini hits quota", "https://console.groq.com"),
    Check("Foursquare Places", False, ["FOURSQUARE_API_KEY"], probe_foursquare,
          "ratings sparse -> neutral prior", "https://docs.foursquare.com"),
    Check("Booking.com (RapidAPI)", False, ["BOOKING_RAPIDAPI_KEY"], probe_booking,
          "hotel costs estimated, not real", "https://rapidapi.com"),
    Check("Ticketmaster", False, ["TICKETMASTER_API_KEY"], probe_ticketmaster,
          "events (coverage ~zero anyway)", "https://developer.ticketmaster.com"),
]

LLM_ONLY = {"Gemini (Google AI Studio)", "Groq (LLM failover)"}


def run(check: Check, quick: bool) -> Result:
    missing = [v for v in check.env_vars if not env(v)]
    if quick:
        if missing:
            return Result(check, FAIL if check.required else SKIP, f"unset: {', '.join(missing)}")
        return Result(check, OK, ", ".join(f"{v}={mask(v)}" for v in check.env_vars) or "keyless")
    if check.probe is None:
        return Result(check, SKIP, "no probe")
    try:
        ok, detail = check.probe()
    except Exception as e:                                    # a probe must never take the run down
        ok, detail = False, f"probe crashed: {type(e).__name__}: {e}"
    if ok:
        return Result(check, OK, detail)
    return Result(check, FAIL if check.required else WARN, detail)


def main() -> int:
    p = argparse.ArgumentParser(description="Pre-flight check for external services.")
    p.add_argument("--quick", action="store_true", help="key presence only, no network")
    p.add_argument("--llm", action="store_true", help="Gemini/Groq only")
    args = p.parse_args()

    checks = [c for c in CHECKS if c.name in LLM_ONLY] if args.llm else CHECKS

    print(f"\nSmartJourney API pre-flight  ·  {REPO_ROOT}")
    print(f"mode: {'quick (no network)' if args.quick else 'live probes'}\n")

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda c: run(c, args.quick), checks))

    width = max(len(r.check.name) for r in results) + 2
    print(f"{'':6} {'SERVICE'.ljust(width)} {'REQ':4} DETAIL")
    print("-" * (width + 62))
    for r in results:
        # ASCII only: the default Windows console is cp1252 and raises on emoji.
        req = "REQ" if r.check.required else "opt"
        print(f"[{r.status}] {r.check.name.ljust(width)} {req:4} {r.detail}")

    failed_req = [r for r in results if r.check.required and r.status == FAIL]
    warned = [r for r in results if r.status == WARN]

    print()
    if failed_req:
        print(f"{len(failed_req)} REQUIRED service(s) failing - fix before implementation:\n")
        for r in failed_req:
            print(f"  · {r.check.name}: {r.detail}")
            print(f"    without it: {r.check.breaks}   setup: {r.check.setup_url}")
    if warned:
        print(f"\n{len(warned)} optional service(s) unconfigured (system degrades as documented):")
        for r in warned:
            print(f"  · {r.check.name} -> {r.check.breaks}")
    if not failed_req:
        print("All required services are green. Phase 0a complete.")

    print("\nSee docs/master_plan/API_SETUP.md for setup steps.\n")
    return 1 if failed_req else 0


if __name__ == "__main__":
    raise SystemExit(main())
