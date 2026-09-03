# tests/test_foursquare_enrich.py
# No real network or database - requests.get and get_connection are both
# mocked directly (this connector uses `requests`, not httpx - see the note
# in test_wikidata_enrich.py about why respx would silently miss these calls).

from unittest.mock import MagicMock, patch

from app.data.connectors.foursquare_enrich import FoursquareEnrichConnector, PER_DISTRICT_LIMIT


def _cursor_for_fetch(monthly_calls=0, candidates=None):
    """A fake cursor whose fetchone/fetchall responses depend on which
    query ran - _monthly_call_count's SELECT SUM(...) vs the candidates SELECT."""
    cursor = MagicMock()

    def execute(sql, params=None):
        cursor._last_sql = sql

    def fetchone():
        return (monthly_calls,)

    def fetchall():
        return candidates or []

    cursor.execute.side_effect = execute
    cursor.fetchone.side_effect = fetchone
    cursor.fetchall.side_effect = fetchall
    return cursor


def _fake_conn(cursor):
    conn = MagicMock()
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    return conn


def _response(status_code=200, json_data=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    resp.raise_for_status.return_value = None if status_code < 400 else Exception("http error")
    return resp


class _FakeDistrict:
    id = "district-1"
    name = "Kandy District"
    province = "Central Province"
    lat, lon = 7.29, 80.64


async def test_fetch_respects_monthly_budget_exhausted(monkeypatch):
    monkeypatch.setattr("app.data.connectors.foursquare_enrich.settings.foursquare_api_key", "fake-key")
    monkeypatch.setattr("app.data.connectors.foursquare_enrich.settings.foursquare_monthly_budget", 10)
    cursor = _cursor_for_fetch(monthly_calls=10)   # budget already spent this month
    conn = _fake_conn(cursor)

    with patch("app.data.connectors.foursquare_enrich.get_connection", return_value=conn), \
         patch("app.data.connectors.foursquare_enrich.requests.get") as mock_get:
        connector = FoursquareEnrichConnector(limit=5)
        result = await connector.fetch(_FakeDistrict())

    assert result == []
    mock_get.assert_not_called()


async def test_fetch_limits_candidates_by_limit_argument(monkeypatch):
    """Regression test for a real bug found during development: --limit was
    accepted by the CLI but never threaded through to the connector, so
    every run silently used PER_DISTRICT_LIMIT regardless of what was asked."""
    monkeypatch.setattr("app.data.connectors.foursquare_enrich.settings.foursquare_api_key", "fake-key")
    monkeypatch.setattr("app.data.connectors.foursquare_enrich.settings.foursquare_monthly_budget", 500)
    candidates = [("id-1", "Place A", 7.29, 80.64), ("id-2", "Place B", 7.30, 80.65)]
    cursor = _cursor_for_fetch(monthly_calls=0, candidates=candidates)
    conn = _fake_conn(cursor)

    with patch("app.data.connectors.foursquare_enrich.get_connection", return_value=conn), \
         patch("app.data.connectors.foursquare_enrich.requests.get") as mock_get, \
         patch("app.data.connectors.foursquare_enrich.time.sleep"):
        mock_get.return_value = _response(json_data={"results": []})
        connector = FoursquareEnrichConnector(limit=2)
        result = await connector.fetch(_FakeDistrict())

    assert len(result) == 2
    assert mock_get.call_count == 2


async def test_fetch_only_requests_free_tier_fields(monkeypatch):
    """rating/price/stats are Premium-gated (verified live - see
    docs/master_plan/API_SETUP.md §4.1) - this connector must never request
    them, or an unattended nightly run risks hitting a paid endpoint."""
    monkeypatch.setattr("app.data.connectors.foursquare_enrich.settings.foursquare_api_key", "fake-key")
    monkeypatch.setattr("app.data.connectors.foursquare_enrich.settings.foursquare_monthly_budget", 500)
    cursor = _cursor_for_fetch(monthly_calls=0, candidates=[("id-1", "Place A", 7.29, 80.64)])
    conn = _fake_conn(cursor)

    with patch("app.data.connectors.foursquare_enrich.get_connection", return_value=conn), \
         patch("app.data.connectors.foursquare_enrich.requests.get") as mock_get, \
         patch("app.data.connectors.foursquare_enrich.time.sleep"):
        mock_get.return_value = _response(json_data={"results": []})
        connector = FoursquareEnrichConnector(limit=1)
        await connector.fetch(_FakeDistrict())

    requested_fields = mock_get.call_args.kwargs["params"]["fields"]
    for forbidden in ("rating", "price", "stats"):
        assert forbidden not in requested_fields.split(",")


async def test_fetch_stops_early_on_429_mid_run(monkeypatch):
    monkeypatch.setattr("app.data.connectors.foursquare_enrich.settings.foursquare_api_key", "fake-key")
    monkeypatch.setattr("app.data.connectors.foursquare_enrich.settings.foursquare_monthly_budget", 500)
    candidates = [("id-1", "A", 7.29, 80.64), ("id-2", "B", 7.30, 80.65), ("id-3", "C", 7.31, 80.66)]
    cursor = _cursor_for_fetch(monthly_calls=0, candidates=candidates)
    conn = _fake_conn(cursor)

    with patch("app.data.connectors.foursquare_enrich.get_connection", return_value=conn), \
         patch("app.data.connectors.foursquare_enrich.requests.get") as mock_get, \
         patch("app.data.connectors.foursquare_enrich.time.sleep"):
        mock_get.side_effect = [
            _response(json_data={"results": [{"fsq_place_id": "x"}]}),
            _response(status_code=429),
        ]
        connector = FoursquareEnrichConnector(limit=3)
        result = await connector.fetch(_FakeDistrict())

    assert len(result) == 1   # stopped after the 429, third candidate never attempted
    assert mock_get.call_count == 2


def test_upsert_marks_all_as_checked_counts_only_matches():
    connector = FoursquareEnrichConnector()
    cursor = MagicMock()
    conn = _fake_conn(cursor)

    with patch("app.data.connectors.foursquare_enrich.get_connection", return_value=conn):
        matched = connector.upsert([
            {"listing_id": "id-1", "match": {"fsq_place_id": "x"}},
            {"listing_id": "id-2", "match": None},
        ])

    assert matched == 1                       # only the real match counts
    assert cursor.execute.call_count == 2      # but BOTH get foursquare_checked_at set
