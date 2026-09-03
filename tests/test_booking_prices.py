# tests/test_booking_prices.py
# No real network or database - requests.get and get_connection are mocked
# directly (this connector uses `requests`, not httpx).

from unittest.mock import MagicMock, patch

from app.data.connectors.booking_prices import BookingPricesConnector, _price_level_bucket


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


def test_price_level_bucket_bounds():
    assert _price_level_bucket(None) is None
    assert _price_level_bucket(5_000) == 1
    assert _price_level_bucket(15_000) == 2
    assert _price_level_bucket(30_000) == 3
    assert _price_level_bucket(60_000) == 4


async def test_fetch_returns_empty_without_key(monkeypatch):
    monkeypatch.setattr("app.data.connectors.booking_prices.settings.booking_rapidapi_key", "")
    connector = BookingPricesConnector()
    result = await connector.fetch(_FakeDistrict())
    assert result == []


async def test_fetch_captures_price_rating_and_photo(monkeypatch):
    monkeypatch.setattr("app.data.connectors.booking_prices.settings.booking_rapidapi_key", "fake-key")
    connector = BookingPricesConnector()

    with patch("app.data.connectors.booking_prices.requests.Session") as MockSession:
        session = MockSession.return_value
        session.get.side_effect = [
            _response(json_data={"data": [{"country": "Sri Lanka", "dest_id": "1", "hotels": 50, "search_type": "city"}]}),
            _response(json_data={"data": {"hotels": [{
                "property": {
                    "name": "Test Hotel", "latitude": 7.29, "longitude": 80.64,
                    "priceBreakdown": {"grossPrice": {"value": 100.0, "currency": "USD"}},
                    "reviewScore": 8.9, "reviewCount": 245,
                    "photoUrls": ["https://example.com/photo.jpg"],
                },
            }]}}),
        ]
        result = await connector.fetch(_FakeDistrict())

    assert len(result) == 1
    r = result[0]
    assert r["price_usd"] == 100.0
    assert r["review_score_10"] == 8.9
    assert r["review_count"] == 245
    assert r["photo_url"] == "https://example.com/photo.jpg"


def test_normalize_converts_review_score_to_five_point_scale(monkeypatch):
    monkeypatch.setattr("app.data.connectors.booking_prices.settings.usd_lkr_rate", 300.0)
    connector = BookingPricesConnector()

    fake_cursor = MagicMock()
    fake_cursor.fetchall.return_value = [("listing-1", 7.290, 80.640)]
    fake_conn = MagicMock()
    fake_conn.__enter__ = MagicMock(return_value=fake_conn)
    fake_conn.__exit__ = MagicMock(return_value=False)
    fake_conn.cursor.return_value.__enter__ = MagicMock(return_value=fake_cursor)
    fake_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    raw = [{
        "name": "Test Hotel", "lat": 7.2901, "lon": 80.6401,   # ~110m from the listing - within 1km match radius
        "price_usd": 100.0, "currency": "USD",
        "review_score_10": 8.9, "review_count": 245, "photo_url": "https://example.com/x.jpg",
    }]

    with patch("app.data.connectors.booking_prices.get_connection", return_value=fake_conn):
        rows = connector.normalize(raw, _FakeDistrict())

    assert len(rows) == 1
    r = rows[0]
    assert r["listing_id"] == "listing-1"
    assert r["price_per_night"] == 100.0 * 300.0
    # 8.9/10 -> 1 + (8.9/10)*4 = 4.56 -> rounded to 4.6
    assert r["rating"] == 4.6
    assert r["rating_count"] == 245
    assert r["photo_url"] == "https://example.com/x.jpg"


def test_normalize_no_match_within_radius_produces_no_update():
    connector = BookingPricesConnector()
    fake_cursor = MagicMock()
    fake_cursor.fetchall.return_value = [("listing-1", 7.290, 80.640)]
    fake_conn = MagicMock()
    fake_conn.__enter__ = MagicMock(return_value=fake_conn)
    fake_conn.__exit__ = MagicMock(return_value=False)
    fake_conn.cursor.return_value.__enter__ = MagicMock(return_value=fake_cursor)
    fake_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    raw = [{  # far away - outside the 1km match radius
        "name": "Far Hotel", "lat": 8.0, "lon": 81.0,
        "price_usd": 50.0, "currency": "USD", "review_score_10": 7.0, "review_count": 10, "photo_url": None,
    }]

    with patch("app.data.connectors.booking_prices.get_connection", return_value=fake_conn):
        rows = connector.normalize(raw, _FakeDistrict())

    assert rows == []


def test_upsert_writes_rating_via_coalesce_and_image():
    connector = BookingPricesConnector()
    fake_cursor = MagicMock()
    fake_conn = MagicMock()
    fake_conn.__enter__ = MagicMock(return_value=fake_conn)
    fake_conn.__exit__ = MagicMock(return_value=False)
    fake_conn.cursor.return_value.__enter__ = MagicMock(return_value=fake_cursor)
    fake_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    with patch("app.data.connectors.booking_prices.get_connection", return_value=fake_conn):
        count = connector.upsert([{
            "listing_id": "listing-1", "price_per_night": 30000.0, "currency": "LKR",
            "price_level": 2, "rating": 4.6, "rating_count": 245, "photo_url": "https://x/y.jpg",
        }])

    assert count == 1
    executed_sql = [call.args[0] for call in fake_cursor.execute.call_args_list]
    assert any("UPDATE travel_listing" in sql for sql in executed_sql)
    assert any("INSERT INTO listing_image" in sql for sql in executed_sql)
