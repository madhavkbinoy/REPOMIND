import os, sqlite3
from rank_bm25 import BM25Okapi
from dotenv import load_dotenv

load_dotenv()

_index:  BM25Okapi | None = None
_ids:    list[str]  | None = None
_loaded: bool              = False


def _load():
    global _index, _ids, _loaded
    if _loaded:
        return
    db     = sqlite3.connect(os.getenv('DATABASE_PATH', './db/repomind.db'))
    rows   = db.execute('SELECT chunk_id, text FROM bm25_index').fetchall()
    db.close()
    _ids   = [r[0] for r in rows]
    corpus = [r[1].lower().split() for r in rows]
    _index = BM25Okapi(corpus)
    _loaded = True
    print(f'BM25 index loaded: {len(rows)} docs')


def bm25_search(query: str, k: int = 20) -> list[dict]:
    _load()
    scores  = _index.get_scores(query.lower().split())
    top_idx = sorted(range(len(scores)), key=lambda i: -scores[i])[:k]
    return [{'id': _ids[i], 'bm25_score': float(scores[i])} for i in top_idx if scores[i] > 0]