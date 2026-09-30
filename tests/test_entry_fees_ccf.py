# tests/test_entry_fees_ccf.py
# No network or database: the parser runs against the real CCF price table
# (tests/fixtures/scrape/ccf_ticket_prices.html, captured 2026-09-30), and
# the geocoder, pool and psycopg2 connection are faked.

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from app.data.connectors import entry_fees_ccf as ccf
from app.data.connectors.entry_fees_ccf import CCFEntryFeesConnector, parse_fee_table, split_site_name

FIXTURE = (Path(__file__).parent / "fixtures" / "scrape" / "ccf_ticket_prices.html").read_text(encoding="utf-8")


def test_parses_real_table_rows():
    fees = {f["site_name"]: f for f in parse_fee_table(FIXTURE)}
    assert fees["Sigiriya"]["foreign_adult"] == 11690.0
    assert fees["Sigiriya"]["foreign_child"] == 6680.0
    assert fees["Polonnaruwa"]["foreign_adult"] == 10020.0


def test_site_with_no_half_ticket_has_no_child_price():
    fees = {f["site_name"]: f for f in parse_fee_table(FIXTURE)}
    assert fees["Sigiriya (Museum)"]["foreign_adult"] == 2004.0
    assert fees["Sigiriya (Museum)"]["foreign_child"] is None


def test_skips_header_group_headings_and_activity_subrows():
    names = [f["site_name"] for f in parse_fee_table(FIXTURE)]
    assert "Site" not in names
    assert "Galle" not in names                      # heading row, no price
    assert not any(n.startswith("-") for n in names)  # diving tours etc.
    assert "Galle (Museum)" in names


def test_falls_back_to_usd_when_lkr_cell_is_empty(monkeypatch):
    monkeypatch.setattr("app.data.scraping.settings.usd_lkr_rate", 300.0)
    html = "<table><tr><td>Site X</td><td>10.00</td><td></td><td></td><td></td></tr></table>"
    assert parse_fee_table(html) == [{"site_name": "Site X", "foreign_adult": 3000.0, "foreign_child": None}]


def test_page_without_table_yields_nothing():
    assert parse_fee_table("<html><body>maintenance</body></html>") == []


def test_split_site_name():
    assert split_site_name("Galle (Museum)") == ("Galle", "Galle Museum")
    assert split_site_name("Ritigala") == ("Ritigala", "Ritigala")


async def test_fetch_suggests_only_confident_matches(monkeypatch):
    html = ("<table>"
            "<tr><td>Ritigala</td><td>6.00</td><td>2004.00</td><td>3.00</td><td>1002.00</td></tr>"
            "<tr><td>Dambulla (Museum)</td><td>3.00</td><td>1002.00</td><td>1.50</td><td>501.00</td></tr>"
            "</table>")
    monkeypatch.setattr(ccf, "polite_get", lambda url: html)
    monkeypatch.setattr(ccf, "resolve_place", AsyncMock(return_value={"district_id": "d1"}))

    pool = MagicMock()

    async def fetchrow(sql, query, district_id):
        # Ritigala: the CCF name is contained in the listing name. Dambulla
        # Museum: the best candidate is a different museum - must NOT link.
        if query == "Ritigala":
            return {"id": "listing-ritigala", "name": "Ritigala Ancient Buddhist Monastery", "ws": 1.0}
        return {"id": "listing-wrong", "name": "Badulla Museum", "ws": 0.48}
    pool.fetchrow = fetchrow
    monkeypatch.setattr(ccf, "get_pool", AsyncMock(return_value=pool))

    rows = {r["site_name"]: r for r in await CCFEntryFeesConnector().fetch(None)}

    assert rows["Ritigala"]["listing_id"] == "listing-ritigala"
    assert rows["Dambulla (Museum)"]["listing_id"] is None


async def test_fetch_without_database_still_returns_fees_unlinked(monkeypatch):
    monkeypatch.setattr(ccf, "polite_get", lambda url: FIXTURE)
    monkeypatch.setattr(ccf, "get_pool", AsyncMock(return_value=None))
    rows = await CCFEntryFeesConnector().fetch(None)
    assert rows and all(r["listing_id"] is None for r in rows)


def test_upsert_sql_preserves_review_but_reopens_changed_prices(monkeypatch):
    cursor = MagicMock()
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor
    conn.__enter__.return_value = conn
    monkeypatch.setattr(ccf, "get_connection", lambda: conn)

    connector = CCFEntryFeesConnector()
    rows = connector.normalize([{"site_name": "Sigiriya", "foreign_adult": 11690.0,
                                 "foreign_child": 6680.0, "listing_id": None}], None)
    assert connector.upsert(rows) == 1

    sql, params = cursor.executemany.call_args.args
    assert "'pending'" in sql and "ON CONFLICT (source, external_ref)" in sql
    # An approved fee whose price changed goes back for review...
    assert "WHEN listing_entry_fee.status = 'approved'" in sql
    # ...and an admin's listing link is never replaced by a new guess.
    assert "COALESCE(listing_entry_fee.listing_id, EXCLUDED.listing_id)" in sql
    assert list(params)[0][1] == "Sigiriya"


def test_external_ref_is_stable_per_site():
    a = CCFEntryFeesConnector().normalize([{"site_name": "Sigiriya", "listing_id": None}], None)
    b = CCFEntryFeesConnector().normalize([{"site_name": "sigiriya", "listing_id": None}], None)
    assert a[0]["external_ref"] == b[0]["external_ref"]
