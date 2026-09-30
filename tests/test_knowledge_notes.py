# tests/test_knowledge_notes.py
# No real database - fetch_all_districts and run_ingest are faked. Real
# filesystem, against a temp NOTES_DIR (not app/data/knowledge/ - these
# tests must pass unaffected by whatever real notes exist there).

from unittest.mock import AsyncMock

import pytest

from app.data.connectors import knowledge_notes as kn
from app.data.connectors.knowledge_notes import KnowledgeNotesConnector, parse_note


def _write(tmp_path, name, content):
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


NOTE = """---
title: Test Note
source_url: https://example.gov.lk/x
district: Kandy District
last_verified: 2026-09-30
---
## Heading
Body text.
"""

NATIONAL_NOTE = """---
title: National Note
source_url: https://example.gov.lk/y
last_verified: 2026-09-30
---
Body with no heading.
"""


def test_parse_note_reads_front_matter_and_body(tmp_path):
    path = _write(tmp_path, "test-note.md", NOTE)
    note = parse_note(path)
    assert note["title"] == "Test Note"
    assert note["source_url"] == "https://example.gov.lk/x"
    assert note["district_name"] == "Kandy District"
    assert note["last_verified"].isoformat() == "2026-09-30"
    assert note["text"] == "## Heading\nBody text."


def test_parse_note_blank_district_is_national(tmp_path):
    path = _write(tmp_path, "national.md", NATIONAL_NOTE)
    assert parse_note(path)["district_name"] is None


def test_parse_note_missing_front_matter_raises(tmp_path):
    path = _write(tmp_path, "broken.md", "Just plain text, no front matter.")
    with pytest.raises(ValueError, match="front matter"):
        parse_note(path)


def test_parse_note_missing_title_raises(tmp_path):
    path = _write(tmp_path, "no-title.md", "---\nsource_url: x\n---\nBody.")
    with pytest.raises(ValueError, match="title"):
        parse_note(path)


# ---- Connector.fetch -------------------------------------------------------

class _D:
    def __init__(self, name, id_):
        self.name, self.id = name, id_


async def test_fetch_resolves_known_district_by_name(tmp_path, monkeypatch):
    monkeypatch.setattr(kn, "NOTES_DIR", tmp_path)
    _write(tmp_path, "a.md", NOTE)
    monkeypatch.setattr(kn, "fetch_all_districts", lambda: [_D("Kandy District", "d-kandy")])

    raw = await KnowledgeNotesConnector().fetch(None)

    assert raw[0]["district_id"] == "d-kandy"


async def test_fetch_national_note_has_no_district(tmp_path, monkeypatch):
    monkeypatch.setattr(kn, "NOTES_DIR", tmp_path)
    _write(tmp_path, "b.md", NATIONAL_NOTE)
    monkeypatch.setattr(kn, "fetch_all_districts", lambda: [])

    raw = await KnowledgeNotesConnector().fetch(None)
    assert raw[0]["district_id"] is None


async def test_fetch_unknown_district_name_falls_back_to_national_with_a_warning(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(kn, "NOTES_DIR", tmp_path)
    _write(tmp_path, "c.md", NOTE)   # names "Kandy District"
    monkeypatch.setattr(kn, "fetch_all_districts", lambda: [])   # no districts known

    raw = await KnowledgeNotesConnector().fetch(None)
    assert raw[0]["district_id"] is None
    assert "unknown district" in caplog.text


async def test_fetch_skips_a_broken_note_but_keeps_the_rest(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(kn, "NOTES_DIR", tmp_path)
    _write(tmp_path, "good.md", NATIONAL_NOTE)
    _write(tmp_path, "bad.md", "no front matter here")
    monkeypatch.setattr(kn, "fetch_all_districts", lambda: [])

    raw = await KnowledgeNotesConnector().fetch(None)
    assert len(raw) == 1
    assert raw[0]["title"] == "National Note"


async def test_fetch_missing_notes_dir_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(kn, "NOTES_DIR", tmp_path / "does-not-exist")
    assert await KnowledgeNotesConnector().fetch(None) == []


# ---- normalize / upsert ---------------------------------------------------

def test_normalize_carries_last_verified_as_iso_date():
    raw = [{"title": "T", "source_url": "u", "district_name": None, "district_id": None,
           "last_verified": __import__("datetime").date(2026, 9, 30),
           "text": "body", "source_ref": "t"}]
    [doc] = KnowledgeNotesConnector().normalize(raw, None)
    assert doc.last_verified == "2026-09-30"
    assert doc.license == "internal"
    assert doc.source == "team_note"


def test_normalize_handles_a_missing_last_verified():
    raw = [{"title": "T", "source_url": "u", "district_name": None, "district_id": None,
           "last_verified": None, "text": "body", "source_ref": "t"}]
    [doc] = KnowledgeNotesConnector().normalize(raw, None)
    assert doc.last_verified is None


def test_every_real_note_file_parses_without_error():
    # Guards the actual checked-in notes, not a fixture - a broken front
    # matter in a real note must fail loudly here, not at 2am on the
    # scheduler.
    for path in sorted(kn.NOTES_DIR.glob("*.md")):
        note = parse_note(path)
        assert note["title"]
        assert note["text"]


def test_upsert_calls_run_ingest_and_returns_written_count(monkeypatch):
    monkeypatch.setattr(kn, "run_ingest", lambda docs: (9, 9))
    assert KnowledgeNotesConnector().upsert([]) == 9
