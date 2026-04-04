from sentence_transformers import CrossEncoder

_ce = CrossEncoder('cross-encoder/ms-marco-MiniLM-L-6-v2')

MIN_RERANK_SCORE = 0.0  # Drop chunks with negative scores - they are poor matches


def rerank(query: str, candidates: list[dict], top_n: int = 5) -> list[dict]:
    if not candidates:
        return []
    scores = _ce.predict([(query, c['text']) for c in candidates])
    ranked = sorted(zip(scores, candidates), key=lambda x: -x[0])
    # Drop chunks with negative CrossEncoder scores - they are poor matches
    return [c for score, c in ranked[:top_n] if score > MIN_RERANK_SCORE]