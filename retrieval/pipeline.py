import os, sqlite3
from groq import Groq
from qdrant_client import QdrantClient
from qdrant_client.models import Filter, FieldCondition, MatchValue
from dotenv import load_dotenv

from .vector_search import vector_search
from .bm25_search   import bm25_search
from .reranker       import rerank

load_dotenv()

llm    = Groq(api_key=os.getenv('GROQ_API_KEY'))
qdrant = QdrantClient(host=os.getenv('QDRANT_HOST', 'localhost'), port=int(os.getenv('QDRANT_PORT', 6333)))
db     = sqlite3.connect(os.getenv('DATABASE_PATH', './db/repomind.db'))

REWRITE_PROMPT = '''
Rewrite this question as a short search query optimised for finding
GitHub issues and PRs about design decisions and architectural rationale.
Return ONLY the rewritten query, nothing else.

History: {history}
Question: {question}
'''


def rewrite_query(question: str, history: list = []) -> str:
    hist = '\n'.join(f"{m['role']}: {m['content']}" for m in history[-4:])
    resp = llm.chat.completions.create(
        model='llama-3.3-70b-versatile',
        max_tokens=100,
        messages=[{'role': 'user', 'content': REWRITE_PROMPT.format(history=hist or 'none', question=question)}]
    )
    content = resp.choices[0].message.content
    return content.strip() if content else ""


def _fetch_by_number(collection: str, number: int, stype: str) -> list[dict]:
    try:
        results = qdrant.scroll(
            collection_name=collection,
            scroll_filter=Filter(must=[
                FieldCondition(key='number',      match=MatchValue(value=number)),
                FieldCondition(key='source_type', match=MatchValue(value=stype)),
            ]),
            limit=2, with_payload=True
        )
        points = results[0] if results and results[0] else []
        return [{'id': r.id, **r.payload} for r in points]
    except Exception:
        return []


def expand_with_links(chunks: list[dict], collection: str) -> list[dict]:
    seen     = {c['id'] for c in chunks}
    expanded = list(chunks)
    for chunk in chunks:
        if chunk.get('source_type') == 'code' and chunk.get('file_path'):
            rows = db.execute(
                'SELECT DISTINCT issue_number, pr_number FROM code_issue_links WHERE file_path=?',
                [chunk['file_path']]
            ).fetchall()
            for issue_num, pr_num in rows:
                for num, stype in [(issue_num, 'issue'), (pr_num, 'pr')]:
                    if not num:
                        continue
                    for lc in _fetch_by_number(collection, num, stype):
                        if lc['id'] not in seen:
                            expanded.append(lc)
                            seen.add(lc['id'])
    return expanded


def _rrf(vec_hits: list[dict], bm25_hits: list[dict], k: int = 60) -> list[str]:
    scores = {}
    for rank, h in enumerate(vec_hits):
        scores[h['id']] = scores.get(h['id'], 0) + 1 / (k + rank + 1)
    for rank, h in enumerate(bm25_hits):
        scores[h['id']] = scores.get(h['id'], 0) + 1 / (k + rank + 1)
    return sorted(scores, key=lambda x: -scores[x])


def retrieve(question: str, collection: str, history: list = []):
    rewritten  = rewrite_query(question, history)
    vec_hits   = vector_search(rewritten, collection, k=20)
    bm25_hits  = bm25_search(rewritten, k=20)
    
    if not vec_hits and not bm25_hits:
        return [], 0.0, rewritten
    
    fused_ids  = _rrf(vec_hits, bm25_hits)
    vec_map    = {h['id']: h for h in vec_hits}
    candidates = [vec_map[i] for i in fused_ids if i in vec_map]
    best_score = vec_hits[0]['score'] if vec_hits else 0.0
    top        = rerank(rewritten, candidates[:20], top_n=7) if candidates else []
    final      = expand_with_links(top, collection) if top else []
    return final, best_score, rewritten