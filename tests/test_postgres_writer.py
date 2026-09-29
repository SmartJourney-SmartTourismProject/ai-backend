# tests/test_postgres_writer.py
# No real database - psycopg2's connection is faked so these test the SQL this
# module builds, especially the geography binding that the Supabase REST layer
# used to handle implicitly (see docs/POSTGRES_MIGRATION_PLAN.md §3.3).

from unittest.mock import MagicMock

import app.data.postgres_writer as writer


class _FakeCursor:
    def __init__(self, fetch_result=None, fetchall_result=None):
        self.executed: list[tuple] = []
        self.executemany_calls: list[tuple] = []
        self._fetch_result = fetch_result
        self._fetchall_result = fetchall_result or []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def executemany(self, sql, seq):
        self.executemany_calls.append((sql, list(seq)))

    def fetchone(self):
        return self._fetch_result

    def fetchall(self):
        return self._fetchall_result

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake_conn(cursor):
    conn = MagicMock()
    conn.cursor.return_value = cursor
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    return conn


def _patch_conn(monkeypatch, cursor):
    monkeypatch.setattr(writer, "get_connection", lambda: _fake_conn(cursor))
    return cursor


def test_to_point_wkt_uses_lon_lat_order():
    # WKT is (lon, lat) - the reverse of how we pass coordinates everywhere
    # else, and an easy thing to get backwards.
    assert writer.to_point_wkt(lat=7.2906, lon=80.6337) == "POINT(80.6337 7.2906)"


def test_upsert_wraps_geo_columns_in_st_geogfromtext(monkeypatch):
    cur = _patch_conn(monkeypatch, _FakeCursor())

    count = writer.upsert_rows(
        "travel_listing",
        [{"external_ref": "osm-1", "name": "Hotel", "location": "POINT(80.63 7.29)"}],
        on_conflict="external_ref",
        geo_columns={"location"},
    )

    sql, rows = cur.executemany_calls[0]
    assert count == 1
    # The geography column binds through ST_GeogFromText; plain columns don't.
    assert "ST_GeogFromText(%s)" in sql
    assert sql.count("ST_GeogFromText") == 1
    assert "ON CONFLICT (external_ref) DO UPDATE" in sql
    # The conflict key itself must not be in the UPDATE SET clause.
    assert "external_ref = EXCLUDED.external_ref" not in sql
    assert rows == [("osm-1", "Hotel", "POINT(80.63 7.29)")]


def test_upsert_without_geo_columns_binds_everything_plainly(monkeypatch):
    cur = _patch_conn(monkeypatch, _FakeCursor())

    writer.upsert_rows(
        "district", [{"external_ref": "d-1", "name": "Kandy"}], on_conflict="external_ref"
    )

    sql, _ = cur.executemany_calls[0]
    assert "ST_GeogFromText" not in sql


def test_upsert_empty_rows_is_a_noop(monkeypatch):
    cur = _patch_conn(monkeypatch, _FakeCursor())
    assert writer.upsert_rows("travel_listing", [], on_conflict="external_ref") == 0
    assert cur.executemany_calls == []


def test_upsert_returns_zero_when_database_unavailable(monkeypatch):
    monkeypatch.setattr(writer, "get_connection", lambda: None)
    count = writer.upsert_rows(
        "travel_listing", [{"external_ref": "x", "name": "y"}], on_conflict="external_ref"
    )
    assert count == 0


def test_id_map_returns_name_to_id(monkeypatch):
    _patch_conn(monkeypatch, _FakeCursor(fetchall_result=[("Kandy", "uuid-1"), ("Galle", "uuid-2")]))
    assert writer.get_district_id_map() == {"Kandy": "uuid-1", "Galle": "uuid-2"}


def test_id_map_empty_when_database_unavailable(monkeypatch):
    monkeypatch.setattr(writer, "get_connection", lambda: None)
    assert writer.get_district_id_map() == {}


def test_has_column_queries_information_schema(monkeypatch):
    writer._column_cache.clear()
    cur = _patch_conn(monkeypatch, _FakeCursor(fetch_result=(1,)))

    assert writer.has_column("travel_listing", "price_per_night") is True

    sql, params = cur.executed[0]
    assert "information_schema.columns" in sql
    assert params == ("travel_listing", "price_per_night")


def test_has_column_false_for_missing_column(monkeypatch):
    writer._column_cache.clear()
    _patch_conn(monkeypatch, _FakeCursor(fetch_result=None))
    assert writer.has_column("travel_listing", "nope") is False


def test_has_column_is_cached(monkeypatch):
    writer._column_cache.clear()
    cur = _patch_conn(monkeypatch, _FakeCursor(fetch_result=(1,)))

    writer.has_column("travel_listing", "price_per_night")
    writer.has_column("travel_listing", "price_per_night")

    assert len(cur.executed) == 1  # second call served from the cache
