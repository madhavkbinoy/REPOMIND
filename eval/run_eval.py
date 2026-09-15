"""
Evaluate retrieval and refusal behaviour, and emit the results table.

What gets measured
------------------
retrieval   Recall@k and MRR against the golden set, across ablation configurations.
            A hit means a chunk from the ground-truth document appears in the results.

refusal     On the negative set, how often the system correctly declines. This is the
            headline claim -- "it refuses rather than fabricating" -- so it needs a
            number, not three anecdotes.

threshold   A sweep of CONFIDENCE_THRESHOLD showing the coverage / false-answer
            tradeoff, so 0.40 is a choice rather than a constant.

Query rewrites are cached: the same question produces the same rewrite in every
configuration, so paying for it once instead of once per config cuts LLM calls by ~5x.

Usage
-----
    python eval/run_eval.py retrieval    # ablation table
    python eval/run_eval.py refusal      # refusal accuracy on the negative set
    python eval/run_eval.py threshold    # threshold sweep
    python eval/run_eval.py all
"""
import json, os, sys, time
from pathlib import Path

from dotenv import load_dotenv

# Runnable as `python eval/run_eval.py` from the repo root, which otherwise leaves
# the project packages off sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

load_dotenv()

from retrieval.vector_search import vector_search, fetch_by_ids
from retrieval.bm25_search   import bm25_search
from retrieval.reranker      import rerank
from retrieval.pipeline      import rewrite_query, _rrf, retrieve

COLLECTION = 'kubernetes_kubernetes'
EVAL_DIR   = Path('./eval')
GOLDEN     = EVAL_DIR / 'golden.jsonl'
NEGATIVE   = EVAL_DIR / 'negatives.jsonl'
CACHE      = EVAL_DIR / '.rewrite_cache.json'
# Auto-generated tables go here. eval/results.md is the CURATED analysis and is
# hand-written -- the harness used to overwrite it, destroying the write-up each run.
RESULTS    = EVAL_DIR / 'metrics.md'

K = 10


# --------------------------------------------------------------------------

def load(path: Path) -> list[dict]:
    if not path.exists():
        sys.exit(f'Missing {path} -- run eval/build_golden.py first.')
    return [json.loads(l) for l in path.open() if l.strip()]


_cache: dict = json.loads(CACHE.read_text()) if CACHE.exists() else {}


def cached_rewrite(q: str) -> str:
    if q not in _cache:
        _cache[q] = rewrite_query(q)
        CACHE.write_text(json.dumps(_cache, indent=1))
    return _cache[q]


def retrieve_variant(question: str, mode: str, rewrite: bool = True) -> list[dict]:
    """One retrieval configuration. Mirrors pipeline.retrieve() stage for stage."""
    q = cached_rewrite(question) if rewrite else question

    vec  = vector_search(q, COLLECTION, k=20) if mode != 'bm25'   else []
    bm25 = bm25_search(q, k=20)               if mode != 'vector' else []

    if mode == 'vector':
        cands = vec
    elif mode == 'bm25':
        by_id = {str(h['id']): h for h in fetch_by_ids([h['id'] for h in bm25], COLLECTION)}
        cands = [by_id[str(h['id'])] for h in bm25 if str(h['id']) in by_id]
    else:
        fused     = _rrf(vec, bm25)
        chunk_map = {str(h['id']): h for h in vec}
        missing   = [i for i in fused if i not in chunk_map]
        for c in fetch_by_ids(missing, COLLECTION):
            chunk_map[str(c['id'])] = c
        cands = [chunk_map[i] for i in fused if i in chunk_map]

    if mode == 'rrf+rerank':
        cands = rerank(q, cands[:20], top_n=K)
    return cands[:K]


def score(rows: list[dict], mode: str, rewrite: bool = True) -> dict:
    """Recall@K and MRR. A hit = a chunk from the ground-truth document."""
    hits, rr = 0, 0.0
    for r in rows:
        got = retrieve_variant(r['question'], mode, rewrite)
        rank = None
        for i, c in enumerate(got, 1):
            if c.get('source_type') == r['source_type'] and c.get('number') == r['number']:
                rank = i
                break
        if rank:
            hits += 1
            rr   += 1.0 / rank
    n = len(rows) or 1
    return {'recall': hits / n, 'mrr': rr / n, 'n': len(rows)}


# --------------------------------------------------------------------------

CONFIGS = [
    ('Vector only',            'vector',     True),
    ('BM25 only',              'bm25',       True),
    ('RRF hybrid',             'rrf',        True),
    ('RRF + cross-encoder',    'rrf+rerank', True),
    ('RRF + rerank, no rewrite', 'rrf+rerank', False),
]


def run_retrieval() -> str:
    rows = load(GOLDEN)
    print(f'Golden set: {len(rows)} questions\n')
    out = [f'### Retrieval (n={len(rows)}, k={K})\n',
           '| Configuration | Recall@10 | MRR |', '|---|---:|---:|']
    for label, mode, rw in CONFIGS:
        t0 = time.time()
        s  = score(rows, mode, rw)
        print(f'  {label:28} recall={s["recall"]:.3f}  mrr={s["mrr"]:.3f}  ({time.time()-t0:.0f}s)')
        out.append(f'| {label} | {s["recall"]:.3f} | {s["mrr"]:.3f} |')
    return '\n'.join(out) + '\n'


def run_refusal() -> str:
    from generation.generator import should_answer
    neg = load(NEGATIVE)
    pos = load(GOLDEN)

    def gate(q):
        # Must call the PRODUCTION path. This previously used retrieve_variant and took
        # max(score) over the reranked chunks, while retrieve() gates on vec_hits[0]
        # ['score'] -- the top vector hit, whether or not it survives reranking. The two
        # differ on every question (eval ran ~0.05-0.15 high), so a threshold tuned here
        # was tuned against a quantity the system never evaluates.
        chunks, best, _ = retrieve(q, COLLECTION)
        return should_answer(best, chunks), best

    refused = sum(1 for r in neg if not gate(r['question'])[0])
    answered = sum(1 for r in pos if gate(r['question'])[0])
    print(f'  negative set: refused {refused}/{len(neg)}')
    print(f'  golden set:   answered {answered}/{len(pos)}')

    return (f'\n### Refusal behaviour\n\n'
            f'| Set | n | Outcome |\n|---|---:|---|\n'
            f'| Out-of-scope | {len(neg)} | **{refused/len(neg):.1%}** correctly refused |\n'
            f'| In-scope | {len(pos)} | **{answered/len(pos):.1%}** answered |\n\n'
            f'Refusal here is the retrieval gate only. The model can also decline with '
            f'`INSUFFICIENT_CONTEXT` after generation, so the end-to-end refusal rate is '
            f'at least this high.\n')


def run_threshold() -> str:
    neg, pos = load(NEGATIVE), load(GOLDEN)

    def best_of(q):
        chunks, best, _ = retrieve(q, COLLECTION)
        return best, bool(chunks)

    pos_s = [best_of(r['question']) for r in pos]
    neg_s = [best_of(r['question']) for r in neg]

    out = ['\n### Confidence threshold sweep\n',
           '| Threshold | In-scope answered | Out-of-scope leaked |', '|---:|---:|---:|']
    for t in [round(0.20 + 0.05 * i, 2) for i in range(13)]:
        cov  = sum(1 for s, has in pos_s if has and s >= t) / (len(pos_s) or 1)
        leak = sum(1 for s, has in neg_s if has and s >= t) / (len(neg_s) or 1)
        mark = '  ← current' if abs(t - float(os.getenv('CONFIDENCE_THRESHOLD', 0.55))) < 1e-9 else ''
        print(f'  t={t:.2f}  coverage={cov:.1%}  leak={leak:.1%}{mark}')
        out.append(f'| {t:.2f} | {cov:.1%} | {leak:.1%} |{mark}')
    out.append('\n"Leaked" means an out-of-scope question cleared the gate and reached the '
               'model — where `INSUFFICIENT_CONTEXT` is the remaining defence.\n')
    return '\n'.join(out)


def run_bias() -> str:
    """Test whether BM25's advantage is real or an artifact of question construction.

    Golden questions were drafted from their source documents, so they inherit vocabulary
    from them -- which flatters keyword search and penalises paraphrasing rewrites. If that
    is the whole story, BM25's lead should collapse on the questions with LOW overlap. If
    the lead survives there, the effect is real.
    """
    rows = [r for r in load(GOLDEN) if 'overlap' in r]
    if not rows:
        return '\n(no overlap data -- run `build_golden.py overlap`)\n'

    rows.sort(key=lambda r: r['overlap'])
    mid  = len(rows) // 2
    strata = [('Low overlap', rows[:mid]), ('High overlap', rows[mid:])]

    out = ['\n### Is BM25 winning, or is the question set leaking?\n',
           '| Stratum | n | mean overlap | Vector R@10 | BM25 R@10 | BM25 lead |',
           '|---|---:|---:|---:|---:|---:|']
    leads = {}
    for name, group in strata:
        mo = sum(r['overlap'] for r in group) / len(group)
        v  = score(group, 'vector')['recall']
        b  = score(group, 'bm25')['recall']
        leads[name] = b - v
        print(f'  {name:14} n={len(group):2}  overlap={mo:.2f}  vector={v:.3f}  bm25={b:.3f}  lead={b-v:+.3f}')
        out.append(f'| {name} | {len(group)} | {mo:.2f} | {v:.3f} | {b:.3f} | {b-v:+.3f} |')

    lo, hi = leads['Low overlap'], leads['High overlap']
    out.append('')
    if hi > lo + 0.10:
        out.append(f'BM25\'s lead is **{hi:+.3f} on high-overlap questions but {lo:+.3f} on low-overlap '
                   f'ones**. Much of the advantage is an artifact of drafting questions from the source '
                   f'documents, not a property of the retriever. Treat the headline BM25 numbers as an '
                   f'upper bound.')
    elif lo > 0:
        out.append(f'BM25 leads by {lo:+.3f} even on the low-overlap half, where the construction bias '
                   f'is weakest. The advantage survives the obvious confound and looks real.')
    else:
        out.append(f'BM25\'s lead does not survive on low-overlap questions ({lo:+.3f}); it is driven by '
                   f'vocabulary shared with the source document.')
    return '\n'.join(out) + '\n'


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'all'
    EVAL_DIR.mkdir(exist_ok=True)
    parts = []
    if cmd in ('all', 'retrieval'):  parts.append(run_retrieval())
    if cmd in ('all', 'refusal'):    parts.append(run_refusal())
    if cmd in ('all', 'threshold'):  parts.append(run_threshold())
    if cmd in ('all', 'bias'):       parts.append(run_bias())

    import sqlite3
    try:
        n = sqlite3.connect(os.getenv('DATABASE_PATH', './db/repomind.db')).execute(
            'SELECT COUNT(*) FROM chunks').fetchone()[0]
    except Exception:
        n = 0

    out = RESULTS if cmd == 'all' else RESULTS.with_name(f'metrics_{cmd}.md')
    out.write_text('# Evaluation results\n\n'
                   f'Generated by `eval/run_eval.py {cmd}`. Corpus: {n:,} chunks.\n\n'
                   + '\n'.join(parts))
    print(f'\nWritten to {out}')
    if cmd != 'all':
        print(f'(partial run -- {RESULTS.name} left intact; rerun `all` to refresh it)')


if __name__ == '__main__':
    main()
