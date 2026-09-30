# tests/test_knowledge_wikivoyage.py
# No network - requests.get is mocked with response shapes captured live
# from the real Wikivoyage API 2026-09-30 (see SCRAPE_SOURCES.md).

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.data.connectors import knowledge_wikivoyage as kw
from app.data.connectors.knowledge_wikivoyage import WikivoyageKnowledgeConnector


@pytest.fixture(autouse=True)
def _no_real_delay(monkeypatch):
    # _get()'s REQUEST_DELAY_S politeness sleep is real time.sleep(1) -
    # fine live, pure waste against a mocked requests.get in a unit test.
    monkeypatch.setattr(kw.time, "sleep", lambda seconds: None)


def _resp(json_data):
    r = MagicMock()
    r.status_code = 200
    r.headers = {}
    r.json.return_value = json_data
    r.raise_for_status.return_value = None
    return r


def test_category_pages_extracts_titles():
    data = {"query": {"categorymembers": [{"title": "Kandy"}, {"title": "Sigiriya"}]}}
    with patch.object(kw.requests, "get", return_value=_resp(data)):
        assert kw._category_pages("Category:Central Province (Sri Lanka)") == ["Kandy", "Sigiriya"]


def test_subcategories_extracts_titles():
    data = {"query": {"categorymembers": [{"title": "Category:Central Province (Sri Lanka)"}]}}
    with patch.object(kw.requests, "get", return_value=_resp(data)):
        assert kw._subcategories("Category:Sri Lanka") == ["Category:Central Province (Sri Lanka)"]


def test_fetch_page_text_returns_extract_and_url():
    data = {"query": {"pages": {"123": {
        "title": "Ella", "fullurl": "https://en.wikivoyage.org/wiki/Ella",
        "extract": "Ella is a small town.\n\n== Get in ==\nBy train.",
    }}}}
    with patch.object(kw.requests, "get", return_value=_resp(data)):
        page = kw._fetch_page_text("Ella")
    assert page == {"title": "Ella", "url": "https://en.wikivoyage.org/wiki/Ella",
                    "text": "Ella is a small town.\n\n== Get in ==\nBy train."}


def test_fetch_page_text_returns_none_for_a_missing_page():
    data = {"query": {"pages": {"-1": {"title": "Nonexistent Page", "missing": ""}}}}
    with patch.object(kw.requests, "get", return_value=_resp(data)):
        assert kw._fetch_page_text("Nonexistent Page") is None


def test_fetch_page_text_returns_none_for_an_empty_extract():
    # A redirect/disambiguation stub with no real prose - must not become
    # an empty knowledge_document.
    data = {"query": {"pages": {"1": {"title": "Stub", "fullurl": "x", "extract": "   "}}}}
    with patch.object(kw.requests, "get", return_value=_resp(data)):
        assert kw._fetch_page_text("Stub") is None


def test_discover_pages_puts_country_first_and_walks_provinces_then_cities():
    responses = {
        ("categorymembers", "Category:Sri Lanka", "page"):
            {"query": {"categorymembers": [{"title": "Western Province (Sri Lanka)"},
                                            {"title": "Wilpattu National Park"}]}},
        ("categorymembers", "Category:Sri Lanka", "subcat"):
            {"query": {"categorymembers": [{"title": "Category:Central Province (Sri Lanka)"}]}},
        ("categorymembers", "Category:Central Province (Sri Lanka)", "page"):
            {"query": {"categorymembers": [{"title": "Kandy"}, {"title": "Sigiriya"}]}},
    }

    def fake_get(url, params, headers, timeout):
        key = ("categorymembers", params["cmtitle"], params["cmtype"])
        return _resp(responses[key])

    with patch.object(kw.requests, "get", side_effect=fake_get):
        pages = kw.discover_pages()

    assert pages == ["Sri Lanka", "Western Province (Sri Lanka)", "Wilpattu National Park", "Kandy", "Sigiriya"]


def test_discover_pages_deduplicates_across_categories():
    responses = {
        ("categorymembers", "Category:Sri Lanka", "page"): {"query": {"categorymembers": []}},
        ("categorymembers", "Category:Sri Lanka", "subcat"):
            {"query": {"categorymembers": [{"title": "Category:A"}, {"title": "Category:B"}]}},
        ("categorymembers", "Category:A", "page"): {"query": {"categorymembers": [{"title": "Shared City"}]}},
        ("categorymembers", "Category:B", "page"): {"query": {"categorymembers": [{"title": "Shared City"}]}},
    }

    def fake_get(url, params, headers, timeout):
        return _resp(responses[("categorymembers", params["cmtitle"], params["cmtype"])])

    with patch.object(kw.requests, "get", side_effect=fake_get):
        pages = kw.discover_pages()

    assert pages.count("Shared City") == 1


# ---- Connector.fetch / normalize -----------------------------------------

async def test_fetch_places_each_city_page_by_district_and_country_page_gets_none(monkeypatch):
    monkeypatch.setattr(kw, "discover_pages", lambda: ["Sri Lanka", "Kandy"])

    def fake_fetch(title):
        return {"title": title, "url": f"https://en.wikivoyage.org/wiki/{title}",
                "text": f"About {title}."}
    monkeypatch.setattr(kw, "_fetch_page_text", fake_fetch)
    monkeypatch.setattr(kw, "resolve_place", AsyncMock(return_value={"district_id": "d-kandy", "confidence": "high"}))

    raw = await WikivoyageKnowledgeConnector().fetch(None)

    assert raw[0]["source_ref"] == "Sri Lanka" and raw[0]["district_id"] is None
    assert raw[1]["source_ref"] == "Kandy" and raw[1]["district_id"] == "d-kandy"


async def test_fetch_falls_back_to_country_level_when_unresolvable(monkeypatch):
    monkeypatch.setattr(kw, "discover_pages", lambda: ["Some Vague Region"])
    monkeypatch.setattr(kw, "_fetch_page_text", lambda title: {"title": title, "url": "u", "text": "t"})
    monkeypatch.setattr(kw, "resolve_place", AsyncMock(return_value=None))

    raw = await WikivoyageKnowledgeConnector().fetch(None)
    assert raw[0]["district_id"] is None


async def test_fetch_out_of_country_match_falls_back_to_country_level(monkeypatch):
    # resolve_place can return a real hit that's outside Sri Lanka entirely
    # (e.g. a page title that also names a place elsewhere) - never pin
    # Sri Lankan travel content to a foreign district.
    monkeypatch.setattr(kw, "discover_pages", lambda: ["Ambiguous Name"])
    monkeypatch.setattr(kw, "_fetch_page_text", lambda title: {"title": title, "url": "u", "text": "t"})
    monkeypatch.setattr(kw, "resolve_place", AsyncMock(
        return_value={"district_id": None, "confidence": "out_of_country"}
    ))

    raw = await WikivoyageKnowledgeConnector().fetch(None)
    assert raw[0]["district_id"] is None


async def test_fetch_skips_pages_that_failed_to_load(monkeypatch):
    monkeypatch.setattr(kw, "discover_pages", lambda: ["Sri Lanka", "Broken Page"])
    monkeypatch.setattr(kw, "_fetch_page_text", lambda title: None if title == "Broken Page"
                        else {"title": title, "url": "u", "text": "t"})
    monkeypatch.setattr(kw, "resolve_place", AsyncMock(return_value=None))

    raw = await WikivoyageKnowledgeConnector().fetch(None)
    assert [r["source_ref"] for r in raw] == ["Sri Lanka"]


def test_normalize_builds_document_inputs_with_a_stable_content_hash():
    raw = [{"title": "Kandy", "url": "https://en.wikivoyage.org/wiki/Kandy",
           "text": "About Kandy.", "source_ref": "Kandy", "district_id": "d-kandy"}]
    [doc] = WikivoyageKnowledgeConnector().normalize(raw, None)

    assert doc.source == "wikivoyage"
    assert doc.license == "CC BY-SA 3.0"
    assert doc.district_id == "d-kandy"
    assert len(doc.content_hash) == 64   # sha256 hex

    # Same text -> same hash (identical page content across runs is a no-op
    # write, per upsert_document's content_hash skip).
    [doc2] = WikivoyageKnowledgeConnector().normalize(raw, None)
    assert doc.content_hash == doc2.content_hash


def test_upsert_calls_run_ingest_and_returns_written_count(monkeypatch):
    monkeypatch.setattr(kw, "run_ingest", lambda docs: (7, 7))
    assert WikivoyageKnowledgeConnector().upsert([]) == 7
