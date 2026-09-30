# tests/test_rag_retrieve.py
# No real database or embedding API - get_pool and embed_query are both
# faked, same convention as tests/test_db_tool.py's _FakePool.

from datetime import date
from unittest.mock import AsyncMock

import pytest

from app.rag import retrieve
from app.rag.embeddings import EmbeddingUnavailable
from app.rag.retrieve import Passage, _rrf_merge, best_vector_score, retrieve as do_retrieve


class _FakePool:
    """Returns `sequence[i]` for the i-th fetch() call, in order - a
    hybrid search always calls fetch twice (full-text, then vector), so
    tests give one list per call."""

    def __init__(self, sequence):
        self._sequence = list(sequence)
        self.calls: list[tuple] = []

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        return self._sequence.pop(0)


def _row(id_, content="text", section="Stay safe", title="Kandy", url="https://x", score=0.8):
    return {"id": id_, "content": content, "section": section, "title": title,
           "url": url, "license": "CC BY-SA 3.0", "last_verified": None, "score": score}


@pytest.fixture(autouse=True)
def _enable_rag(monkeypatch):
    monkeypatch.setattr(retrieve.settings, "enable_rag", True)
    monkeypatch.setattr(retrieve.settings, "rag_top_k", 5)


# ---- retrieve() -------------------------------------------------------

async def test_disabled_returns_empty_without_touching_the_database(monkeypatch):
    monkeypatch.setattr(retrieve.settings, "enable_rag", False)
    pool = _FakePool([])
    monkeypatch.setattr(retrieve, "get_pool", AsyncMock(return_value=pool))

    assert await do_retrieve("do I need a visa") == []
    assert pool.calls == []


async def test_no_pool_returns_empty(monkeypatch):
    monkeypatch.setattr(retrieve, "get_pool", AsyncMock(return_value=None))
    assert await do_retrieve("do I need a visa") == []


async def test_combines_vector_and_fulltext_results(monkeypatch):
    fts_rows = [_row("c1"), _row("c2")]
    vector_rows = [_row("c2"), _row("c3")]
    pool = _FakePool([fts_rows, vector_rows])
    monkeypatch.setattr(retrieve, "get_pool", AsyncMock(return_value=pool))
    monkeypatch.setattr(retrieve, "embed_query", lambda q: [0.1, 0.2])

    passages = await do_retrieve("visa rules")

    ids = {p.id for p in passages}
    assert ids == {"c1", "c2", "c3"}
    # c2 appears in both rankings, so it should outrank a single-ranking hit.
    assert passages[0].id == "c2"


async def test_embedding_unavailable_falls_back_to_fulltext_only(monkeypatch):
    fts_rows = [_row("c1")]
    pool = _FakePool([fts_rows])   # only ONE fetch call expected - vector search is skipped
    monkeypatch.setattr(retrieve, "get_pool", AsyncMock(return_value=pool))

    def raise_unavailable(q):
        raise EmbeddingUnavailable("no provider configured")
    monkeypatch.setattr(retrieve, "embed_query", raise_unavailable)

    passages = await do_retrieve("visa rules")

    assert [p.id for p in passages] == ["c1"]
    assert len(pool.calls) == 1
    assert passages[0].vector_score is None


async def test_query_failure_degrades_to_empty_not_an_exception(monkeypatch):
    class _RaisingPool:
        async def fetch(self, *a, **kw):
            raise Exception("relation \"knowledge_chunk\" does not exist")
    monkeypatch.setattr(retrieve, "get_pool", AsyncMock(return_value=_RaisingPool()))
    monkeypatch.setattr(retrieve, "embed_query", lambda q: [0.1])

    assert await do_retrieve("visa rules") == []


async def test_district_id_is_passed_through_to_both_queries(monkeypatch):
    pool = _FakePool([[], []])
    monkeypatch.setattr(retrieve, "get_pool", AsyncMock(return_value=pool))
    monkeypatch.setattr(retrieve, "embed_query", lambda q: [0.1])

    await do_retrieve("visa rules", district_id="d-kandy")

    assert all(args[1] == "d-kandy" for _sql, args in pool.calls)


async def test_result_is_capped_at_top_k(monkeypatch):
    monkeypatch.setattr(retrieve.settings, "rag_top_k", 2)
    rows = [_row(f"c{i}") for i in range(5)]
    pool = _FakePool([rows, []])
    monkeypatch.setattr(retrieve, "get_pool", AsyncMock(return_value=pool))
    monkeypatch.setattr(retrieve, "embed_query", lambda q: [0.1])

    passages = await do_retrieve("visa rules")
    assert len(passages) == 2


async def test_last_verified_is_serialized_to_iso(monkeypatch):
    row = _row("c1")
    row["last_verified"] = date(2026, 9, 30)
    pool = _FakePool([[row], []])
    monkeypatch.setattr(retrieve, "get_pool", AsyncMock(return_value=pool))
    monkeypatch.setattr(retrieve, "embed_query", lambda q: [0.1])

    [passage] = await do_retrieve("visa rules")
    assert passage.last_verified == "2026-09-30"


# ---- _rrf_merge --------------------------------------------------------

def test_rrf_merge_ranks_a_dual_hit_above_a_single_ranking_hit():
    vector_rows = [_row("only_vector", score=0.9)]
    fts_rows = [_row("only_fts")]
    merged = _rrf_merge(vector_rows, fts_rows)
    assert {p.id for p in merged} == {"only_vector", "only_fts"}


def test_rrf_merge_empty_inputs_returns_empty():
    assert _rrf_merge([], []) == []


def test_rrf_merge_preserves_vector_score_for_vector_hits_only():
    merged = _rrf_merge([_row("v1", score=0.77)], [])
    assert merged[0].vector_score == 0.77


def test_rrf_merge_fulltext_only_hit_has_no_vector_score():
    merged = _rrf_merge([], [_row("f1")])
    assert merged[0].vector_score is None


# ---- best_vector_score --------------------------------------------------

def test_best_vector_score_returns_the_max():
    passages = [
        Passage(id="a", content="", section=None, title="T", url=None, license="l",
                last_verified=None, vector_score=0.4, rrf_score=0.01),
        Passage(id="b", content="", section=None, title="T", url=None, license="l",
                last_verified=None, vector_score=0.9, rrf_score=0.02),
    ]
    assert best_vector_score(passages) == 0.9


def test_best_vector_score_none_when_every_hit_is_fulltext_only():
    passages = [Passage(id="a", content="", section=None, title="T", url=None, license="l",
                        last_verified=None, vector_score=None, rrf_score=0.01)]
    assert best_vector_score(passages) is None


def test_best_vector_score_empty_list_is_none():
    assert best_vector_score([]) is None
