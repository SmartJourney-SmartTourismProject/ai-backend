"""
In-memory vector store with cosine similarity search.

Primary: FAISS (faiss-cpu) for fast approximate search.
Fallback: pure-Python cosine similarity — same API, no extra deps.
Designed for daily-rebuilt candidate pools (per the build plan), not
long-lived persistence.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from app.rag.embeddings import embed_query, embed_texts

logger = logging.getLogger(__name__)

try:  # pragma: no cover - depends on environment
    import faiss

    _FAISS_AVAILABLE = True
except ImportError:  # pragma: no cover
    _FAISS_AVAILABLE = False


def cosine_similarity(a: List[float], b: List[float]) -> float:
    """Cosine similarity between two vectors."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


class VectorStore:
    """Stores embeddings with their metadata and supports similarity search."""

    def __init__(self, index_name: str = "default") -> None:
        self.index_name = index_name
        self._vectors: List[List[float]] = []
        self._metadata: List[Dict[str, Any]] = []
        self._faiss_index = None
        self._dim: Optional[int] = None

    # ---- Construction ----------------------------------------------------

    def add_documents(
        self,
        texts: List[str],
        metadatas: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """Embed texts and store alongside their metadata."""
        if not texts:
            return
        vectors = embed_texts(texts)
        self.add_vectors(vectors, metadatas or [{} for _ in texts])

    def add_vectors(
        self,
        vectors: List[List[float]],
        metadatas: List[Dict[str, Any]],
    ) -> None:
        """Add already-computed vectors."""
        if not vectors:
            return
        if self._dim is None:
            self._dim = len(vectors[0])
            self._maybe_init_faiss(self._dim)
        self._vectors.extend(vectors)
        self._metadata.extend(metadatas)
        if self._faiss_index is not None:
            import numpy as np

            # Normalize so inner product (IndexFlatIP) equals cosine similarity,
            # matching the normalization applied to query vectors in _search_faiss.
            vecs = np.array(vectors, dtype="float32")
            faiss.normalize_L2(vecs)
            self._faiss_index.add(vecs)

    def _maybe_init_faiss(self, dim: int) -> None:
        """Initialize a FAISS index when available."""
        if _FAISS_AVAILABLE:
            try:
                self._faiss_index = faiss.IndexFlatIP(dim)  # inner product == cosine on normalized vecs
                logger.info("FAISS index initialized for %s (dim=%d)", self.index_name, dim)
            except Exception as exc:  # pragma: no cover
                logger.warning("FAISS init failed, using python fallback: %s", exc)
                self._faiss_index = None

    # ---- Search ----------------------------------------------------------

    def similarity_search(
        self,
        query: str,
        k: int = 5,
        score_threshold: Optional[float] = None,
        filter_fn=None,
    ) -> List[Dict[str, Any]]:
        """
        Return top-k documents for a query.

        filter_fn: optional callable(metadata: dict) -> bool applied before scoring.
        score_threshold: if set, only results with score >= threshold are returned.
        """
        if not self._vectors:
            return []
        query_vec = embed_query(query)

        if self._faiss_index is not None and filter_fn is None:
            return self._search_faiss(query_vec, k, score_threshold)

        return self._search_python(query_vec, k, score_threshold, filter_fn)

    def _search_faiss(
        self,
        query_vec: List[float],
        k: int,
        score_threshold: Optional[float],
    ) -> List[Dict[str, Any]]:
        import numpy as np

        q = np.array([query_vec], dtype="float32")
        faiss.normalize_L2(q)
        scores, indices = self._faiss_index.search(q, min(k, len(self._vectors)))
        results: List[Dict[str, Any]] = []
        for score, idx in zip(scores[0], indices[0]):
            if idx < 0:
                continue
            if score_threshold is not None and float(score) < score_threshold:
                continue
            results.append(
                {
                    "metadata": self._metadata[int(idx)],
                    "score": float(score),
                }
            )
        return results

    def _search_python(
        self,
        query_vec: List[float],
        k: int,
        score_threshold: Optional[float],
        filter_fn,
    ) -> List[Dict[str, Any]]:
        scored: List[Tuple[float, Dict[str, Any]]] = []
        for vec, meta in zip(self._vectors, self._metadata):
            if filter_fn is not None and not filter_fn(meta):
                continue
            score = cosine_similarity(query_vec, vec)
            if score_threshold is not None and score < score_threshold:
                continue
            scored.append((score, meta))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [{"metadata": m, "score": s} for s, m in scored[:k]]

    # ---- State -----------------------------------------------------------

    def __len__(self) -> int:
        return len(self._vectors)

    def clear(self) -> None:
        """Reset the store (used when refreshing daily data)."""
        self._vectors = []
        self._metadata = []
        self._faiss_index = None
        self._dim = None
