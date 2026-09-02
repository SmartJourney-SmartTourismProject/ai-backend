# tests/test_wikidata_enrich.py
# No real network or database. IMPORTANT: this connector uses the `requests`
# library (matching the sync-batch-script convention shared by
# overpass_ingest.py/seed_districts.py/osm_listings.py - these run outside
# the FastAPI event loop via psycopg2), NOT httpx - so requests.get is
# mocked directly via unittest.mock, not respx (which only intercepts
# httpx and would silently let these calls reach the real network - caught
# during development when a mocked-with-respx version of this file produced
# real Wikipedia content instead of the fixture data).

from unittest.mock import MagicMock, patch

from app.data.connectors.wikidata_enrich import WikidataEnrichConnector


def _response(status_code=200, json_data=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    if status_code >= 400:
        import requests
        resp.raise_for_status.side_effect = requests.exceptions.HTTPError(f"{status_code}")
    else:
        resp.raise_for_status.return_value = None
    return resp


def _geosearch_json(pageid=24362778, dist=11):
    return {"query": {"geosearch": [
        {"pageid": pageid, "title": "Temple of the Tooth", "lat": 7.2936, "lon": 80.6414, "dist": dist},
    ]}}


def _details_json(pageid=24362778, extract="A Buddhist temple.", langlinkscount=31, has_photo=True):
    page = {"pageid": pageid, "title": "Temple of the Tooth", "extract": extract, "langlinkscount": langlinkscount}
    if has_photo:
        page["original"] = {"source": "https://upload.wikimedia.org/x.jpg", "width": 100, "height": 100}
    return {"query": {"pages": {str(pageid): page}}}


async def test_normalize_matches_and_enriches():
    connector = WikidataEnrichConnector(limit=10)
    raw = [{"id": "listing-1", "lat": 7.2936, "lon": 80.6413}]

    with patch("app.data.connectors.wikidata_enrich.requests.get") as mock_get, \
         patch("app.data.connectors.wikidata_enrich.time.sleep"):
        mock_get.side_effect = [
            _response(json_data=_geosearch_json()),
            _response(json_data=_details_json()),
        ]
        rows = connector.normalize(raw, district=None)

    assert rows == [{
        "listing_id": "listing-1", "description": "A Buddhist temple.",
        "photo_url": "https://upload.wikimedia.org/x.jpg", "popularity_prior": 31,
    }]


async def test_normalize_no_geosearch_match_skips_listing():
    connector = WikidataEnrichConnector(limit=10)
    raw = [{"id": "listing-1", "lat": 0.0, "lon": 0.0}]

    with patch("app.data.connectors.wikidata_enrich.requests.get") as mock_get, \
         patch("app.data.connectors.wikidata_enrich.time.sleep"):
        mock_get.return_value = _response(json_data={"query": {"geosearch": []}})
        rows = connector.normalize(raw, district=None)

    assert rows == []


async def test_normalize_no_extract_skips_listing():
    """A geosearch hit with no usable extract (disambiguation page, stub)
    must not produce a row with description=None."""
    connector = WikidataEnrichConnector(limit=10)
    raw = [{"id": "listing-1", "lat": 7.29, "lon": 80.64}]

    with patch("app.data.connectors.wikidata_enrich.requests.get") as mock_get, \
         patch("app.data.connectors.wikidata_enrich.time.sleep"):
        mock_get.side_effect = [
            _response(json_data=_geosearch_json()),
            _response(json_data=_details_json(extract=None)),
        ]
        rows = connector.normalize(raw, district=None)

    assert rows == []


async def test_normalize_429_retries_then_succeeds():
    connector = WikidataEnrichConnector(limit=10)
    raw = [{"id": "listing-1", "lat": 7.2936, "lon": 80.6413}]

    with patch("app.data.connectors.wikidata_enrich.requests.get") as mock_get, \
         patch("app.data.connectors.wikidata_enrich.time.sleep"):
        mock_get.side_effect = [
            _response(status_code=429),                 # geosearch throttled once
            _response(json_data=_geosearch_json()),      # retry succeeds
            _response(json_data=_details_json()),        # details call succeeds
        ]
        rows = connector.normalize(raw, district=None)

    assert len(rows) == 1
    assert rows[0]["listing_id"] == "listing-1"


async def test_normalize_429_exhausts_retries_skips_listing():
    connector = WikidataEnrichConnector(limit=10)
    raw = [{"id": "listing-1", "lat": 7.29, "lon": 80.64}]

    with patch("app.data.connectors.wikidata_enrich.requests.get") as mock_get, \
         patch("app.data.connectors.wikidata_enrich.time.sleep"):
        mock_get.return_value = _response(status_code=429)
        rows = connector.normalize(raw, district=None)

    assert rows == []
    # _MAX_RETRIES=2 -> 3 attempts total for the one geosearch call, then give up
    assert mock_get.call_count == 3


def test_upsert_writes_description_and_image():
    connector = WikidataEnrichConnector()
    fake_cursor = MagicMock()
    fake_conn = MagicMock()
    fake_conn.__enter__ = MagicMock(return_value=fake_conn)
    fake_conn.__exit__ = MagicMock(return_value=False)
    fake_conn.cursor.return_value.__enter__ = MagicMock(return_value=fake_cursor)
    fake_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    with patch("app.data.connectors.wikidata_enrich.get_connection", return_value=fake_conn):
        count = connector.upsert([{
            "listing_id": "listing-1", "description": "desc", "photo_url": "http://x/y.jpg",
            "popularity_prior": 10,
        }])

    assert count == 1
    executed_sql = [call.args[0] for call in fake_cursor.execute.call_args_list]
    assert any("UPDATE travel_listing" in sql for sql in executed_sql)
    assert any("INSERT INTO listing_image" in sql for sql in executed_sql)


def test_upsert_empty_rows_returns_zero_without_connecting():
    connector = WikidataEnrichConnector()
    with patch("app.data.connectors.wikidata_enrich.get_connection") as mock_get_conn:
        count = connector.upsert([])
    assert count == 0
    mock_get_conn.assert_not_called()
