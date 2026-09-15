import logging, os, sqlite3, threading, time
from rank_bm25 import BM25Okapi
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)
# See vector_search.py: configures the root handler only if nobody else has, at WARNING,
# and raises just this module's logger to LOG_LEVEL.
if not logging.getLogger().handlers:
    logging.basicConfig(level=logging.WARNING, format='%(levelname)s %(name)s: %(message)s')
log.setLevel(os.getenv('LOG_LEVEL', 'INFO'))

DB_PATH = os.getenv('DATABASE_PATH', './db/repomind.db')

# The entire bm25_index table lives in this process: 113k documents of full chunk text,
# built once and held for the life of the process.
#
# SQLite FTS5 over the same table would remove the resident corpus entirely -- no load
# time, no 100s of MB of tokenised text, and newly-indexed chunks visible immediately
# with no cache to invalidate. It is NOT done here on purpose: FTS5's bm25() is a
# different ranking function with its own tokenizer, its own k1/b and its own handling
# of `--eviction-hard`-shaped tokens, so it would change which chunks come back and in
# what order. That has to be run as an ablation against eval/run_eval.py and compared
# with the 0.840 BM25-only / 0.920 fused baseline before it can be adopted.
#
# Until then the cache is explicitly invalidatable: chunks written by /api/index or the
# GitHub webhook after this process started are invisible to keyword search until
# something calls invalidate() or refresh(), or the process restarts.

# Opt-in staleness check. 0 (the default) = off, i.e. exactly the previous behaviour:
# load once, never look again. Set to e.g. 60 in a long-running API process that indexes
# in-process, and a search will rebuild the corpus at most once per interval when the
# bm25_index row count has changed.
AUTO_REFRESH_SECONDS = int(os.getenv('BM25_AUTO_REFRESH_SECONDS', 0))

_lock:       threading.Lock     = threading.Lock()
_index:      BM25Okapi | None   = None
_ids:        list[str]  | None  = None
_loaded:     bool               = False
_checked_at: float              = 0.0
_stats:      dict               = {'docs': 0, 'load_seconds': 0.0, 'loaded_at': None, 'error': None}


def _load(force: bool = False):
    """Build the in-memory corpus. Idempotent, thread-safe, cheap to call.

    `retrieve` runs in a thread-pool executor (api/routes/chat.py), so two concurrent
    first requests used to build two 113k-document BM25 indexes at once and throw one
    away. The lock makes the second waiter reuse the first one's work. The new index is
    published in a single assignment at the end so a concurrent reader always sees a
    complete corpus -- the old one or the new one, never a half-built one.
    """
    global _index, _ids, _loaded, _checked_at
    with _lock:
        if _loaded and not force:
            return
        t0 = time.perf_counter()
        try:
            db   = sqlite3.connect(DB_PATH)
            rows = db.execute('SELECT chunk_id, text FROM bm25_index').fetchall()
            db.close()
        except Exception as e:
            _loaded = True
            _stats.update(error=f'{type(e).__name__}: {e}')
            log.error('BM25 corpus unavailable from %s (%s) -- keyword search is DISABLED, retrieval is vector-only',
                      DB_PATH, _stats['error'])
            return

        if not rows:
            _loaded = True
            _stats.update(docs=0, load_seconds=time.perf_counter() - t0, loaded_at=time.time(), error='bm25_index is empty')
            log.warning('BM25 corpus is EMPTY: %s has 0 rows in bm25_index -- every bm25_search() will return [] because '
                        'there is nothing to search, not because nothing matched. Run index_small.py.', DB_PATH)
            return

        ids     = [r[0] for r in rows]
        # Tokenisation must stay identical to the query side in bm25_search().
        corpus  = [(r[1] or '').lower().split() for r in rows]
        index   = BM25Okapi(corpus)
        elapsed = time.perf_counter() - t0

        _ids, _index, _loaded, _checked_at = ids, index, True, time.time()
        _stats.update(docs=len(rows), load_seconds=elapsed, loaded_at=time.time(), error=None)
        log.info('BM25 index loaded: %d docs from %s in %.2fs', len(rows), DB_PATH, elapsed)


def invalidate():
    """Drop the cached corpus; the next bm25_search() rebuilds it from SQLite.

    Call this after anything writes to bm25_index in this process (workers/tasks.py
    full_index_repo / update_issue_task, index_small.py). Without it, keyword search
    keeps answering from the corpus as it looked at process start.
    """
    global _loaded
    with _lock:
        _loaded = False
    log.info('BM25 index invalidated -- next search reloads from %s', DB_PATH)


def refresh():
    """Rebuild the corpus now, keeping the old one queryable until the new one is ready.

    If the rebuild fails (DB locked, file gone) the previous corpus stays in service and
    index_status()['error'] says why it is stale, rather than keyword search going dark.
    A load failure is not retried automatically -- call invalidate()/refresh() again --
    so a broken DB cannot turn every query into a failing SQLite round-trip.
    """
    _load(force=True)


def index_status() -> dict:
    """Why a search came back empty. 'no corpus' and 'no matches' are different bugs.

    Callers that need to act on the difference (health checks, /api/index, tests) should
    read this rather than inferring from an empty list.
    """
    return {'loaded': _loaded, 'ready': _index is not None, **_stats}


def _is_stale() -> bool:
    """True if bm25_index has a different row count than the corpus we hold."""
    global _checked_at
    now = time.time()
    if now - _checked_at < AUTO_REFRESH_SECONDS:
        return False
    _checked_at = now
    try:
        db = sqlite3.connect(DB_PATH)
        n  = db.execute('SELECT count(*) FROM bm25_index').fetchone()[0]
        db.close()
    except Exception as e:
        log.warning('BM25 staleness check failed (%s: %s) -- keeping the cached corpus', type(e).__name__, e)
        return False
    if n != _stats['docs']:
        log.info('BM25 corpus is stale: %d rows on disk vs %d in memory -- reloading', n, _stats['docs'])
        return True
    return False


def bm25_search(query: str, k: int = 20) -> list[dict]:
    """Lexical retrieval over the resident corpus. Always returns a list.

    An empty return has two very different meanings and they are now separated in the
    log (and programmatically via index_status()):
      WARNING keyword search UNAVAILABLE -> no corpus: empty table or unreadable DB
      INFO    0 matches                  -> corpus healthy, no document scored > 0
    """
    if not _loaded:
        _load()
    elif AUTO_REFRESH_SECONDS and _is_stale():
        _load(force=True)

    if _index is None or _ids is None:
        log.warning('bm25_search: keyword search UNAVAILABLE (%s) -- returning [] because there is no corpus, '
                    'not because nothing matched. Retrieval is running on vector search alone.',
                    _stats['error'] or 'index not loaded')
        return []

    tokens = query.lower().split()
    if not tokens:
        log.warning('bm25_search: query %r has no tokens -- returning []', query)
        return []

    t0      = time.perf_counter()
    scores  = _index.get_scores(tokens)
    top_idx = sorted(range(len(scores)), key=lambda i: -scores[i])[:k]
    hits    = [{'id': _ids[i], 'bm25_score': float(scores[i])} for i in top_idx if scores[i] > 0]

    if not hits:
        log.info('bm25_search: 0 matches for %r across %d docs -- corpus is healthy, nothing scored above 0',
                 query[:120], _stats['docs'])
    else:
        log.debug('bm25_search: %d hits in %.0f ms (top %.3f)', len(hits), (time.perf_counter() - t0) * 1000, hits[0]['bm25_score'])
    return hits
