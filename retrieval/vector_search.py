import os
from qdrant_client import QdrantClient
from ingestion.embedder import embed_texts
from dotenv import load_dotenv

load_dotenv()

qdrant = QdrantClient(host=os.getenv('QDRANT_HOST', 'localhost'), port=int(os.getenv('QDRANT_PORT', 6333)))


def vector_search(query: str, collection: str, k: int = 20) -> list[dict]:
    vec     = embed_texts([query])[0]
    results = qdrant.query_points(
        collection_name=collection,
        query=vec,
        limit=k,
        with_payload=True,
        score_threshold=0.25
    )
    return [{'id': r.id, 'score': r.score, **r.payload} for r in results.points]