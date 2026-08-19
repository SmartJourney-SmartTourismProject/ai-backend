"""
Retrieval layer for RAG: turns a user query + filters into ranked candidates.

The retriever indexes candidate data (listings, events, travel docs) into a
VectorStore, then returns the top-k most relevant items per category — which
the RecommendationAgent feeds to the LLM for curation.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from app.rag.vector_store import VectorStore

logger = logging.getLogger(__name__)


def _item_text(item: Dict[str, Any]) -> str:
    """Build a searchable text blob for a candidate item."""
    parts: List[str] = []
    for key in ("name", "title", "description", "category", "tags", "type"):
        val = item.get(key)
        if isinstance(val, str) and val:
            parts.append(val)
        elif isinstance(val, list):
            parts.extend(str(v) for v in val if v)
    # Interests / keywords
    interests = item.get("interests")
    if isinstance(interests, list):
        parts.extend(str(i) for i in interests)
    # Location hint
    for key in ("destination", "city", "area"):
        if item.get(key):
            parts.append(str(item[key]))
    return " ".join(parts)


def build_metadata(item: Dict[str, Any]) -> Dict[str, Any]:
    """Extract stable metadata for retrieval results."""
    return {
        "id": item.get("id"),
        "name": item.get("name") or item.get("title"),
        "category": item.get("category"),
        "destination": item.get("destination"),
        "price_range": item.get("price_range"),
        "rating": item.get("rating"),
        "raw": item,
    }


class Retriever:
    """
    Category-aware retriever.

    Usage:
        retriever = Retriever()
        retriever.index_listings("Kandy", "hotel", [hotel_dict, ...])
        hotels = await retriever.retrieve("Kandy", "hotel", "luxury beach resort", k=5)
    """

    def __init__(self) -> None:
        self._stores: Dict[str, VectorStore] = {}

    def _store_for(self, category: str) -> VectorStore:
        if category not in self._stores:
            self._stores[category] = VectorStore(index_name=category)
        return self._stores[category]

    # ---- Indexing --------------------------------------------------------

    def index_items(
        self,
        category: str,
        items: List[Dict[str, Any]],
        destination: Optional[str] = None,
    ) -> int:
        """Embed and index a list of items under a category."""
        if not items:
            return 0
        store = self._store_for(category)
        texts = [_item_text(item) for item in items]
        metadatas = []
        for item in items:
            meta = build_metadata(item)
            if destination:
                meta["destination"] = destination
            metadatas.append(meta)
        store.add_documents(texts, metadatas)
        logger.info(
            "Indexed %d item(s) into category=%s (total=%d)",
            len(items), category, len(store),
        )
        return len(items)

    def index_categories(
        self,
        data: Dict[str, List[Dict[str, Any]]],
        destination: Optional[str] = None,
    ) -> Dict[str, int]:
        """Index a dict of {category: [items]} and return per-category counts."""
        counts: Dict[str, int] = {}
        for category, items in data.items():
            counts[category] = self.index_items(category, items, destination)
        return counts

    # ---- Retrieval -------------------------------------------------------

    def retrieve(
        self,
        category: str,
        query: str,
        k: int = 5,
        score_threshold: Optional[float] = None,
        destination: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Retrieve top-k items for a query within a category."""
        store = self._store_for(category)
        filter_fn: Optional[Callable[[Dict[str, Any]], bool]] = None
        if destination:
            dest_lower = destination.lower()
            filter_fn = lambda meta: (  # noqa: E731
                not meta.get("destination") or dest_lower in str(meta["destination"]).lower()
            )
        hits = store.similarity_search(
            query,
            k=k,
            score_threshold=score_threshold,
            filter_fn=filter_fn,
        )
        return [
            {
                "item": hit["metadata"].get("raw"),
                "metadata": {k: v for k, v in hit["metadata"].items() if k != "raw"},
                "score": hit["score"],
            }
            for hit in hits
        ]

    def retrieve_all_categories(
        self,
        query: str,
        categories: Optional[List[str]] = None,
        k: int = 5,
        destination: Optional[str] = None,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Retrieve from all indexed categories at once."""
        cats = categories or list(self._stores.keys())
        result: Dict[str, List[Dict[str, Any]]] = {}
        for category in cats:
            result[category] = self.retrieve(
                category, query, k=k, destination=destination
            )
        return result

    def clear(self) -> None:
        """Clear all category stores (daily data refresh)."""
        for store in self._stores.values():
            store.clear()
        self._stores.clear()
