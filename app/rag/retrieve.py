"""
Hybrid retrieval over the knowledge base - what app/prompts/answer_prompt.py
and app/rag/tips.py both read from. Two independent rankings, merged:

  - vector search (embedding <=> query), which finds paraphrases and
    related concepts an exact keyword match misses ("do I need to dress
    modestly" -> a passage that says "cover your shoulders and knees")
  - full-text search (tsv @@ websearch_to_tsquery), which finds proper
    nouns and acronyms an embedding can blur ("Dalada Maligawa", "ETA")

merged by Reciprocal Rank Fusion (RRF) rather than either alone.

Same "degrade, never break" rule as app/tools/db_tool.py's _listing_select:
no embedding provider configured / embedding API down -> full-text only;
the knowledge_chunk table or the `vector` extension isn't there yet
(migration 0013 not applied) -> empty result with one warning, not an
exception. RAG is optional, additive infrastructure - it must never be
able to take down a request that doesn't even use it.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

from app.config.settings import settings
from app.rag.embeddings import EmbeddingUnavailable, embed_query
from app.utils.db_pool import get_pool

logger = logging.getLogger(__name__)

# How many candidates each individual ranking contributes to the RRF merge -
# wider than rag_top_k so a chunk that's merely OK in one ranking but great
# in the other still has a chance to surface.
_CANDIDATE_POOL = 20
# Standard RRF constant (Cormack et al. 2009) - large enough that a rank-1
# hit in one list doesn't totally dominate a rank-2 hit that's strong in
# both lists.
_RRF_K = 60

_VECTOR_SQL = """
    SELECT c.id, c.content, c.section, d.title, d.url, d.license, d.last_verified,
           1 - (c.embedding <=> $1) AS score
    FROM knowledge_chunk c
    JOIN knowledge_document d ON d.id = c.document_id
    WHERE c.embedding IS NOT NULL AND d.is_active
      AND ($2::uuid IS NULL OR c.district_id = $2 OR c.district_id IS NULL)
    ORDER BY c.embedding <=> $1
    LIMIT $3
"""

_FTS_SQL = """
    SELECT c.id, c.content, c.section, d.title, d.url, d.license, d.last_verified,
           ts_rank(c.tsv, websearch_to_tsquery('english', $1)) AS score
    FROM knowledge_chunk c
    JOIN knowledge_document d ON d.id = c.document_id
    WHERE c.tsv @@ websearch_to_tsquery('english', $1) AND d.is_active
      AND ($2::uuid IS NULL OR c.district_id = $2 OR c.district_id IS NULL)
    ORDER BY score DESC
    LIMIT $3
"""


@dataclass
class Passage:
    id: str
    content: str
    section: Optional[str]
    title: str
    url: Optional[str]
    license: str
    last_verified: Optional[str]
    # The raw top-1 vector cosine similarity this passage's chunk achieved
    # (None if it was found by full-text only) - what the "nothing
    # relevant" gate below actually checks, kept distinct from `rrf_score`
    # (an RRF rank fusion score has no fixed scale to compare against a
    # threshold; cosine similarity does).
    vector_score: Optional[float]
    rrf_score: float


def _rrf_merge(vector_rows: list, fts_rows: list) -> list[Passage]:
    scores: dict[str, float] = {}
    vector_scores: dict[str, Optional[float]] = {}
    by_id: dict[str, dict] = {}

    for rank, row in enumerate(vector_rows, start=1):
        scores[row["id"]] = scores.get(row["id"], 0.0) + 1.0 / (_RRF_K + rank)
        vector_scores[row["id"]] = float(row["score"])
        by_id[row["id"]] = row
    for rank, row in enumerate(fts_rows, start=1):
        scores[row["id"]] = scores.get(row["id"], 0.0) + 1.0 / (_RRF_K + rank)
        vector_scores.setdefault(row["id"], None)
        by_id.setdefault(row["id"], row)

    ranked_ids = sorted(scores, key=lambda i: scores[i], reverse=True)
    return [
        Passage(
            id=i, content=by_id[i]["content"], section=by_id[i]["section"],
            title=by_id[i]["title"], url=by_id[i]["url"], license=by_id[i]["license"],
            last_verified=by_id[i]["last_verified"].isoformat() if by_id[i]["last_verified"] else None,
            vector_score=vector_scores[i], rrf_score=scores[i],
        )
        for i in ranked_ids
    ]


async def retrieve(query: str, district_id: Optional[str] = None, top_k: Optional[int] = None) -> list[Passage]:
    """Best-effort top-k passages for `query`, or [] if RAG is off, not yet
    seeded, or every search path failed. Never raises - a knowledge-base
    problem must never break trip planning or turn a chat turn into a 500."""
    if not settings.enable_rag:
        return []

    top_k = top_k or settings.rag_top_k
    pool = await get_pool()
    if pool is None:
        logger.debug("retrieve: no database pool - RAG disabled for this call")
        return []

    query_vector = None
    try:
        query_vector = await asyncio.to_thread(embed_query, query)
    except EmbeddingUnavailable as e:
        logger.warning(f"retrieve: embedding unavailable, falling back to full-text search only: {e}")

    try:
        fts_rows = await pool.fetch(_FTS_SQL, query, district_id, _CANDIDATE_POOL)
        vector_rows = []
        if query_vector is not None:
            vector_rows = await pool.fetch(_VECTOR_SQL, query_vector, district_id, _CANDIDATE_POOL)
    except Exception as e:
        # Most likely migration 0013 hasn't been applied on this database
        # yet (relation/extension doesn't exist) - RAG is optional, so this
        # degrades to "nothing found" rather than surfacing a 500 to a
        # request that may not even be asking a question.
        logger.warning(f"retrieve: knowledge base query failed (migration 0013 applied?): {e}")
        return []

    return _rrf_merge(vector_rows, fts_rows)[:top_k]


def best_vector_score(passages: list[Passage]) -> Optional[float]:
    """The confidence gate app/prompts/answer_prompt.py's caller checks
    against settings.rag_min_score before spending an LLM call - None when
    every result came from full-text only (no calibrated score available),
    which the caller treats as "not confident enough" rather than "certain".
    """
    scores = [p.vector_score for p in passages if p.vector_score is not None]
    return max(scores) if scores else None
