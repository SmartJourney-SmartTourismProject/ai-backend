"""
Shared write path for both knowledge connectors (knowledge_wikivoyage.py,
knowledge_notes.py) - one document (a Wikivoyage page, a team note) in,
its chunk rows written, its embeddings backfilled.

Split from a generic upsert_rows() call (unlike every other connector in
app/data/connectors/) because a knowledge document needs conditional
control flow a flat batch upsert can't express: skip re-chunking entirely
when content_hash is unchanged, replace ALL of a changed document's chunks
(not merge - a shrunk section must not leave orphaned old chunks behind),
and only embed the rows that just became NULL.

Synchronous (psycopg2), same as postgres_writer.py - ingestion runs outside
the FastAPI event loop.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from app.data.postgres_writer import get_connection
from app.rag import embeddings
from app.rag.chunking import RawChunk, chunk_document, embedding_text

logger = logging.getLogger(__name__)

# One call to the embedding API per this many chunks - keeps a single
# request payload reasonable and means one failure doesn't cost re-doing
# an entire large document.
_EMBED_BATCH_SIZE = 50


@dataclass
class DocumentInput:
    source: str                # 'wikivoyage' | 'team_note'
    source_ref: str
    title: str
    url: Optional[str]
    license: str
    district_id: Optional[str]
    content_hash: str
    text: str                  # section-marked plain text, ready for chunk_document()
    last_verified: Optional[str] = None   # ISO date, team_note only


def upsert_document(conn, doc: DocumentInput) -> int:
    """Writes one document + its chunks. Returns the number of chunks
    written (0 if the document was already current - `content_hash`
    unchanged - which is the common case on a re-run)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, content_hash FROM knowledge_document WHERE source = %s AND source_ref = %s",
            (doc.source, doc.source_ref),
        )
        existing = cur.fetchone()

        if existing and existing[1] == doc.content_hash:
            # Unchanged - keep its existing chunks (and their embeddings)
            # untouched. Still worth refreshing fetched_at/title/url in case
            # those alone changed (a page rename keeps the same source_ref).
            cur.execute(
                "UPDATE knowledge_document SET title = %s, url = %s, fetched_at = now() WHERE id = %s",
                (doc.title, doc.url, existing[0]),
            )
            conn.commit()
            return 0

        if existing:
            document_id = existing[0]
            cur.execute(
                "UPDATE knowledge_document SET title = %s, url = %s, license = %s, district_id = %s, "
                "content_hash = %s, last_verified = %s, fetched_at = now(), is_active = true WHERE id = %s",
                (doc.title, doc.url, doc.license, doc.district_id, doc.content_hash, doc.last_verified, document_id),
            )
            # The whole point of the hash check above: content changed, so
            # every old chunk for this document is stale - delete rather
            # than diff, since chunk boundaries can shift even for a small
            # text edit (packing is greedy over paragraphs).
            cur.execute("DELETE FROM knowledge_chunk WHERE document_id = %s", (document_id,))
        else:
            cur.execute(
                "INSERT INTO knowledge_document "
                "(source, source_ref, title, url, license, district_id, content_hash, last_verified) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (doc.source, doc.source_ref, doc.title, doc.url, doc.license,
                 doc.district_id, doc.content_hash, doc.last_verified),
            )
            document_id = cur.fetchone()[0]

        chunks = chunk_document(doc.text, breadcrumb=doc.title)
        for chunk in chunks:
            cur.execute(
                "INSERT INTO knowledge_chunk (document_id, chunk_index, section, content, district_id) "
                "VALUES (%s, %s, %s, %s, %s)",
                (document_id, chunk.chunk_index, chunk.section, chunk.content, doc.district_id),
            )
        conn.commit()
        return len(chunks)


def backfill_embeddings(conn, limit: int = 2_000) -> int:
    """Embeds every chunk with embedding IS NULL (new or just-changed
    documents), up to `limit` per call. Returns the number embedded.
    Safe to call repeatedly / after a partial failure - already-embedded
    rows are never re-selected."""
    from pgvector.psycopg2 import register_vector
    register_vector(conn)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT c.id, c.section, c.content, d.title "
            "FROM knowledge_chunk c JOIN knowledge_document d ON d.id = c.document_id "
            "WHERE c.embedding IS NULL AND d.is_active "
            "ORDER BY c.document_id, c.chunk_index LIMIT %s",
            (limit,),
        )
        rows = cur.fetchall()

    if not rows:
        return 0

    model = embeddings.active_embedding_model()
    if model is None:
        logger.warning("backfill_embeddings: no embedding provider configured - chunks stay unembedded")
        return 0

    embedded = 0
    for i in range(0, len(rows), _EMBED_BATCH_SIZE):
        batch = rows[i:i + _EMBED_BATCH_SIZE]
        texts = [
            embedding_text(title, RawChunk(section=section, content=content, chunk_index=0))
            for _id, section, content, title in batch
        ]
        try:
            vectors = embeddings.embed_texts(texts, "RETRIEVAL_DOCUMENT")
        except embeddings.EmbeddingUnavailable as e:
            logger.warning(f"backfill_embeddings: stopped after {embedded} chunks - {e}")
            break

        with conn.cursor() as cur:
            for (chunk_id, *_rest), vector in zip(batch, vectors):
                cur.execute(
                    "UPDATE knowledge_chunk SET embedding = %s, embedding_model = %s WHERE id = %s",
                    (vector, model, chunk_id),
                )
        conn.commit()
        embedded += len(batch)

    return embedded


def run_ingest(documents: list[DocumentInput]) -> tuple[int, int]:
    """Writes every document, then backfills embeddings for whatever became
    NULL. Returns (chunks_written, chunks_embedded). No-ops (0, 0) if the
    database isn't configured, matching every other connector's upsert()
    contract."""
    conn = get_connection()
    if conn is None:
        return 0, 0
    try:
        written = sum(upsert_document(conn, doc) for doc in documents)
        embedded = backfill_embeddings(conn)
        return written, embedded
    finally:
        conn.close()
