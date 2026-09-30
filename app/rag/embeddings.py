"""
The single construction point for every embedding call in the RAG system -
same role for embeddings that app/core/llm.py's get_llm() plays for chat
completions, and deliberately copying that module's provider-chain shape
(EMBEDDING_PROVIDER_CHAIN: "<provider>:<model>", comma separated) rather than
inventing a different one.

Team decision (2026-09-30, see settings.py): Gemini's free tier is the
active provider; a paid OpenAI embedding model may replace it later. That
switch is meant to cost a .env line and `pip install openai` - the OpenAI
branch below is already written and only imports the `openai` package
inside the function that needs it, so its absence never breaks the Gemini
path.

Not merged into app/core/llm.py itself: LangChain's chat-model abstraction
(BaseChatModel) has no embeddings equivalent that both Gemini and OpenAI
implement identically enough to share get_llm()'s _build() dispatch - each
provider's embedding SDK has its own batching/dimensionality/task-type
shape, so this module talks to the plain google-genai / openai clients
directly rather than through LangChain.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Literal, Optional

from app.config.settings import settings

logger = logging.getLogger(__name__)

TaskType = Literal["RETRIEVAL_DOCUMENT", "RETRIEVAL_QUERY"]

# Gemini's free tier enforces its embed_content quota per TEXT, not per
# HTTP call - live-confirmed 2026-09-30 ingesting the Wikivoyage corpus:
# a batch of 50 texts in one request still hit a 429 "quotaValue: 100"
# ("EmbedContentRequestsPerMinutePerUserPerProjectPerModel-FreeTier") after
# only a couple of calls. Kept well under the observed 100/minute ceiling
# rather than at the API's own (much larger) batch-size limit.
_GEMINI_BATCH_SIZE = 40
_GEMINI_MAX_RETRIES = 4
_GEMINI_DEFAULT_BACKOFF_S = 20.0
_RETRY_DELAY_RE = re.compile(r"retryDelay['\"]?\s*:\s*['\"]?(\d+(?:\.\d+)?)s")


class EmbeddingUnavailable(Exception):
    """The configured embedding provider could not be reached, or no
    provider has a configured key. Distinct from "nothing matched" - that
    is a normal empty result, this is a real failure the caller must
    degrade around (app/rag/retrieve.py falls back to full-text search)."""


def _has_key_for(provider: str) -> bool:
    if provider == "gemini":
        return bool(settings.gemini_api_key)
    if provider == "openai":
        return bool(settings.openai_api_key)
    return False


def _retry_delay_s(error: Exception) -> float:
    """Gemini's 429 body names its own suggested wait ("Please retry in
    15.9s" / a structured retryDelay field) - honoring that recovers faster
    than a fixed guess, but only when it's present and sane."""
    match = _RETRY_DELAY_RE.search(str(error))
    return float(match.group(1)) if match else _GEMINI_DEFAULT_BACKOFF_S


def _embed_gemini(model: str, texts: list[str], task_type: TaskType) -> list[list[float]]:
    from google import genai
    from google.genai import types
    from google.genai.errors import ClientError

    client = genai.Client(api_key=settings.gemini_api_key)
    config = types.EmbedContentConfig(task_type=task_type, output_dimensionality=settings.embedding_dim)

    out: list[list[float]] = []
    for i in range(0, len(texts), _GEMINI_BATCH_SIZE):
        batch = texts[i:i + _GEMINI_BATCH_SIZE]
        for attempt in range(_GEMINI_MAX_RETRIES + 1):
            try:
                response = client.models.embed_content(model=model, contents=batch, config=config)
                out.extend(e.values for e in response.embeddings)
                break
            except ClientError as e:
                # A user is waiting on a QUERY embedding (NestJS gives up at
                # 120s) - never sleep-retry one; fail fast so retrieve()
                # drops to full-text search. Same for a DAILY quota, which
                # no retry within this process can outlast (live-found
                # 2026-09-30: after the corpus ingest used the day's 1,000
                # free items, every chat question blocked ~100s retrying).
                if (e.code != 429 or attempt == _GEMINI_MAX_RETRIES
                        or task_type == "RETRIEVAL_QUERY" or "PerDay" in str(e)):
                    raise
                wait = _retry_delay_s(e)
                logger.info(f"embed_texts: Gemini rate limit, waiting {wait:.0f}s (attempt {attempt + 1})")
                time.sleep(wait)
    return out


def _embed_openai(model: str, texts: list[str], task_type: TaskType) -> list[list[float]]:
    """Written ahead of actually switching providers (see module docstring)
    - lazy import so `openai` need not be installed until this branch is
    actually reached. task_type has no OpenAI equivalent (its embeddings
    are not query/document-specialized), so it's accepted and ignored
    rather than changing this function's signature later."""
    try:
        import openai
    except ImportError as e:
        raise EmbeddingUnavailable(
            "EMBEDDING_PROVIDER_CHAIN names 'openai' but the openai package isn't installed - "
            "pip install openai"
        ) from e

    client = openai.OpenAI(api_key=settings.openai_api_key)
    response = client.embeddings.create(model=model, input=texts, dimensions=settings.embedding_dim)
    # OpenAI's response.data is not guaranteed request-order in the SDK
    # type, but the API itself always returns embeddings in input order
    # with a matching `index` - sorting by it is the documented-safe read.
    ordered = sorted(response.data, key=lambda d: d.index)
    return [d.embedding for d in ordered]


_BUILDERS = {"gemini": _embed_gemini, "openai": _embed_openai}


def _chain() -> list[tuple[str, str]]:
    specs = [s.strip() for s in settings.embedding_provider_chain.split(",") if s.strip()]
    return [tuple(s.split(":", 1)) for s in specs]  # type: ignore[misc]


def active_embedding_model() -> Optional[str]:
    """The model string the next embed_texts() call would actually use, or
    None if no provider in the chain has a key. Stored on
    knowledge_chunk.embedding_model - never invented after the fact from
    settings, since a chunk's embedding might have been produced by an
    earlier model than what's configured now."""
    for provider, model in _chain():
        if _has_key_for(provider):
            return model
    return None


def embed_texts(texts: list[str], task_type: TaskType) -> list[list[float]]:
    """Embeds `texts` with the first chain entry that has a configured key,
    trying the next on failure - same failover spirit as get_llm(), but
    synchronous (ingestion and retrieval are both fine blocking briefly;
    nothing here runs on FastAPI's request event loop uncovered - callers
    in app/rag/retrieve.py use asyncio.to_thread())."""
    if not texts:
        return []

    last_error: Optional[Exception] = None
    for provider, model in _chain():
        if not _has_key_for(provider):
            continue
        builder = _BUILDERS.get(provider)
        if builder is None:
            logger.warning(f"embed_texts: unknown embedding provider '{provider}' in chain - skipped")
            continue
        try:
            return builder(model, texts, task_type)
        except Exception as e:
            logger.warning(f"embed_texts: provider '{provider}' ({model}) failed, trying next: {e}")
            last_error = e

    raise EmbeddingUnavailable(
        "No embedding provider succeeded. Set GEMINI_API_KEY (or OPENAI_API_KEY with "
        "EMBEDDING_PROVIDER_CHAIN=openai:...) in .env."
    ) from last_error


def embed_query(text: str) -> list[float]:
    """The common single-query case - app/rag/retrieve.py's entry point."""
    return embed_texts([text], "RETRIEVAL_QUERY")[0]
