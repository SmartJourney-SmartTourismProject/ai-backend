# tests/test_web_events_friday.py
# No network: parsing runs against trimmed copies of real friday.lk pages
# (tests/fixtures/scrape/, captured 2026-09-30), and the crawl/geocode/write
# edges are faked.

from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.data.connectors import web_events_friday as wf
from app.data.connectors.web_events_friday import FridayEventsConnector

FIXTURES = Path(__file__).parent / "fixtures" / "scrape"
EVENT_URL = "https://www.friday.lk/events/5f04845d-7663-43a1-a872-2b9522b1ad09"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _raw(**overrides):
    soon = datetime.now(timezone.utc) + timedelta(days=5)
    base = {
        "uuid": "5f04845d-7663-43a1-a872-2b9522b1ad09", "url": EVENT_URL,
        "name": "WILD AIR 1.0", "description": "A night of electronic music.",
        "start": soon.isoformat(), "end": (soon + timedelta(hours=3)).isoformat(),
        "status": "https://schema.org/EventScheduled",
        "venue": "Park Street Warehouse", "locality": "Colombo", "country": "LK",
        "price": "3500.00", "currency": "LKR", "keywords": "music, Colombo",
        "place": {"district_id": "d-colombo", "lat": 6.93, "lon": 79.85},
    }
    return {**base, **overrides}


# ---- parsing real pages -----------------------------------------------------

def test_city_slugs_from_real_index_page():
    slugs = wf.parse_city_slugs(_fixture("friday_index.html"))
    assert "colombo" in slugs and "kandy" in slugs and "nuwara-eliya" in slugs
    assert "submit" not in slugs
    assert len(slugs) == len(set(slugs))


def test_event_urls_from_real_city_page_item_list():
    urls = wf.parse_event_urls(_fixture("friday_city_colombo.html"))
    assert EVENT_URL in urls
    assert all(wf._EVENT_LINK.match(u) for u in urls)


def test_event_from_real_detail_page():
    event = wf.parse_event(_fixture("friday_event_detail.html"), EVENT_URL)
    assert event["name"] == "WILD AIR 1.0"
    assert event["start"] == "2026-10-02T12:30:00.000Z"
    assert event["locality"] == "Colombo" and event["country"] == "LK"
    assert event["venue"].startswith("Park Street Warehouse")
    assert event["uuid"] == "5f04845d-7663-43a1-a872-2b9522b1ad09"


def test_page_without_event_json_ld_yields_nothing():
    assert wf.parse_event("<html><body>no data</body></html>", EVENT_URL) is None


# ---- normalize --------------------------------------------------------------

def test_real_detail_page_price_quirk_is_stored_as_unknown():
    # The captured page really says "60008000.00" (a 6,000-8,000 range run
    # together) - it must never reach a budget as 60 million rupees.
    event = wf.parse_event(_fixture("friday_event_detail.html"), EVENT_URL)
    assert wf._price_lkr(event) is None


def test_normalize_keeps_a_good_event():
    [row] = FridayEventsConnector().normalize([_raw()], None)
    assert row["district_id"] == "d-colombo"
    assert row["price"] == 3500.0
    assert row["external_ref"] == "5f04845d-7663-43a1-a872-2b9522b1ad09"
    assert row["source_url"] == EVENT_URL
    assert "music" in row["tags"] and "nightlife" in row["tags"]


def test_normalize_drops_past_far_future_cancelled_foreign_and_unplaced():
    past = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    far = (datetime.now(timezone.utc) + timedelta(days=400)).isoformat()
    raws = [
        _raw(start=past, end=past),
        _raw(start=far, end=None),
        _raw(status="https://schema.org/EventCancelled"),
        _raw(country="IN"),
        _raw(place=None),
        _raw(place={"district_id": None, "lat": 0, "lon": 0}),
        _raw(name=""),
        _raw(start="not a date"),
    ]
    assert FridayEventsConnector().normalize(raws, None) == []


def test_open_ended_event_is_kept():
    [row] = FridayEventsConnector().normalize([_raw(end=None)], None)
    assert row["end_datetime"] is None


def test_long_description_is_truncated_on_a_word():
    [row] = FridayEventsConnector().normalize([_raw(description="word " * 200)], None)
    assert len(row["description"]) <= wf.DESCRIPTION_MAX + 1
    assert row["description"].endswith("…")


def test_usd_price_is_converted(monkeypatch):
    monkeypatch.setattr("app.data.scraping.settings.usd_lkr_rate", 300.0)
    assert wf._price_lkr({"price": "20", "currency": "USD"}) == 6000.0


# ---- fetch / upsert ---------------------------------------------------------

async def test_fetch_crawls_index_cities_events_and_resolves_each_town_once(monkeypatch):
    pages = {
        "https://www.friday.lk/events": _fixture("friday_index.html"),
        "https://www.friday.lk/events/colombo": _fixture("friday_city_colombo.html"),
        EVENT_URL: _fixture("friday_event_detail.html"),
    }
    monkeypatch.setattr(wf, "polite_get", lambda url: pages.get(url))
    lookups = []

    async def fake_resolve(name):
        lookups.append(name)
        return {"district_id": "d-colombo", "lat": 6.93, "lon": 79.85}
    monkeypatch.setattr(wf, "resolve_place", fake_resolve)

    raw = await FridayEventsConnector().fetch(None)

    assert any(r["uuid"] == "5f04845d-7663-43a1-a872-2b9522b1ad09" for r in raw)
    assert lookups.count("Colombo") == 1
    assert all(r["place"]["district_id"] == "d-colombo" for r in raw)


async def test_fetch_returns_nothing_when_index_is_unreachable(monkeypatch):
    monkeypatch.setattr(wf, "polite_get", lambda url: None)
    assert await FridayEventsConnector().fetch(None) == []


def test_upsert_lands_pending_and_preserves_moderation(monkeypatch):
    captured = {}

    def fake_upsert(table, rows, **kwargs):
        captured.update(table=table, rows=rows, **kwargs)
        return len(rows)
    monkeypatch.setattr(wf, "upsert_rows", fake_upsert)

    connector = FridayEventsConnector()
    assert connector.upsert(connector.normalize([_raw()], None)) == 1

    row = captured["rows"][0]
    assert row["is_verified"] is False and row["source"] == "friday_lk"
    assert row["source_url"] == EVENT_URL
    assert captured["insert_only"] == {"is_verified"}
    assert captured["on_conflict"] == ("source", "external_ref")
