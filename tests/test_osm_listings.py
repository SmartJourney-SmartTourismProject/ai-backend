# tests/test_osm_listings.py
# No real network or database - requests.Session and postgres_writer's
# helpers are mocked directly (this connector uses `requests`, not httpx).

from unittest.mock import MagicMock, patch

from app.data.connectors.base import District
from app.data.connectors.osm_listings import (
    OSMListingsConnector, _category_for, _canonical_tags, haversine_km, _query_overpass,
    _area_clause, build_query,
)


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


# ---- pure helpers ---------------------------------------------------------

def test_category_for_hotel_variants():
    assert _category_for({"tourism": "hotel"}) == "hotel"
    assert _category_for({"tourism": "guest_house"}) == "hotel"
    assert _category_for({"tourism": "hostel"}) == "hotel"


def test_category_for_restaurant_variants():
    assert _category_for({"amenity": "restaurant"}) == "restaurant"
    assert _category_for({"amenity": "cafe"}) == "restaurant"


def test_category_for_defaults_to_attraction():
    assert _category_for({"historic": "ruins"}) == "attraction"
    assert _category_for({}) == "attraction"


def test_canonical_tags_maps_and_dedupes():
    tag_map = {"tourism=hotel": ["stay"], "amenity=restaurant": ["food"]}
    result = _canonical_tags({"tourism": "hotel", "amenity": "restaurant"}, tag_map)
    assert result == ["food", "stay"]   # sorted


def test_canonical_tags_unmapped_key_produces_nothing():
    assert _canonical_tags({"weird_key": "weird_value"}, {}) == []


def test_haversine_km_zero_distance():
    assert haversine_km(7.29, 80.64, 7.29, 80.64) == 0.0


def test_haversine_km_known_pair():
    d = haversine_km(6.9271, 79.8612, 7.2906, 80.6337)   # Colombo -> Kandy
    assert 90 < d < 100


# ---- area clause: relation-id vs name lookup ---------------------------------

def test_area_clause_uses_relation_id_when_available():
    """Regression test for a real bug found live: name-based area lookup
    (area["name"=...]["admin_level"="5"]) returned zero elements for
    Kurunegala District specifically - not a rate limit, confirmed by
    querying the bare area clause and getting an empty result back.
    rel(<id>); map_to_area bypasses Overpass's derived area index and
    fixed it (23 hotels vs 0). Must be preferred whenever we have an id."""
    d = District(id="x", name="Kurunegala District", province="North Western Province",
                 lat=7.48, lon=80.36, osm_relation_id=5351778)
    clause = _area_clause(d)
    assert clause == "rel(5351778);map_to_area->.searchArea;"
    assert "name=" not in clause


def test_area_clause_falls_back_to_name_without_relation_id():
    d = District(id="x", name="Test District", province="Test Province", lat=0, lon=0,
                 osm_relation_id=None)
    clause = _area_clause(d)
    assert clause == 'area["name"="Test District"]["admin_level"="5"]->.searchArea;'


def test_build_query_embeds_the_area_clause():
    d = District(id="x", name="Kandy District", province="Central Province",
                 lat=7.29, lon=80.63, osm_relation_id=5351794)
    query = build_query(d)
    assert "rel(5351794);map_to_area->.searchArea;" in query
    assert 'tourism"="hotel"' in query


# ---- mirror rotation --------------------------------------------------------

def test_query_overpass_rotates_on_failure_status():
    session = MagicMock()
    session.post.side_effect = [
        _response(status_code=429),
        _response(status_code=200, json_data={"elements": [{"id": 1}]}),
    ]
    with patch("app.data.connectors.osm_listings.time.sleep"):
        result = _query_overpass("fake query", session)
    assert result == {"elements": [{"id": 1}]}
    assert session.post.call_count == 2


def test_query_overpass_all_mirrors_fail_returns_empty_dict():
    session = MagicMock()
    session.post.return_value = _response(status_code=504)
    with patch("app.data.connectors.osm_listings.time.sleep"):
        result = _query_overpass("fake query", session)
    assert result == {}
    # 2 attempts x 3 mirrors = 6 calls total, per the retry-the-whole-rotation design
    assert session.post.call_count == 6


# ---- normalize --------------------------------------------------------------

def test_normalize_builds_expected_row_shape():
    connector = OSMListingsConnector.__new__(OSMListingsConnector)  # skip __init__'s DB call
    connector._tag_map = {"tourism=hotel": ["stay"]}

    raw = [{
        "id": 123, "lat": 7.29, "lon": 80.64,
        "tags": {"tourism": "hotel", "name": "Test Hotel", "opening_hours": "Mo-Su 00:00-24:00"},
        "_transit_elements": [{"lat": 7.2901, "lon": 80.6401, "tags": {"name": "Test Station"}}],
    }]

    rows = connector.normalize(raw, _FakeDistrict())

    assert len(rows) == 1
    r = rows[0]
    assert r["name"] == "Test Hotel"
    assert r["category"] == "hotel"
    assert r["tags"] == ["stay"]
    assert r["has_public_transit"] is True
    assert r["nearest_transit_stop"] == "Test Station"
    assert r["source"] == "osm" and r["external_ref"] == "123"


def test_normalize_skips_elements_without_name_or_coords():
    connector = OSMListingsConnector.__new__(OSMListingsConnector)
    connector._tag_map = {}
    raw = [
        {"id": 1, "lat": 7.29, "lon": 80.64, "tags": {}},               # no name
        {"id": 2, "tags": {"name": "No coords"}},                        # no lat/lon
    ]
    assert connector.normalize(raw, _FakeDistrict()) == []


def test_normalize_empty_raw_returns_empty():
    connector = OSMListingsConnector.__new__(OSMListingsConnector)
    connector._tag_map = {}
    assert connector.normalize([], _FakeDistrict()) == []


# ---- upsert -------------------------------------------------------------

def test_upsert_uses_compound_conflict_key_and_skips_unknown_category():
    connector = OSMListingsConnector.__new__(OSMListingsConnector)
    rows = [
        {"district_id": "d1", "category": "hotel", "name": "A", "description": None,
         "lat": 7.29, "lon": 80.64, "tags": ["stay"], "opening_hours_raw": None,
         "has_public_transit": False, "nearest_transit_stop": None,
         "source": "osm", "external_ref": "1"},
        {"district_id": "d1", "category": "unknown_category", "name": "B", "description": None,
         "lat": 7.29, "lon": 80.64, "tags": [], "opening_hours_raw": None,
         "has_public_transit": False, "nearest_transit_stop": None,
         "source": "osm", "external_ref": "2"},
    ]

    with patch("app.data.connectors.osm_listings.ensure_categories",
               return_value={"hotel": "cat-hotel-id"}), \
         patch("app.data.connectors.osm_listings.upsert_rows", return_value=1) as mock_upsert:
        count = connector.upsert(rows)

    assert count == 1
    args, kwargs = mock_upsert.call_args
    db_rows = args[1]
    assert len(db_rows) == 1   # the "unknown_category" row was dropped
    assert kwargs["on_conflict"] == ("source", "external_ref")


def test_upsert_empty_rows_returns_zero_without_querying_categories():
    connector = OSMListingsConnector.__new__(OSMListingsConnector)
    with patch("app.data.connectors.osm_listings.ensure_categories") as mock_ensure:
        count = connector.upsert([])
    assert count == 0
    mock_ensure.assert_not_called()
