import os
from groq import Groq
from dotenv import load_dotenv

from .vector_search import vector_search, fetch_by_ids
from .bm25_search   import bm25_search
from .reranker       import rerank

load_dotenv()

llm    = Groq(api_key=os.getenv('GROQ_API_KEY'))
MODEL_FAST = os.getenv('GROQ_MODEL_FAST', 'qwen/qwen3.8-27b')

# How many chunks reach the model. This is a context-volume decision, not a count:
# chunks are 256 tokens, so TOP_N=7 gave ~1,800 tokens -- far too little, and the model
# answered INSUFFICIENT_CONTEXT on in-scope questions whose answer was sitting in the
# context it did receive. The old value was calibrated when chunks were ~671 tokens.
TOP_N = int(os.getenv('RETRIEVAL_TOP_N', 18))

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
        model=MODEL_FAST,
        max_tokens=512,
        messages=[{'role': 'user', 'content': REWRITE_PROMPT.format(history=hist or 'none', question=question)}]
    )
    content = (resp.choices[0].message.content or '').strip()
    # Reasoning models can spend the whole budget on reasoning and return empty
    # content. Falling back to the raw question keeps retrieval working; returning
    # '' silently produced a meaningless embedding and zero BM25 scores.
    if not content:
        print(f'[rewrite] empty response from {MODEL_FAST}, using raw question')
        return question
    return content


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

    # Hydrate BM25-only hits. Previously this filtered to `if i in vec_map`,
    # which discarded every document BM25 found that the embedding search
    # missed -- exactly the identifier-shaped matches BM25 is here for.
    # Ids are normalised to str: Qdrant returns UUID objects, SQLite stores text.
    chunk_map = {str(h['id']): h for h in vec_hits}
    missing   = [i for i in fused_ids if i not in chunk_map]
    for c in fetch_by_ids(missing, collection):
        chunk_map[str(c['id'])] = c

    candidates = [chunk_map[i] for i in fused_ids if i in chunk_map]
    best_score = vec_hits[0]['score'] if vec_hits else 0.0
    top        = rerank(rewritten, candidates[:40], top_n=TOP_N) if candidates else []
    return top, best_score, rewritten