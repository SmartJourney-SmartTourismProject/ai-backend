# tests/test_ticketmaster_events.py
# No real network - requests.get is mocked directly (this connector uses
# `requests`, not httpx). No real database - upsert_rows is exercised
# against a captured SQL string rather than a live connection, matching
# the existing test_events_ingest.py style if present, else the pattern
# used across this suite for postgres_writer-backed connectors.

from unittest.mock import MagicMock, patch

from app.data.connectors.ticketmaster_events import TicketmasterEventsConnector


class _FakeDistrict:
    id = "district-1"
    name = "Kandy District"
    lat, lon = 7.29, 80.64


def _response(status_code=200, json_data=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    resp.raise_for_status.return_value = None
    return resp


async def test_fetch_returns_empty_without_key(monkeypatch):
    monkeypatch.setattr("app.data.connectors.ticketmaster_events.settings.ticketmaster_api_key", "")
    connector = TicketmasterEventsConnector()
    result = await connector.fetch(_FakeDistrict())
    assert result == []


async def test_fetch_returns_raw_events(monkeypatch):
    monkeypatch.setattr("app.data.connectors.ticketmaster_events.settings.ticketmaster_api_key", "fake-key")
    connector = TicketmasterEventsConnector()

    with patch("app.data.connectors.ticketmaster_events.requests.get") as mock_get:
        mock_get.return_value = _response(json_data={
            "_embedded": {"events": [{"id": "ev-1", "name": "Test Event"}]},
        })
        result = await connector.fetch(_FakeDistrict())

    assert result == [{"id": "ev-1", "name": "Test Event"}]


async def test_fetch_no_events_key_returns_empty_list_not_error(monkeypatch):
    monkeypatch.setattr("app.data.connectors.ticketmaster_events.settings.ticketmaster_api_key", "fake-key")
    connector = TicketmasterEventsConnector()

    with patch("app.data.connectors.ticketmaster_events.requests.get") as mock_get:
        mock_get.return_value = _response(json_data={})   # no _embedded key at all - the documented Sri Lanka gap
        result = await connector.fetch(_FakeDistrict())

    assert result == []


def test_normalize_builds_expected_row_shape():
    connector = TicketmasterEventsConnector()
    raw = [{
        "id": "ev-1", "name": "Kandy Festival",
        "info": "A local celebration.",
        "dates": {"start": {"dateTime": "2026-10-01T18:00:00Z"}},
        "_embedded": {"venues": [{"name": "Town Hall", "location": {"latitude": "7.29", "longitude": "80.64"}}]},
        "priceRanges": [{"min": 500, "max": 2000}],
    }]

    rows = connector.normalize(raw, _FakeDistrict())

    assert len(rows) == 1
    r = rows[0]
    assert r["name"] == "Kandy Festival"
    assert r["venue_name"] == "Town Hall"
    assert r["lat"] == 7.29 and r["lon"] == 80.64
    assert r["price_min"] == 500 and r["price_max"] == 2000
    assert r["source"] == "ticketmaster" and r["external_ref"] == "ev-1"


def test_normalize_drops_events_missing_required_fields():
    connector = TicketmasterEventsConnector()
    raw = [
        {"id": "ev-1", "name": None, "dates": {"start": {"dateTime": "2026-10-01T18:00:00Z"}}},   # no name
        {"id": None, "name": "Has no id", "dates": {"start": {"dateTime": "2026-10-01T18:00:00Z"}}},  # no id
        {"id": "ev-3", "name": "Has no date", "dates": {}},   # no start datetime
    ]
    rows = connector.normalize(raw, _FakeDistrict())
    assert rows == []


def test_normalize_falls_back_to_district_coords_when_venue_has_none():
    connector = TicketmasterEventsConnector()
    raw = [{
        "id": "ev-1", "name": "Event", "dates": {"start": {"dateTime": "2026-10-01T18:00:00Z"}},
        "_embedded": {"venues": [{"name": "Somewhere", "location": {}}]},
    }]
    rows = connector.normalize(raw, _FakeDistrict())
    assert rows[0]["lat"] == 7.29 and rows[0]["lon"] == 80.64


def test_upsert_uses_compound_conflict_key():
    connector = TicketmasterEventsConnector()
    rows = [{
        "district_id": "d1", "name": "Event", "description": None,
        "start_datetime": "2026-10-01T18:00:00Z", "end_datetime": None,
        "venue_name": "Hall", "lat": 7.29, "lon": 80.64,
        "price_min": None, "price_max": None, "source": "ticketmaster", "external_ref": "ev-1",
    }]
    with patch("app.data.connectors.ticketmaster_events.upsert_rows", return_value=1) as mock_upsert:
        count = connector.upsert(rows)

    assert count == 1
    _, kwargs = mock_upsert.call_args
    assert kwargs["on_conflict"] == ("source", "external_ref")
    assert "location" in kwargs["geo_columns"]
