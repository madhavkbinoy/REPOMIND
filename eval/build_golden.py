"""
Build the golden set: questions whose answer is known to live in a specific document.

Method
------
Pick source documents with substantial discussion (long threads argue about something;
short ones report and close). Ask the LLM to write a question answerable *only* from
that document, and record the document's number as ground truth.

The LLM writes the question; it does not decide the answer. Ground truth is the
document ID, which is a fact, not a judgement. That keeps the labels trustworthy
even though generation is automated.

Every generated question still needs a human pass -- `review` prints them in a form
that makes rejecting bad ones fast. A golden set you have not read is not a golden set.

Usage
-----
    python eval/build_golden.py generate    # draft questions -> eval/golden_raw.jsonl
    python eval/build_golden.py review      # print for human checking
    python eval/build_golden.py accept      # promote reviewed -> eval/golden.jsonl
    python eval/build_golden.py negatives   # write the out-of-scope set
"""
import json, os, random, re, sys
from pathlib import Path

from dotenv import load_dotenv
from groq import Groq

load_dotenv()

RAW      = Path('./data/raw/kubernetes_kubernetes')
OUT_DIR  = Path('./eval')
RAW_OUT  = OUT_DIR / 'golden_raw.jsonl'
GOLDEN   = OUT_DIR / 'golden.jsonl'
NEGATIVE = OUT_DIR / 'negatives.jsonl'

# A stalled request with no timeout hangs the whole run -- observed blocking for
# minutes on a single PR. Bound it and let the retry logic handle transients.
client = Groq(api_key=os.getenv('GROQ_API_KEY'), timeout=45.0, max_retries=2)
MODEL  = os.getenv('GROQ_MODEL', 'openai/gpt-oss-120b')

N_ISSUES, N_PRS, N_KEPS = 20, 20, 10
MIN_COMMENTS = 4
SEED = 20260914

GEN_PROMPT = '''Below is a GitHub {kind} from the Kubernetes kubelet subsystem.

Write ONE question that a software engineer would genuinely ask about kubelet's design,
and whose answer is contained in this document.

Hard rules:
- NEVER refer to the document itself. "Why does this PR...", "Why does the proposal...",
  "Why does the issue..." are all INVALID. The reader has not seen this document and does
  not know it exists. Ask about the SUBJECT, not about the text.
- Do NOT mention the issue/PR/KEP number, the title, or any author's name.
- Ask about design rationale ("why was X done this way", "what problem does Y solve"),
  not trivia, dates, version numbers, or dependency bumps.
- Phrase it in YOUR OWN WORDS. Avoid reusing distinctive phrases, variable names, or
  sentence fragments copied verbatim from the text -- except where a technical term has
  no natural synonym (e.g. "kubelet", "cgroup v2", "PLEG").
- One sentence. Return ONLY the question, no preamble.

Good:   "Why does kubelet refuse to admit new pods once a node reports disk pressure?"
Bad:    "Why does this PR add a disk pressure check to the admission handler?"

---
{text}
---'''


def lexical_overlap(question: str, doc_text: str) -> float:
    """Fraction of the question's content words that also appear in the source document.

    The golden set is LLM-drafted FROM each document, so questions inherit vocabulary
    from them -- which flatters keyword search and penalises query rewriting. Recording
    the overlap per question turns that confound into something measurable: the eval can
    then report retrieval quality split by overlap, instead of merely disclaiming it.
    """
    stop = {'the','a','an','is','are','was','were','be','been','why','how','what','does',
            'do','did','of','for','to','in','on','at','by','with','and','or','not','it',
            'this','that','when','which','from','as','its','their','there','than','then'}
    qw = {w for w in re.findall(r'[a-z0-9_.-]+', question.lower()) if w not in stop and len(w) > 2}
    if not qw:
        return 0.0
    dw = set(re.findall(r'[a-z0-9_.-]+', doc_text.lower()))
    return len(qw & dw) / len(qw)


def _txt(d: dict, limit: int = 6000) -> str:
    """Flatten a scraped document into the text the question should be answerable from."""
    parts = [d.get('title') or '', d.get('body') or '']
    for c in (d.get('comments', {}) or {}).get('nodes', []) or []:
        parts.append(c.get('body') or '')
    for r in (d.get('reviews', {}) or {}).get('nodes', []) or []:
        parts.append(r.get('body') or '')
    for sec in d.get('sections', []) or []:
        parts.append(f"{sec['heading']}\n{sec['body']}")
    return '\n\n'.join(p for p in parts if p)[:limit]


def _n_comments(d: dict) -> int:
    return len((d.get('comments', {}) or {}).get('nodes', []) or [])


def generate():
    random.seed(SEED)
    OUT_DIR.mkdir(exist_ok=True)
    picks = []

    for kind, folder, n in [('issue', 'issues', N_ISSUES), ('pull request', 'prs', N_PRS)]:
        docs = []
        for p in (RAW / folder).glob('*.json'):
            d = json.loads(p.read_text())
            if _n_comments(d) >= MIN_COMMENTS and (d.get('body') or '').strip():
                docs.append(d)
        random.shuffle(docs)
        # Prefer the most-discussed: long threads are where rationale actually lives.
        docs.sort(key=_n_comments, reverse=True)
        picks += [(kind, folder.rstrip('s'), d) for d in docs[:n]]

    keps = [json.loads(p.read_text()) for p in (RAW / 'keps').glob('*.json')]
    keps = [k for k in keps if any(s['heading'].lower().startswith('alternativ')
                                   for s in k['sections'])]
    random.shuffle(keps)
    picks += [('enhancement proposal', 'kep', k) for k in keps[:N_KEPS]]

    done = set()
    if RAW_OUT.exists():
        for l in RAW_OUT.open():
            r = json.loads(l)
            done.add((r['source_type'], r['number']))
        print(f'Resuming: {len(done)} already drafted')

    print(f'Drafting up to {len(picks)} questions...')
    with RAW_OUT.open('a') as f:
        for i, (kind, stype, doc) in enumerate(picks, 1):
            num_ = doc.get('number') or doc.get('kep_number')
            if (stype, num_) in done:
                continue
            text = _txt(doc)
            if len(text) < 400:
                continue
            try:
                resp = client.chat.completions.create(
                    model=MODEL, max_tokens=2048,
                    messages=[{'role': 'user',
                               'content': GEN_PROMPT.format(kind=kind, text=text)}])
                q = (resp.choices[0].message.content or '').strip().strip('"')
            except Exception as e:
                print(f'  {i}: generation failed: {e}')
                continue
            if not q or len(q) < 15:
                continue
            num = doc.get('number') or doc.get('kep_number')
            f.write(json.dumps({
                'question':     q,
                'source_type':  stype,
                'number':       num,
                'title':        doc.get('title'),
                'url':          doc.get('url') or
                                f'https://github.com/kubernetes/kubernetes/issues/{num}',
                'n_comments':   _n_comments(doc),
                'overlap':      round(lexical_overlap(q, text), 3),
                'accepted':     None,
            }) + '\n')
            print(f'  {i}/{len(picks)} {stype} #{num}: {q[:74]}')
    print(f'\nWrote {RAW_OUT}. Now run: review')


SELF_REF = re.compile(
    r'\b(this|the)\s+(issue|pr|pull request|proposal|kep|document|thread|change|patch)\b', re.I)
NUMBERED = re.compile(r'#\d+|KEP-\d+')


def defects(q: str) -> list[str]:
    """Mechanical checks a question must pass to be usable as ground truth."""
    d = []
    if SELF_REF.search(q):           d.append('self-reference')
    if NUMBERED.search(q):           d.append('cites-number')
    if len(q.split()) > 45:          d.append('too-long')
    if not q.rstrip().endswith('?'): d.append('not-a-question')
    return d


def fix():
    """Regenerate questions that fail the mechanical checks, in place.

    A self-referential question ("Why does this PR...") cannot be asked by someone who
    has not already found the answer, so it measures nothing. These are regenerated
    against the stricter prompt rather than dropped, to keep the set size stable.
    """
    rows = [json.loads(l) for l in RAW_OUT.open()]
    bad  = [(i, r) for i, r in enumerate(rows) if defects(r['question'])]
    print(f'{len(bad)}/{len(rows)} questions need regenerating')
    if not bad:
        return

    docs = {}
    for folder, stype in [('issues', 'issue'), ('prs', 'pr'), ('keps', 'kep')]:
        for path in (RAW / folder).glob('*.json'):
            d = json.loads(path.read_text())
            docs[(stype, d.get('number') or d.get('kep_number'))] = d

    kinds = {'issue': 'issue', 'pr': 'pull request', 'kep': 'enhancement proposal'}
    for i, r in bad:
        doc = docs.get((r['source_type'], r['number']))
        if not doc:
            print(f"  [{i}] source doc missing, leaving as-is")
            continue
        text = _txt(doc)
        best = None
        for attempt in range(3):
            try:
                resp = client.chat.completions.create(
                    model=MODEL, max_tokens=2048,
                    messages=[{'role': 'user', 'content': GEN_PROMPT.format(
                        kind=kinds[r['source_type']], text=text)}])
                q = (resp.choices[0].message.content or '').strip().strip('"')
            except Exception as e:
                print(f'  [{i}] failed: {e}')
                break
            if q and not defects(q):
                best = q
                break
        if best:
            rows[i]['question'] = best
            rows[i]['overlap']  = round(lexical_overlap(best, text), 3)
            rows[i]['regenerated'] = True
            print(f'  [{i}] {r["source_type"]} #{r["number"]}: {best[:88]}')
        else:
            rows[i]['accepted'] = False
            print(f'  [{i}] {r["source_type"]} #{r["number"]}: still defective -> rejected')

    with RAW_OUT.open('w') as f:
        for r in rows:
            f.write(json.dumps(r) + '\n')
    print(f'\nRewrote {RAW_OUT}. Now run: accept')


def backfill_overlap():
    """Add the overlap field to rows drafted before the metric existed."""
    rows = [json.loads(l) for l in RAW_OUT.open()]
    docs = {}
    for folder, stype in [('issues', 'issue'), ('prs', 'pr'), ('keps', 'kep')]:
        for path in (RAW / folder).glob('*.json'):
            d = json.loads(path.read_text())
            docs[(stype, d.get('number') or d.get('kep_number'))] = d
    n = 0
    for r in rows:
        if 'overlap' in r:
            continue
        doc = docs.get((r['source_type'], r['number']))
        if doc:
            r['overlap'] = round(lexical_overlap(r['question'], _txt(doc)), 3)
            n += 1
    with RAW_OUT.open('w') as f:
        for r in rows:
            f.write(json.dumps(r) + '\n')
    print(f'backfilled overlap on {n} rows')


def review():
    if not RAW_OUT.exists():
        sys.exit('No golden_raw.jsonl -- run `generate` first.')
    rows = [json.loads(l) for l in RAW_OUT.open()]
    print(f'{len(rows)} drafted questions. Reject any that:')
    print('  - name the issue/PR explicitly, or could only be asked by someone who read it')
    print('  - ask about trivia (dates, who said what) rather than design rationale')
    print('  - are answerable from general Kubernetes knowledge without this document\n')
    for i, r in enumerate(rows, 1):
        print(f'[{i:>2}] {r["source_type"]} #{r["number"]}  ({r["n_comments"]} comments)')
        print(f'     Q: {r["question"]}')
        print(f'     {r["url"]}\n')
    print(f'Edit "accepted": true/false in {RAW_OUT}, then run: accept')


def accept():
    rows = [json.loads(l) for l in RAW_OUT.open()]
    # Unreviewed rows default to accepted so the harness is runnable immediately,
    # but they are counted separately -- an unreviewed set is a weaker claim.
    kept = [r for r in rows if r.get('accepted') is not False]
    unreviewed = sum(1 for r in kept if r.get('accepted') is None)
    with GOLDEN.open('w') as f:
        for r in kept:
            f.write(json.dumps(r) + '\n')
    print(f'{GOLDEN}: {len(kept)} questions ({unreviewed} not yet human-reviewed)')


NEGATIVES = [
    "How does the etcd raft implementation handle leader election?",
    "What algorithm does kube-scheduler use for bin packing?",
    "Why does the API server use etcd instead of a relational database?",
    "How does kube-proxy implement iptables rules for services?",
    "What is the reasoning behind the CRD validation schema design?",
    "How does the cloud controller manager handle node lifecycle?",
    "Why was the in-tree cloud provider code deprecated?",
    "How does kubeadm bootstrap a control plane?",
    "What consistency guarantees does the watch cache provide?",
    "How does the scheduler framework handle plugin ordering?",
    "Why does Kubernetes use a declarative API instead of imperative commands?",
    "How is leader election implemented in controller-manager?",
    "What is the design rationale behind admission webhooks?",
    "How does the garbage collector determine owner references?",
    "Why does the HPA use a stabilization window?",
    "How does CNI plugin chaining work?",
    "What was the reasoning for removing dockershim from the scheduler?",
    "How does the service mesh sidecar injection work in Istio?",
    "How do I make sourdough bread?",
    "What is the capital of France?",
    "Write me a Python function to reverse a linked list.",
    "How does PostgreSQL implement MVCC?",
]


def negatives():
    OUT_DIR.mkdir(exist_ok=True)
    with NEGATIVE.open('w') as f:
        for q in NEGATIVES:
            f.write(json.dumps({'question': q, 'expect': 'refusal'}) + '\n')
    print(f'{NEGATIVE}: {len(NEGATIVES)} out-of-scope questions')
    print('These are real Kubernetes topics deliberately outside the kubelet index,')
    print('plus a few obvious non-sequiturs. Correct behaviour is refusal on all of them.')


if __name__ == '__main__':
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'generate'
    {'generate': generate, 'review': review, 'fix': fix,
     'overlap': backfill_overlap, 'accept': accept, 'negatives': negatives}[cmd]()
