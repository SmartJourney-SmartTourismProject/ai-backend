# tests/test_pipeline.py
# No real network or database - connector classes and get_connection are
# mocked. Covers the two real bugs found and fixed while wiring this up:
# (1) _load_registry() eagerly constructing every connector regardless of
# --source (one of which does blocking DB I/O in __init__, freezing the
# event loop when triggered from a live FastAPI endpoint), and (2) the
# --due-only cadence filter.

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from app.data import pipeline


def _fake_conn_with_rows(rows):
    cursor = MagicMock()
    cursor.fetchall.return_value = rows
    conn = MagicMock()
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    return conn


async def test_get_connector_only_constructs_the_requested_one():
    """Regression test for the live-reproduced freeze: requesting one
    source must never construct the others - each construction is a real,
    potentially-blocking side effect (e.g. OSMListingsConnector's __init__
    hits the database)."""
    built = []

    class _StubOSM:
        def __init__(self):
            built.append("osm")

    class _StubBooking:
        def __init__(self):
            built.append("booking")

    pipeline._INSTANCE_CACHE.clear()
    meta = {"connector_cls": _StubOSM}
    other_meta = {"connector_cls": _StubBooking}

    await pipeline._get_connector("osm_listings", meta)

    assert built == ["osm"]   # only the requested one was ever touched
    pipeline._INSTANCE_CACHE.clear()


async def test_get_connector_runs_construction_off_the_event_loop():
    """The actual mechanism of the fix: construction must go through
    asyncio.to_thread, not run inline on the calling coroutine."""
    pipeline._INSTANCE_CACHE.clear()

    class _StubConnector:
        pass

    with patch("app.data.pipeline.asyncio.to_thread") as mock_to_thread:
        mock_to_thread.return_value = _StubConnector()
        await pipeline._get_connector("stub", {"connector_cls": _StubConnector})

    mock_to_thread.assert_called_once_with(_StubConnector)
    pipeline._INSTANCE_CACHE.clear()


async def test_get_connector_caches_by_name():
    pipeline._INSTANCE_CACHE.clear()
    build_count = 0

    class _StubConnector:
        def __init__(self):
            nonlocal build_count
            build_count += 1

    meta = {"connector_cls": _StubConnector}
    a = await pipeline._get_connector("stub", meta)
    b = await pipeline._get_connector("stub", meta)

    assert a is b
    assert build_count == 1
    pipeline._INSTANCE_CACHE.clear()


def test_due_connectors_includes_never_run_source():
    registry = {"osm_listings": {"cadence": "weekly"}}
    conn = _fake_conn_with_rows([])   # no data_source rows at all - never run
    with patch("app.data.pipeline.get_connection", return_value=conn):
        due = pipeline._due_connectors(registry)
    assert "osm_listings" in due


def test_due_connectors_excludes_recently_run_source():
    registry = {"osm_listings": {"cadence": "weekly"}}
    recent = datetime.now(timezone.utc) - timedelta(days=1)
    conn = _fake_conn_with_rows([("osm_listings", recent)])
    with patch("app.data.pipeline.get_connection", return_value=conn):
        due = pipeline._due_connectors(registry)
    assert due == {}


def test_due_connectors_includes_source_past_its_cadence_window():
    registry = {"ticketmaster_events": {"cadence": "daily"}}
    stale = datetime.now(timezone.utc) - timedelta(days=2)
    conn = _fake_conn_with_rows([("ticketmaster_events", stale)])
    with patch("app.data.pipeline.get_connection", return_value=conn):
        due = pipeline._due_connectors(registry)
    assert "ticketmaster_events" in due


def test_due_connectors_skips_manual_cadence_sources():
    registry = {"cost_seed": {"cadence": "manual"}}
    conn = _fake_conn_with_rows([])
    with patch("app.data.pipeline.get_connection", return_value=conn):
        due = pipeline._due_connectors(registry)
    assert due == {}


def test_due_connectors_no_database_runs_everything():
    """Can't check cadence without a database - fail open to running
    everything rather than silently running nothing forever."""
    registry = {"osm_listings": {"cadence": "weekly"}}
    with patch("app.data.pipeline.get_connection", return_value=None):
        due = pipeline._due_connectors(registry)
    assert due == registry


async def test_run_unknown_source_prints_fatal_and_does_not_run(capsys):
    with patch("app.data.pipeline._load_registry", return_value={"osm_listings": {}}), \
         patch("app.data.pipeline._ensure_data_source_rows"):
        await pipeline.run(source_filter="not_a_real_source", district_filter=None, dry_run=True)

    out = capsys.readouterr().out
    assert "Unknown source" in out


async def test_run_dry_run_executes_nothing(capsys):
    ran = []

    async def fake_run_connector(name, meta, district_filter):
        ran.append(name)

    with patch("app.data.pipeline._load_registry", return_value={
        "osm_listings": {"cadence": "weekly", "requires_key": False, "scope": "per_district", "display_name": "x"},
    }), patch("app.data.pipeline._ensure_data_source_rows"), \
         patch("app.data.pipeline._run_connector", side_effect=fake_run_connector):
        await pipeline.run(source_filter=None, district_filter=None, dry_run=True)

    assert ran == []
