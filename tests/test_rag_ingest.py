# tests/test_rag_ingest.py
# No real database - psycopg2 is faked. pgvector.psycopg2.register_vector
# is patched to a no-op: it introspects a real connection's pg_type oid,
# which a MagicMock can't answer meaningfully, and isn't what's under test
# here (embeddings.py's own tests cover the embedding calls).

from unittest.mock import MagicMock, patch

import pytest

from app.rag import embeddings, ingest
from app.rag.ingest import DocumentInput, backfill_embeddings, run_ingest, upsert_document


class _FakeCursor:
    """Tracks every execute() call; fetchone()/fetchall() are scripted via
    a queue set per-test, since upsert_document interleaves SELECTs and
    INSERTs and needs the right answer at the right point."""

    def __init__(self, fetchone_queue=None, fetchall_result=None):
        self.executed: list[tuple[str, tuple]] = []
        self._fetchone_queue = list(fetchone_queue or [])
        self._fetchall_result = fetchall_result or []

    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self._fetchone_queue.pop(0) if self._fetchone_queue else None

    def fetchall(self):
        return self._fetchall_result

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, cursor: _FakeCursor):
        self._cursor = cursor
        self.commits = 0

    def cursor(self):
        return self._cursor

    def commit(self):
        self.commits += 1

    def close(self):
        pass


@pytest.fixture(autouse=True)
def _no_op_register_vector(monkeypatch):
    monkeypatch.setattr("pgvector.psycopg2.register_vector", lambda conn: None)


def _doc(**overrides):
    base = dict(
        source="wikivoyage", source_ref="Kandy", title="Kandy",
        url="https://en.wikivoyage.org/wiki/Kandy", license="CC BY-SA 3.0",
        district_id="d-kandy", content_hash="hash-1",
        text="== Stay safe ==\nWatch for touts near the temple.",
    )
    return DocumentInput(**{**base, **overrides})


# ---- upsert_document ---------------------------------------------------

def test_new_document_is_inserted_with_its_chunks():
    cur = _FakeCursor(fetchone_queue=[None, ("doc-1",)])   # SELECT existing -> none, INSERT RETURNING id
    conn = _FakeConn(cur)

    count = upsert_document(conn, _doc())

    assert count == 1   # one chunk: "Watch for touts near the temple."
    kinds = [sql.split()[0] for sql, _ in cur.executed]
    assert kinds == ["SELECT", "INSERT", "INSERT"]   # lookup, document, chunk
    assert conn.commits == 1


def test_unchanged_content_hash_skips_rechunking_entirely():
    cur = _FakeCursor(fetchone_queue=[("doc-1", "hash-1")])   # existing, same hash
    conn = _FakeConn(cur)

    count = upsert_document(conn, _doc(content_hash="hash-1"))

    assert count == 0
    kinds = [sql.split()[0] for sql, _ in cur.executed]
    assert kinds == ["SELECT", "UPDATE"]   # only the fetched_at/title touch-up
    assert not any("knowledge_chunk" in sql for sql, _ in cur.executed)


def test_changed_content_hash_deletes_old_chunks_before_inserting_new_ones():
    cur = _FakeCursor(fetchone_queue=[("doc-1", "old-hash")])   # existing, different hash
    conn = _FakeConn(cur)

    count = upsert_document(conn, _doc(content_hash="new-hash"))

    assert count == 1
    executed_sql = [sql for sql, _ in cur.executed]
    assert any(sql.startswith("UPDATE knowledge_document") for sql in executed_sql)
    delete_idx = next(i for i, sql in enumerate(executed_sql) if sql.startswith("DELETE FROM knowledge_chunk"))
    insert_idx = next(i for i, sql in enumerate(executed_sql) if sql.startswith("INSERT INTO knowledge_chunk"))
    assert delete_idx < insert_idx   # old chunks gone before new ones land, never both at once
    assert cur.executed[delete_idx][1] == ("doc-1",)


def test_multi_section_document_inserts_one_chunk_row_per_chunk():
    text = "== A ==\nFirst section text.\n\n== B ==\nSecond section text."
    cur = _FakeCursor(fetchone_queue=[None, ("doc-2",)])
    conn = _FakeConn(cur)

    count = upsert_document(conn, _doc(text=text))

    assert count == 2
    chunk_inserts = [p for sql, p in cur.executed if sql.startswith("INSERT INTO knowledge_chunk")]
    assert [p[1] for p in chunk_inserts] == [0, 1]   # chunk_index 0, 1
    assert chunk_inserts[0][4] == "d-kandy"   # district_id carried onto every chunk


# ---- backfill_embeddings ------------------------------------------------

def test_backfill_embeds_only_null_rows_and_records_the_model(monkeypatch):
    cur = _FakeCursor(fetchall_result=[
        ("chunk-1", "Stay safe", "Watch your bag.", "Kandy"),
        ("chunk-2", None, "Lead paragraph.", "Kandy"),
    ])
    conn = _FakeConn(cur)
    monkeypatch.setattr(embeddings, "active_embedding_model", lambda: "gemini-embedding-001")
    monkeypatch.setattr(embeddings, "embed_texts", lambda texts, task_type: [[0.1] * 3] * len(texts))

    count = backfill_embeddings(conn)

    assert count == 2
    updates = [(sql, p) for sql, p in cur.executed if sql.startswith("UPDATE knowledge_chunk")]
    assert len(updates) == 2
    assert updates[0][1] == ([0.1, 0.1, 0.1], "gemini-embedding-001", "chunk-1")


def test_backfill_is_a_noop_when_nothing_is_pending(monkeypatch):
    cur = _FakeCursor(fetchall_result=[])
    conn = _FakeConn(cur)
    monkeypatch.setattr(embeddings, "embed_texts", MagicMock(side_effect=AssertionError("must not be called")))

    assert backfill_embeddings(conn) == 0


def test_backfill_skips_when_no_provider_is_configured(monkeypatch):
    cur = _FakeCursor(fetchall_result=[("chunk-1", None, "text", "Title")])
    conn = _FakeConn(cur)
    monkeypatch.setattr(embeddings, "active_embedding_model", lambda: None)

    assert backfill_embeddings(conn) == 0


def test_backfill_stops_cleanly_on_embedding_failure_partway(monkeypatch):
    rows = [(f"chunk-{i}", None, "text", "Title") for i in range(3)]
    cur = _FakeCursor(fetchall_result=rows)
    conn = _FakeConn(cur)
    monkeypatch.setattr(ingest, "_EMBED_BATCH_SIZE", 1)
    monkeypatch.setattr(embeddings, "active_embedding_model", lambda: "gemini-embedding-001")

    calls = {"n": 0}

    def flaky_embed(texts, task_type):
        calls["n"] += 1
        if calls["n"] == 2:
            raise embeddings.EmbeddingUnavailable("quota exceeded")
        return [[0.1]]
    monkeypatch.setattr(embeddings, "embed_texts", flaky_embed)

    # First batch succeeds and commits; second fails and the loop stops -
    # a partial run must leave the first batch's work in place, not roll
    # everything back, since it's cheap to resume from where it stopped.
    assert backfill_embeddings(conn) == 1


# ---- run_ingest ----------------------------------------------------------

def test_run_ingest_no_database_returns_zero_zero(monkeypatch):
    monkeypatch.setattr(ingest, "get_connection", lambda: None)
    assert run_ingest([_doc()]) == (0, 0)


def test_run_ingest_writes_then_backfills_and_always_closes(monkeypatch):
    cur = _FakeCursor(fetchone_queue=[None, ("doc-1",)], fetchall_result=[])
    conn = MagicMock(wraps=_FakeConn(cur))
    conn.cursor.side_effect = lambda: cur
    monkeypatch.setattr(ingest, "get_connection", lambda: conn)
    monkeypatch.setattr(embeddings, "active_embedding_model", lambda: None)

    written, embedded = run_ingest([_doc()])

    assert written == 1
    assert embedded == 0
    conn.close.assert_called_once()
