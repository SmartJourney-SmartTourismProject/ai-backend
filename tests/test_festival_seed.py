# tests/test_festival_seed.py
# No database - districts and the writer are faked. The Poya calendar is the
# real `holidays` gazette data for 2026, which is exactly what's under test.

from datetime import date

from app.data.connectors import festival_seed
from app.data.connectors.festival_seed import FestivalSeedConnector, load_rules, poya_dates, resolve_rule


def _rule(**overrides):
    base = {
        "name": "Kandy Esala Perahera", "district": "Kandy District",
        "anchor_poya": "Esala", "start_offset_days": "-9",
        "end_anchor_poya": "Esala", "end_offset_days": "0", "end_next_year": "false",
        "venue_name": "Temple of the Tooth", "lat": "7.29", "lon": "80.64",
        "tags": "culture;festival", "description": "d", "source_note": "s",
    }
    return {**base, **overrides}


def test_poya_dates_come_from_the_gazette():
    p = poya_dates(2026)
    assert p["Esala"] == date(2026, 7, 29)
    # 2026-05-01 is Workers' Day AND Vesak - the joined name must still parse.
    assert p["Vesak"] == date(2026, 5, 1)


def test_leap_month_adhi_poya_does_not_shadow_the_real_one():
    # 2026 has both "Adhi Poson" (May 30) and "Poson" (June 29). Poson
    # observance is the second; a prefix match would have picked the first.
    p = poya_dates(2026)
    assert p["Poson"] == date(2026, 6, 29)
    assert p["Adhi Poson"] == date(2026, 5, 30)


def test_resolve_rule_applies_offsets():
    cal = {2026: poya_dates(2026)}
    assert resolve_rule(_rule(), 2026, cal) == (date(2026, 7, 20), date(2026, 7, 29))


def test_resolve_rule_crosses_new_year():
    cal = {2026: {"Unduvap": date(2026, 12, 23)}, 2027: {"Vesak": date(2027, 5, 20)}}
    rule = _rule(anchor_poya="Unduvap", start_offset_days="0", end_anchor_poya="Vesak", end_next_year="true")
    assert resolve_rule(rule, 2026, cal) == (date(2026, 12, 23), date(2027, 5, 20))


def test_uncovered_year_is_skipped_not_guessed():
    assert resolve_rule(_rule(), 2027, {2027: {}}) is None
    rule = _rule(anchor_poya="Unduvap", end_anchor_poya="Vesak", end_next_year="true")
    assert resolve_rule(rule, 2026, {2026: {"Unduvap": date(2026, 12, 23)}}) is None


def test_every_seed_row_is_well_formed():
    known_poyas = set(poya_dates(2026))
    for rule in load_rules():
        assert rule["anchor_poya"] in known_poyas, rule["name"]
        assert rule["end_anchor_poya"] in known_poyas, rule["name"]
        assert rule["district"].endswith(" District"), rule["name"]
        float(rule["lat"]), float(rule["lon"])
        assert rule["tags"], rule["name"]


async def test_fetch_resolves_rules_and_skips_unknown_districts(monkeypatch):
    monkeypatch.setattr(festival_seed, "today_local", lambda: date(2026, 1, 1))
    monkeypatch.setattr(festival_seed, "load_rules", lambda: [_rule(), _rule(name="Nowhere", district="Atlantis District")])

    class _D:
        name, id = "Kandy District", "d-kandy"
    monkeypatch.setattr(festival_seed, "fetch_all_districts", lambda: [_D()])

    raw = await FestivalSeedConnector().fetch(None)

    # 2026 resolves; 2027 isn't in the gazette data yet, so it's skipped.
    assert [(r["name"], r["start"]) for r in raw] == [("Kandy Esala Perahera", date(2026, 7, 20))]
    assert raw[0]["district_id"] == "d-kandy"


def test_normalize_drops_finished_festivals_and_uses_local_days(monkeypatch):
    monkeypatch.setattr(festival_seed, "today_local", lambda: date(2026, 7, 25))
    raw = [
        {**_rule(), "district_id": "d", "start": date(2026, 7, 20), "end": date(2026, 7, 29)},
        {**_rule(name="Old"), "district_id": "d", "start": date(2026, 5, 1), "end": date(2026, 5, 2)},
    ]
    rows = FestivalSeedConnector().normalize(raw, None)

    assert [r["name"] for r in rows] == ["Kandy Esala Perahera"]
    assert rows[0]["start_datetime"].isoformat() == "2026-07-20T00:00:00+05:30"
    assert rows[0]["end_datetime"].isoformat() == "2026-07-29T23:59:59+05:30"
    assert rows[0]["tags"] == ["culture", "festival"]


def test_upsert_inserts_approved_but_never_overwrites_moderation(monkeypatch):
    captured = {}

    def fake_upsert(table, rows, **kwargs):
        captured.update(table=table, rows=rows, **kwargs)
        return len(rows)
    monkeypatch.setattr(festival_seed, "upsert_rows", fake_upsert)
    monkeypatch.setattr(festival_seed, "today_local", lambda: date(2026, 7, 1))

    raw = [{**_rule(), "district_id": "d", "start": date(2026, 7, 20), "end": date(2026, 7, 29)}]
    connector = FestivalSeedConnector()
    assert connector.upsert(connector.normalize(raw, None)) == 1

    row = captured["rows"][0]
    assert captured["table"] == "local_event"
    assert row["is_verified"] is True and row["source"] == "curated"
    assert row["price_min"] is None     # unknown, never an invented 0
    assert captured["insert_only"] == {"is_verified"}
