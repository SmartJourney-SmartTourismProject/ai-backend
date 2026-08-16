"""
High-level RAG service used by agents.

Bridges the data layer (db_tool / overpass_ingest) and the vector retriever,
and exposes a document store for travel-guide / FAQ style content (a second
RAG source: destination knowledge documents).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from app.rag.retriever import Retriever
from app.rag.vector_store import VectorStore

logger = logging.getLogger(__name__)


class RAGService:
    """
    Combines:
      - candidate-data retrieval (listings/events per category), and
      - knowledge-document retrieval (destination guides, tips, FAQs).

    Kept as a plain class (not async) so it can be used both from async
    agents and from sync scripts/tests.
    """

    def __init__(self) -> None:
        self.retriever = Retriever()
        # Knowledge-document store (destination guides, safety tips, FAQs)
        self.doc_store = VectorStore(index_name="knowledge_docs")

    # ---- Candidate-data indexing ----------------------------------------

    def index_candidate_data(
        self,
        data: Dict[str, List[Dict[str, Any]]],
        destination: Optional[str] = None,
    ) -> Dict[str, int]:
        """Index raw candidate data (hotels/restaurants/attractions/events)."""
        return self.retriever.index_categories(data, destination)

    # ---- Knowledge-document indexing -------------------------------------

    def index_documents(
        self,
        documents: List[Dict[str, Any]],
    ) -> int:
        """
        Index knowledge documents.

        Each document: {"title": str, "content": str, "metadata": {...}}
        """
        if not documents:
            return 0
        texts = [
            f"{doc.get('title', '')}\n{doc.get('content', '')}"
            for doc in documents
        ]
        metadatas = [
            {
                "title": doc.get("title"),
                "type": doc.get("type", "guide"),
                "source": doc.get("source"),
                **doc.get("metadata", {}),
            }
            for doc in documents
        ]
        self.doc_store.add_documents(texts, metadatas)
        logger.info("Indexed %d knowledge document(s)", len(documents))
        return len(documents)

    # ---- Retrieval --------------------------------------------------------

    def retrieve_candidates(
        self,
        category: str,
        query: str,
        k: int = 5,
        destination: Optional[str] = None,
        score_threshold: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """Retrieve top-k candidate items for a category."""
        return self.retriever.retrieve(
            category,
            query,
            k=k,
            destination=destination,
            score_threshold=score_threshold,
        )

    def retrieve_knowledge(
        self,
        query: str,
        k: int = 3,
        destination: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Retrieve relevant knowledge documents for a query."""
        filter_fn = None
        if destination:
            dest_lower = destination.lower()
            filter_fn = lambda meta: (  # noqa: E731
                not meta.get("destination")
                or dest_lower in str(meta.get("destination", "")).lower()
            )
        hits = self.doc_store.similarity_search(
            query, k=k, filter_fn=filter_fn
        )
        return [
            {
                "title": hit["metadata"].get("title"),
                "type": hit["metadata"].get("type"),
                "score": hit["score"],
                "metadata": hit["metadata"],
            }
            for hit in hits
        ]

    # ---- State ------------------------------------------------------------

    def clear(self) -> None:
        self.retriever.clear()
        self.doc_store.clear()


# Module-level singleton shared by agents (daily-refreshed data).
rag_service = RAGService()
