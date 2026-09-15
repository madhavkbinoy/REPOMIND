import logging, os, time
from qdrant_client import QdrantClient
from db.qdrant_client_factory import get_client, describe
from qdrant_client.http.exceptions import UnexpectedResponse
from ingestion.embedder import embed_texts
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)
# The reporting here replaces bare `print`s. basicConfig only runs when nothing (uvicorn,
# pytest, a caller) has configured the root logger, and it installs the handler at WARNING
# so httpx/transformers INFO chatter stays muted; our own logger is raised to LOG_LEVEL
# separately, which is what keeps these diagnostics visible without drowning them.
if not logging.getLogger().handlers:
    logging.basicConfig(level=logging.WARNING, format='%(levelname)s %(name)s: %(message)s')
log.setLevel(os.getenv('LOG_LEVEL', 'INFO'))

QDRANT_HOST = os.getenv('QDRANT_HOST', 'localhost')
QDRANT_PORT = int(os.getenv('QDRANT_PORT', 6333))

qdrant = get_client()   # server, embedded or cloud -- see db/qdrant_client_factory

# Cosine floor below which a hit is not returned at all. 0.25 is the value every number
# in eval/results.md was measured with and stays the default -- the env var exists so the
# floor can be *swept* the way CONFIDENCE_THRESHOLD was, not so it can drift. It is a
# floor, not a tuned knob: on a typical in-scope query the 20th hit still scores ~0.70,
# so 0.25 almost never binds and only clips the nonsense tail of out-of-scope questions.
# Qdrant applies it as a LOWER bound because the collection is COSINE (db/setup.py);
# against a Euclid collection the identical number would mean the opposite.
SCORE_THRESHOLD = float(os.getenv('VECTOR_SCORE_THRESHOLD', 0.25))


def vector_search(query: str, collection: str, k: int = 20) -> list[dict]:
    """Dense retrieval over one Qdrant collection.

    Always returns a list -- the pipeline treats retrieval as best-effort and degrades
    to BM25-only rather than 500ing -- but never silently. Every failure is logged with
    the collection, the host and the exception, because a Qdrant outage, a mistyped
    collection name and a genuinely empty result used to be indistinguishable to the
    caller: all three produced `[]`, the confidence gate saw best_score 0.0, and the user
    got the fallback message with nothing anywhere saying the vector half was down.

    The three cases are now distinct in the log:
      ERROR ... cannot reach Qdrant       -> outage, system is running on BM25 alone
      ERROR ... collection %r not found   -> wrong/absent collection, never any hits
      INFO  ... 0 hits                    -> healthy, the query genuinely matched nothing
    """
    try:
        names = [c.name for c in qdrant.get_collections().collections]
    except Exception as e:
        log.error('vector_search: cannot reach Qdrant at %s:%s (%s: %s) -- retrieval is degraded to BM25-only',
                  QDRANT_HOST, QDRANT_PORT, type(e).__name__, e)
        return []

    if collection not in names:
        log.error('vector_search: collection %r not found at %s:%s (available: %s) -- returning no hits',
                  collection, QDRANT_HOST, QDRANT_PORT, names or 'none')
        return []

    try:
        vec = embed_texts([query])[0]
    except Exception as e:
        log.exception('vector_search: embedding failed for query %r (%s: %s)', query[:120], type(e).__name__, e)
        return []

    t0 = time.perf_counter()
    try:
        results = qdrant.query_points(
            collection_name=collection,
            query=vec,
            limit=k,
            with_payload=True,
            score_threshold=SCORE_THRESHOLD
        )
    except UnexpectedResponse as e:
        if "doesn't exist" in str(e):
            # Lost the race against a collection being dropped between the check above
            # and this query -- same outcome as "not found", different cause.
            log.error('vector_search: collection %r disappeared between the existence check and the query', collection)
        else:
            log.error('vector_search: query failed on %r (HTTP %s: %s)', collection, getattr(e, 'status_code', '?'), e)
        return []
    except Exception as e:
        log.exception('vector_search: query failed on %r (%s: %s)', collection, type(e).__name__, e)
        return []

    # str(r.id): a no-op for this store (Qdrant returns UUID point ids as str) but other
    # client versions hand back uuid.UUID objects, and _rrf in pipeline.py fuses on raw
    # dict keys -- a UUID from here and a str from bm25_search are different keys, so the
    # one thing RRF exists to do, reward documents both retrievers found, would stop
    # happening with no error anywhere. Normalise at the boundary instead.
    hits = [{'id': str(r.id), 'score': r.score, **r.payload} for r in results.points]
    if not hits:
        log.info('vector_search: 0 hits for %r in %r (k=%d, score_threshold=%s) -- store is healthy, the query matched nothing',
                 query[:120], collection, k, SCORE_THRESHOLD)
    else:
        log.debug('vector_search: %d hits in %.0f ms (top %.4f, last %.4f)',
                  len(hits), (time.perf_counter() - t0) * 1000, hits[0]['score'], hits[-1]['score'])
    return hits


def fetch_by_ids(ids: list[str], collection: str) -> list[dict]:
    """Fetch full payloads for point ids we don't already hold.

    BM25 returns bare chunk ids with no payload. Without this, fused BM25-only
    hits have nothing to rerank and get silently dropped.

    A short return is not an error but it is a symptom: SQLite's bm25_index and the
    Qdrant collection are written together by upsert_chunks but can drift apart (the
    collection is recreated for a clean rebuild while bm25_index keeps the old rows,
    since point ids are fresh UUIDs on every indexing run). Those stale ids score in
    BM25, fail to hydrate here, and vanish from the candidate list -- so it is logged.
    """
    if not ids:
        return []
    try:
        points = qdrant.retrieve(
            collection_name=collection, ids=ids, with_payload=True,
        )
    except Exception as e:
        log.error('fetch_by_ids: hydration of %d ids from %r failed (%s: %s) -- those candidates are dropped',
                  len(ids), collection, type(e).__name__, e)
        return []

    if len(points) < len(ids):
        log.warning('fetch_by_ids: %d of %d ids are absent from %r -- bm25_index and Qdrant have drifted apart',
                    len(ids) - len(points), len(ids), collection)
    return [{'id': str(p.id), 'score': None, **p.payload} for p in points]
