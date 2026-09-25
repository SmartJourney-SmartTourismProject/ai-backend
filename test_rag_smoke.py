"""Manual smoke test for the RAG stack (embeddings, vector store, retriever, service)."""
from app.rag.embeddings import embed_query, embed_texts, embedding_dimension
from app.rag.vector_store import VectorStore
from app.rag.retriever import Retriever
from app.rag.rag_service import RAGService


def section(title):
    print("\n" + "=" * 60)
    print(title)
    print("=" * 60)


def test_embeddings():
    section("1. Embeddings")
    dim = embedding_dimension()
    print(f"Embedding dimension: {dim}")
    vecs = embed_texts(["beach resort in Galle", "ancient temple in Kandy"])
    print(f"Embedded {len(vecs)} texts, each len={len(vecs[0])}")
    q = embed_query("waterfall hiking trail")
    print(f"Query embedding len={len(q)}")
    assert len(vecs) == 2 and len(vecs[0]) == dim
    print("OK")


def test_vector_store():
    section("2. VectorStore")
    vs = VectorStore(index_name="test")
    vs.add_documents(
        ["Luxury beach resort with spa in Galle", "Budget hostel near Kandy lake", "Mountain hiking trail with waterfalls"],
        [{"name": "Resort A"}, {"name": "Hostel B"}, {"name": "Trail C"}],
    )
    print(f"Store size: {len(vs)}")
    results = vs.similarity_search("relaxing spa beach vacation", k=2)
    for r in results:
        print(f"  {r['metadata']['name']}  score={r['score']:.4f}")
    assert len(results) == 2
    print("OK")


def test_retriever():
    section("3. Retriever")
    retriever = Retriever()
    retriever.index_categories(
        {
            "hotel": [
                {"id": 1, "name": "Cinnamon Grand", "description": "Luxury hotel in Colombo with pool", "destination": "Colombo"},
                {"id": 2, "name": "Kandy House", "description": "Cozy boutique hotel near temple", "destination": "Kandy"},
            ],
            "attraction": [
                {"id": 10, "name": "Temple of the Tooth", "description": "Sacred Buddhist temple", "destination": "Kandy"},
                {"id": 11, "name": "Galle Fort", "description": "Historic colonial fort by the sea", "destination": "Galle"},
            ],
        },
        destination=None,
    )
    hotels = retriever.retrieve("hotel", "luxury pool colombo", k=2)
    print("Hotel results:")
    for h in hotels:
        print(f"  {h['metadata']['name']}  score={h['score']:.4f}")

    kandy_attractions = retriever.retrieve("attraction", "buddhist temple", k=2, destination="Kandy")
    print("Kandy attraction results:")
    for a in kandy_attractions:
        print(f"  {a['metadata']['name']}  score={a['score']:.4f}")
    assert len(hotels) >= 1
    assert all("kandy" in (a["metadata"].get("destination") or "").lower() for a in kandy_attractions)
    print("OK")


def test_rag_service():
    section("4. RAGService (candidates + knowledge docs)")
    svc = RAGService()
    counts = svc.index_candidate_data(
        {
            "restaurant": [
                {"id": 1, "name": "Ministry of Crab", "description": "Seafood restaurant famous for crab dishes", "destination": "Colombo"},
                {"id": 2, "name": "Slightly Chilled", "description": "Casual dining bar and grill", "destination": "Colombo"},
            ]
        }
    )
    print(f"Indexed candidate counts: {counts}")

    doc_count = svc.index_documents(
        [
            {"title": "Sri Lanka Visa Guide", "content": "Tourists need an ETA to enter Sri Lanka.", "type": "faq"},
            {"title": "Safety Tips", "content": "Avoid swimming during monsoon season on the south coast.", "type": "guide"},
        ]
    )
    print(f"Indexed {doc_count} knowledge documents")

    candidates = svc.retrieve_candidates("restaurant", "seafood crab", k=1)
    print("Candidate retrieval:")
    for c in candidates:
        print(f"  {c['metadata']['name']}  score={c['score']:.4f}")

    knowledge = svc.retrieve_knowledge("do I need a visa", k=1)
    print("Knowledge retrieval:")
    for k in knowledge:
        print(f"  {k['title']}  score={k['score']:.4f}")

    assert len(candidates) == 1
    assert len(knowledge) == 1
    svc.clear()
    print("OK (cleared)")


if __name__ == "__main__":
    test_embeddings()
    test_vector_store()
    test_retriever()
    test_rag_service()
    print("\nAll RAG smoke tests passed.")
