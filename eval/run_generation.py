"""
Measure what happens after retrieval: citations, and end-to-end refusal.

Retrieval metrics (run_eval.py) stop at "did the right document come back". These are
the claims the product actually makes:

  citation grounding   Of the (#N) citations the model emits, how many refer to a
                       document that was actually in its context? A citation to
                       something never retrieved is a hard hallucination -- the model
                       invented a source. This is objective and needs no judgement.

  citation support     Of the grounded citations, how many are attached to a claim the
                       cited text actually supports? This needs judgement; the existing
                       verify_citations() LLM judge is reused, and a sample is written
                       out for human scoring, because grading your own grader is circular.

  end-to-end refusal   The gate is only the first of three layers. This measures what a
                       user actually sees, including the model's own INSUFFICIENT_CONTEXT.

  verifier health      How often verify_citations() fails to parse and silently returns
                       an unverified answer. The system is designed to fail open, so this
                       number is the size of the hole in the guarantee.

Every generation is cached to generations.jsonl, so a rerun costs nothing and the file
doubles as the artifact a human reviews.

Usage
-----
    python eval/run_generation.py run      # generate + score (resumable)
    python eval/run_generation.py report   # metrics from the cache
    python eval/run_generation.py sample   # write a hand-scoring worksheet
"""
import json, os, re, sys, time
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
load_dotenv()

import retrieval.pipeline as P
from retrieval.pipeline   import retrieve
from generation.generator import (should_answer, fallback_response, build_messages,
                                  finalize, client, MODEL, MAX_ANSWER_TOKENS, THRESHOLD)

COLLECTION = 'kubernetes_kubernetes'
EVAL_DIR   = Path(__file__).resolve().parent
GOLDEN     = EVAL_DIR / 'golden.jsonl'
NEGATIVE   = EVAL_DIR / 'negatives.jsonl'
CACHE      = EVAL_DIR / 'generations.jsonl'
WORKSHEET  = EVAL_DIR / 'citation_worksheet.md'

CITATION = re.compile(r'#(\d+)')


def current_config() -> dict:
    """Stamped onto every cached row. Results generated under different retrieval or
    model settings are not comparable, and a cache that silently mixes them produces
    numbers that describe no system that ever existed."""
    import hashlib
    from generation.generator import MODEL, MODEL_VERIFY, THRESHOLD, format_context
    from generation.prompts import SYSTEM_PROMPT

    # The prompt and the context format decide what the model emits as much as the
    # model choice does -- changing the citation label format invalidates every cached
    # answer. Fingerprint both so a cache can never silently span two formats.
    probe = format_context([{'text': 'x', 'url': 'u', 'number': 1, 'source_type': 'issue'}])
    fp = hashlib.sha256((SYSTEM_PROMPT + probe).encode()).hexdigest()[:10]

    return {'top_n': P.TOP_N, 'threshold': THRESHOLD, 'model': MODEL,
            'verify_model': MODEL_VERIFY, 'prompt_fp': fp}


def load(p: Path) -> list[dict]:
    if not p.exists():
        sys.exit(f'Missing {p}')
    return [json.loads(l) for l in p.open() if l.strip()]


def answer_once(question: str) -> dict:
    """Full pipeline for one question, recording which layer refused."""
    chunks, best_score, _ = retrieve(question, COLLECTION)

    if not should_answer(best_score, chunks):
        # should_answer() is `bool(chunks) and best_score >= THRESHOLD`, so it collapses
        # two distinct refusals into one boolean. Attribute them separately or the
        # per-layer accounting is wrong: a question scoring 0.677 with zero surviving
        # chunks was refused by the CROSS-ENCODER (every candidate scored <= 0), not by
        # the confidence gate, which would happily have passed it.
        layer = 'gate' if best_score < THRESHOLD else 'reranker'
        out = fallback_response(best_score)
        out.update(refused_by=layer, best_score=best_score,
                   n_chunks=len(chunks), context_numbers=[])
        return out

    resp = client.chat.completions.create(
        model=MODEL, max_tokens=MAX_ANSWER_TOKENS,
        messages=build_messages(question, chunks, 'kubernetes/kubernetes', []))
    raw = resp.choices[0].message.content or ''

    result = finalize(raw, chunks, best_score)
    result['refused_by']      = 'model' if result.get('is_fallback') else None
    result['n_chunks']        = len(chunks)
    result['raw_answer']      = raw
    # What the model was actually shown -- the denominator for grounding.
    result['context_numbers'] = sorted({c.get('number') for c in chunks
                                        if c.get('number') is not None})
    return result


def run():
    cfg = current_config()
    print(f'Config: {cfg}\n')

    done, stale = set(), 0
    if CACHE.exists():
        for l in CACHE.open():
            if not l.strip():
                continue
            r = json.loads(l)
            if r.get('config') == cfg:
                done.add(r['question'])
            else:
                stale += 1
        print(f'Resuming: {len(done)} usable, {stale} from a different config (ignored)')

    rows = ([{'question': r['question'], 'set': 'golden', 'number': r['number'],
              'source_type': r['source_type']} for r in load(GOLDEN)]
            + [{'question': r['question'], 'set': 'negative'} for r in load(NEGATIVE)])

    todo = [r for r in rows if r['question'] not in done]
    print(f'{len(todo)} to generate ({len(rows)} total)\n')

    with CACHE.open('a') as f:
        for i, r in enumerate(todo, 1):
            t0 = time.time()
            try:
                res = answer_once(r['question'])
            except Exception as e:
                print(f'  [{i}/{len(todo)}] FAILED: {str(e)[:90]}')
                continue

            # Citations must be counted from what the MODEL emitted, not from the
            # verified answer -- verify_citations() strips unsupported ones and replaces
            # them with [UNVERIFIED], so counting the final text conflates "the model
            # failed to cite" with "the verifier rejected the citation". Those are
            # opposite failures and need separate numbers.
            raw      = res.get('raw_answer') or res.get('answer') or ''
            cited    = sorted({int(n) for n in CITATION.findall(raw)})
            in_ctx   = set(res['context_numbers'])
            grounded = [c for c in cited if c in in_ctx]
            survived = sorted({int(n) for n in CITATION.findall(res.get('answer') or '')})

            rec = {**r, 'config': cfg,
                   'answer':            res.get('answer', ''),
                   'is_fallback':       res.get('is_fallback', False),
                   'refused_by':        res.get('refused_by'),
                   'best_score':        round(res.get('best_score', 0), 4),
                   'n_chunks':          res.get('n_chunks', 0),
                   'cited':             cited,
                   'grounded':          grounded,
                   'survived':          survived,
                   'stripped':          sorted(set(cited) - set(survived)),
                   'context_numbers':   res['context_numbers'],
                   'citations_valid':   res.get('citations_valid'),
                   'invalid_citations': res.get('invalid_citations', []),
                   'verification_ran':  res.get('verification_ran'),
                   'sources':           res.get('sources', [])}
            f.write(json.dumps(rec) + '\n')
            f.flush()

            tag = res.get('refused_by') or 'answered'
            print(f'  [{i}/{len(todo)}] {r["set"]:8} {tag:8} cited={len(cited):2} '
                  f'grounded={len(grounded):2} kept={len(survived):2} ({time.time()-t0:.0f}s)')

    report()


def report():
    cfg  = current_config()
    all_ = [json.loads(l) for l in CACHE.open() if l.strip()]
    rows = [r for r in all_ if r.get('config') == cfg]
    if len(rows) < len(all_):
        print(f'({len(all_) - len(rows)} rows from other configs excluded)')
    gold = [r for r in rows if r['set'] == 'golden']
    neg  = [r for r in rows if r['set'] == 'negative']

    # --- citation grounding: did the model cite things it was actually given?
    answered   = [r for r in gold if not r['is_fallback']]
    total_cit  = sum(len(r['cited']) for r in answered)
    total_grnd = sum(len(r['grounded']) for r in answered)
    with_cit   = [r for r in answered if r['cited']]
    perfect    = [r for r in with_cit if len(r['grounded']) == len(r['cited'])]
    uncited    = [r for r in answered if not r['cited']]

    # --- end-to-end refusal on the negative set
    by_gate  = sum(1 for r in neg if r['refused_by'] == 'gate')
    by_rerank = sum(1 for r in neg if r['refused_by'] == 'reranker')
    by_model = sum(1 for r in neg if r['refused_by'] == 'model')
    leaked   = [r for r in neg if not r['is_fallback']]

    # --- verifier health
    ran   = [r for r in answered if r['verification_ran'] is True]
    failed = [r for r in answered if r['verification_ran'] is False]
    flagged = [r for r in answered if r['citations_valid'] is False]

    L = []
    L.append('### Citation grounding (golden set)\n')
    L.append('Objective check: does each cited `#N` refer to a document that was actually in the')
    L.append("model's context? A citation to something never retrieved is an invented source.\n")
    L.append('| Metric | Value |')
    L.append('|---|---:|')
    L.append(f'| Answers generated | {len(answered)} / {len(gold)} |')
    L.append(f'| Citations emitted | {total_cit} |')
    L.append(f'| **Grounded in retrieved context** | **{total_grnd}/{total_cit}'
             f' = {100*total_grnd/max(total_cit,1):.1f}%** |')
    L.append(f'| Answers where every citation is grounded | {len(perfect)}/{len(with_cit)}'
             f' = {100*len(perfect)/max(len(with_cit),1):.1f}% |')
    L.append(f'| Answers with no citation at all | {len(uncited)} |')

    emitted  = sum(len(r.get('cited', [])) for r in answered)
    kept     = sum(len(r.get('survived', [])) for r in answered)
    L.append(f'| Citations surviving verification | {kept}/{emitted}'
             f' = {100*kept/max(emitted,1):.1f}% |')
    L.append('')
    L.append('These are three separate failure modes and conflating them hides the real one:')
    L.append('the model failing to cite, citing something it was never shown (grounding), and')
    L.append('citing a real source that does not support the claim (verification strips it).')

    L.append('\n### End-to-end refusal (negative set)\n')
    L.append('| Layer | Refusals |')
    L.append('|---|---:|')
    L.append(f'| 1. Confidence gate (score < threshold) | {by_gate}/{len(neg)} |')
    L.append(f'| 2. Cross-encoder (no chunk scored > 0) | {by_rerank}/{len(neg)} |')
    L.append(f'| 3. Model (`INSUFFICIENT_CONTEXT`) | {by_model}/{len(neg)} |')
    L.append(f'| **Total refused** | **{by_gate+by_rerank+by_model}/{len(neg)}'
             f' = {100*(by_gate+by_rerank+by_model)/max(len(neg),1):.1f}%** |')
    L.append(f'| Answered anyway | {len(leaked)} |')
    if leaked:
        L.append('')
        for r in leaked[:5]:
            L.append(f'- *"{r["question"][:80]}"* — score {r["best_score"]:.3f}, '
                     f'{len(r["cited"])} citations')

    L.append('\n### Verifier health\n')
    L.append('The verifier fails open by design: an unparseable response returns the answer')
    L.append('unverified rather than blocking it. This is the size of that hole.\n')
    L.append('| Metric | Value |')
    L.append('|---|---:|')
    L.append(f'| Verification ran successfully | {len(ran)}/{len(answered)}'
             f' = {100*len(ran)/max(len(answered),1):.1f}% |')
    L.append(f'| **Failed to parse (answer returned unverified)** | **{len(failed)}'
             f' = {100*len(failed)/max(len(answered),1):.1f}%** |')
    L.append(f'| Answers where the verifier rejected a citation | {len(flagged)} |')

    out = '\n'.join(L)
    print('\n' + out)
    (EVAL_DIR / 'citation_results.md').write_text(
        '# Citation and refusal results\n\n'
        f'From `eval/run_generation.py` over {len(gold)} in-scope and {len(neg)} '
        f'out-of-scope questions.\n\n' + out + '\n')
    print(f'\nWritten to {EVAL_DIR / "citation_results.md"}')


def sample(n: int = 15):
    """Worksheet for the part a machine should not grade: does the cited text actually
    support the claim? The LLM verifier has an opinion; a human has to check it."""
    rows = [json.loads(l) for l in CACHE.open() if l.strip()]
    answered = [r for r in rows if r['set'] == 'golden' and not r['is_fallback'] and r['cited']]

    L = ['# Citation support worksheet\n',
         'Grounding (is the cited doc in context?) is measured automatically.',
         'This is the part that needs a person: **does the cited source actually support',
         'the sentence it is attached to?**\n',
         'For each citation mark `SUPPORTS`, `UNRELATED`, or `OVERSTATED`.\n', '---\n']
    for i, r in enumerate(answered[:n], 1):
        L.append(f'## {i}. {r["question"]}\n')
        L.append(f'Ground truth: {r["source_type"]} #{r["number"]} · score {r["best_score"]:.3f}\n')
        L.append(f'**Answer:**\n\n{r["answer"]}\n')
        L.append(f'Cited: {r["cited"]}  ·  grounded in context: {r["grounded"]}')
        if r['invalid_citations']:
            L.append(f'\nVerifier flagged: {r["invalid_citations"]}')
        L.append('\n| Citation | Verdict | Note |')
        L.append('|---|---|---|')
        for c in r['cited']:
            L.append(f'| #{c} |  |  |')
        L.append('\n---\n')
    WORKSHEET.write_text('\n'.join(L))
    print(f'Wrote {WORKSHEET} — {min(n, len(answered))} answers to score by hand')


if __name__ == '__main__':
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'run'
    {'run': run, 'report': report, 'sample': sample}[cmd]()
