"""
Embedding generation for RAG.

Primary: sentence-transformers (all-MiniLM-L6-v2, 384-dim)
Fallback: deterministic hashing-based embedding so the system still
          works even if optional ML deps are missing.
"""
from __future__ import annotations

import hashlib
import logging
from typing import List, Optional

logger = logging.getLogger(__name__)

try:  # pragma: no cover - depends on environment
    from sentence_transformers import SentenceTransformer

    _TRANSFORMERS_AVAILABLE = True
except ImportError:  # pragma: no cover
    _TRANSFORMERS_AVAILABLE = False

_DEFAULT_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
_DEFAULT_DIM = 384

_model: Optional["SentenceTransformer"] = None
_model_name: str = _DEFAULT_MODEL


def _load_model() -> Optional["SentenceTransformer"]:
    """Lazily load the sentence-transformer model once per process."""
    global _model
    if not _TRANSFORMERS_AVAILABLE:
        return None
    if _model is None:
        try:
            _model = SentenceTransformer(_model_name)
            logger.info("Loaded embedding model %s", _model_name)
        except Exception as exc:  # pragma: no cover - network/model issues
            logger.warning("Failed to load embedding model %s: %s", _model_name, exc)
            _model = None
    return _model


def _hash_embedding(text: str, dim: int = _DEFAULT_DIM) -> List[float]:
    """
    Deterministic fallback embedding: feature-hash tokens into `dim` bins.

    Not semantically meaningful, but stable and functional — used only when
    sentence-transformers is unavailable so RAG retrieval still runs.
    """
    vec = [0.0] * dim
    tokens = text.lower().split()
    for token in tokens:
        digest = hashlib.md5(token.encode("utf-8")).digest()
        for i in range(4):
            idx = int.from_bytes(digest[i * 2 : i * 2 + 2], "little") % dim
            vec[idx] += 1.0
    norm = sum(v * v for v in vec) ** 0.5
    if norm > 0:
        vec = [v / norm for v in vec]
    return vec


def embed_texts(texts: List[str]) -> List[List[float]]:
    """
    Embed a list of texts into vectors.

    Returns semantic vectors when sentence-transformers is installed,
    otherwise deterministic hash-vectors (still normalized, same dim).
    """
    if not texts:
        return []
    model = _load_model()
    if model is not None:
        try:
            return model.encode(texts, convert_to_numpy=True).tolist()
        except Exception as exc:  # pragma: no cover
            logger.warning("sentence-transformers encode failed: %s", exc)
    return [_hash_embedding(t) for t in texts]


def embed_query(text: str) -> List[float]:
    """Embed a single query string."""
    if not text:
        return [0.0] * _DEFAULT_DIM
    return embed_texts([text])[0]


def embedding_dimension() -> int:
    """Return the dimension of generated embeddings."""
    model = _load_model()
    if model is not None:
        try:
            return model.get_sentence_embedding_dimension()
        except Exception:  # pragma: no cover
            pass
    return _DEFAULT_DIM
