# tests/test_scraping.py
# No real network - requests.get is mocked, and the politeness delay is
# zeroed so the suite doesn't sleep.

from unittest.mock import MagicMock, patch

import pytest

from app.data import scraping


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch):
    monkeypatch.setattr(scraping, "_robots", {})
    monkeypatch.setattr(scraping, "_last_hit", {})
    monkeypatch.setattr(scraping, "MIN_INTERVAL_S", 0.0)


def _resp(status=200, text=""):
    r = MagicMock()
    r.status_code = status
    r.text = text
    r.encoding = "utf-8"
    return r


def _router(pages: dict):
    """requests.get stand-in: url -> response (or exception)."""
    def get(url, **kwargs):
        value = pages[url]
        if isinstance(value, Exception):
            raise value
        return value
    return get


def test_polite_get_returns_body_when_allowed():
    pages = {
        "https://ex.lk/robots.txt": _resp(200, "User-agent: *\nDisallow: /admin/\n"),
        "https://ex.lk/events": _resp(200, "<html>ok</html>"),
    }
    with patch.object(scraping.requests, "get", side_effect=_router(pages)):
        assert scraping.polite_get("https://ex.lk/events") == "<html>ok</html>"


def test_polite_get_skips_disallowed_path_without_fetching_it():
    pages = {"https://ex.lk/robots.txt": _resp(200, "User-agent: *\nDisallow: /admin/\n")}
    with patch.object(scraping.requests, "get", side_effect=_router(pages)) as get:
        assert scraping.polite_get("https://ex.lk/admin/secret") is None
    assert [c.args[0] for c in get.call_args_list] == ["https://ex.lk/robots.txt"]


def test_missing_robots_txt_means_unrestricted():
    pages = {
        "https://ex.lk/robots.txt": _resp(404),
        "https://ex.lk/page": _resp(200, "body"),
    }
    with patch.object(scraping.requests, "get", side_effect=_router(pages)):
        assert scraping.polite_get("https://ex.lk/page") == "body"


def test_robots_txt_is_fetched_once_per_host():
    pages = {
        "https://ex.lk/robots.txt": _resp(200, "User-agent: *\nAllow: /\n"),
        "https://ex.lk/a": _resp(200, "a"),
        "https://ex.lk/b": _resp(200, "b"),
    }
    with patch.object(scraping.requests, "get", side_effect=_router(pages)) as get:
        scraping.polite_get("https://ex.lk/a")
        scraping.polite_get("https://ex.lk/b")
    robots_calls = [c for c in get.call_args_list if c.args[0].endswith("robots.txt")]
    assert len(robots_calls) == 1


def test_5xx_is_retried_then_succeeds():
    responses = iter([_resp(503), _resp(200, "recovered")])

    def get(url, **kwargs):
        return _resp(404) if url.endswith("robots.txt") else next(responses)

    with patch.object(scraping.requests, "get", side_effect=get):
        assert scraping.polite_get("https://ex.lk/p") == "recovered"


def test_4xx_is_not_retried():
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        return _resp(404)

    with patch.object(scraping.requests, "get", side_effect=get):
        assert scraping.polite_get("https://ex.lk/gone") is None
    assert calls.count("https://ex.lk/gone") == 1


def test_network_errors_never_raise():
    import requests
    pages = {
        "https://ex.lk/robots.txt": requests.ConnectionError("down"),
        "https://ex.lk/p": requests.ConnectionError("down"),
    }
    with patch.object(scraping.requests, "get", side_effect=_router(pages)):
        assert scraping.polite_get("https://ex.lk/p") is None


def test_every_request_identifies_the_bot():
    pages = {
        "https://ex.lk/robots.txt": _resp(404),
        "https://ex.lk/p": _resp(200, "x"),
    }
    with patch.object(scraping.requests, "get", side_effect=_router(pages)) as get:
        scraping.polite_get("https://ex.lk/p")
    for call in get.call_args_list:
        assert call.kwargs["headers"]["User-Agent"] == scraping.USER_AGENT


def test_stable_ref_ignores_case_and_spacing():
    assert scraping.stable_ref("Kandy  Perahera", "2026-07-20") == scraping.stable_ref("kandy perahera", "2026-07-20")
    assert scraping.stable_ref("Kandy Perahera", "2026-07-20") != scraping.stable_ref("Kandy Perahera", "2027-07-20")


@pytest.mark.parametrize("text,expected", [
    ("Rs. 1,500", 1500.0),
    ("LKR 1500", 1500.0),
    ("11690.00", 11690.0),
    (2004, 2004.0),
    ("Free entry", 0.0),
    ("TBA", None),
    ("", None),
    (None, None),
])
def test_parse_lkr(text, expected):
    assert scraping.parse_lkr(text) == expected


def test_parse_lkr_converts_usd(monkeypatch):
    monkeypatch.setattr(scraping.settings, "usd_lkr_rate", 300.0)
    assert scraping.parse_lkr("USD 30") == 9000.0
    assert scraping.parse_lkr("$35.00") == 10500.0
