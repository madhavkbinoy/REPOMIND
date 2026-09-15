# Phase 0 — Make the Code Honest

**Goal:** eliminate every defect an interviewer could find by reading the repo for ten minutes, so that
what the README claims and what the code does are the same thing.

**Estimated effort:** ~1 week part-time · 6 tasks · one commit each

**Branch:** `git checkout -b phase-0-hardening`

**Definition of done:** every task below verified, `README.md` contains no claim the code doesn't
deliver, and `curl`-level smoke tests in §7 all pass.

---

## Progress

| Task | Status |
|---|---|
| 0.0 Data layer | ✅ env, Qdrant, SQLite, full scrape |
| 0.1 BM25 fix | pending — needs the index |
| 0.2 Remove link expansion | ✅ |
| 0.3 Auth IDOR | ✅ verified 401 / 200 / 401 / 401 |
| 0.4 bcrypt | ✅ legacy sha256 upgrades in place on login |
| 0.5 Collapse generation path | ✅ |
| 0.6 Hygiene batch | ✅ all seven items |
| 0.7 Dead Groq model | ✅ **new — was blocking** |
| 0.8 Verified answer discarded by UI | ✅ **new — anti-hallucination was inert** |

### Corpus as scraped

| Source | Docs | Chunks |
|---|---|---|
| Commits (mainline) | 6,272 | 6,272 |
| PRs | 6,245 | — |
| Issues | 1,987 | — |
| KEPs (sig-node) | 126 | 930 |
| Community docs | 25 | 431 |

### Two blocking defects found during execution, not during planning

**0.7 — `llama-3.3-70b-versatile` no longer exists.** Groq has retired every Llama chat
model; all four call sites returned 404. Nothing in retrieval or generation could run. Replaced with
env-configurable `GROQ_MODEL` / `GROQ_MODEL_FAST`, and routed by task after measuring that the
gpt-oss family are *reasoning* models: they emit reasoning tokens before content, so the original
`max_tokens=100` on query rewriting was entirely consumed by reasoning and returned an **empty
string** — which would have flowed into `vector_search("")` and produced universal fallback that looks
exactly like a corpus problem. `max_tokens` raised across all four sites; the empty case now logs and
falls back to the raw question.

Final routing: `qwen/qwen3.8-27b` for rewriting (7 tokens, no reasoning overhead) and
`openai/gpt-oss-120b` for answering and verification.

**0.8 — the UI discarded the verified answer.** `App.jsx` set `content: answer` — the accumulated
*streamed* tokens — rather than `data.answer` from the done frame. Citation verification ran on every
response, computed a corrected answer with invalid citations stripped, and the frontend threw it
away. The third layer of the anti-hallucination system was inert on the only path the UI uses. Found
while collapsing the duplicated generation path in 0.5, which is exactly the class of bug that
duplication hides.

### Scrape findings worth keeping for `DECISIONS.md`

- **Clone-derived enumeration**: 6,268 PR numbers from merge-commit messages in 48 s and 262 MB, zero
  API calls. The old scraper found 943 by walking the repo's PR list. Only **4** commits needed API
  resolution, down from 8,458, after switching to `--first-parent`.
- **My cost model was wrong by ~17×.** Estimated 5.6 points/PR from a naive requested-node count;
  measured **0.33**. GitHub prices on actual backend work. The whole PR stage cost ~2,200 points and
  ran in ~20 minutes, not the 7 hours estimated — so the depth-vs-speed tradeoff was largely spurious.
- **Back-references nearly doubled the issue corpus.** Label search found 889 issues; `fixes #N`
  references from PR bodies found ~1,098 more. Label search alone would have missed over half.
- **`component/kubelet` matches zero issues** — dead config in `.env`, safe to drop.
- **Truncation was measured, not guessed.** `totalCount` on every connection reported reviews clipped
  on 320 PRs, review comments on 51, comments on 114. At 0.33 pts/PR a deep second pass over just
  those costs ~150 points, so it was worth doing — a decision the instrumentation made for us.

---

## Task order, and why it matters

| # | Task | Type | Needs data? | Est. |
|---|------|------|-------------|------|
| 0.0 | Restore the data layer | setup | — | 30 m hands-on + 2–4 h unattended |
| 0.1 | Fix hybrid search — BM25 hits are discarded | correctness | **yes** | 2–3 h |
| 0.2 | Resolve the dead link-expansion feature | honesty | no | 1–2 h |
| 0.3 | Close the chat-history authorization hole | security | no | 3–4 h |
| 0.4 | Replace unsalted SHA-256 password hashing | security | no | 1–2 h |
| 0.5 | Collapse the duplicated generation path | design | no | 3–4 h |
| 0.6 | Hygiene batch | hygiene | no | 2–3 h |

**Start 0.0 first and let it run unattended.** Scraping is bound by GitHub's rate limiter, not by your
machine, and only Task 0.1 and the §7 smoke tests actually need a populated index. Everything else is
pure code. Write those while the scrape runs:

```
Day 1 morning   0.0  kick off scrape          <- hours, unattended
Day 1-3         0.2  remove link expansion    |
                0.3  bearer-token auth        |  no data required
                0.4  bcrypt                   |
                0.5  collapse generation path |
                0.6  hygiene batch            |
Once indexed    0.1  BM25 fix + verification
                 S7  smoke tests
```

Two ordering constraints inside that: **0.5 before 0.6**, because the hygiene batch touches files 0.5
restructures; and **0.1 last**, because its verification and before/after recall delta need the index.

> **Scope note.** Phase 0 makes retrieval *correct* — it fixes one bug where BM25 results were computed
> and discarded. It does not make retrieval *good*. Whether the cross-encoder, RRF and query rewriting
> earn their place is Phase 1's question, and it can only be answered with measurement.

---

## 0.0 — Restore the data layer

### Why this is first

This checkout has no data at all:

```
data/              missing   - nothing scraped
db/repomind.db     missing   - no SQLite, no BM25 corpus
qdrant_storage/    missing   - Qdrant has never run here
venv/              missing
.env               missing   - no GROQ_API_KEY, no GITHUB_TOKEN
```

`data/`, `db/` and `qdrant_storage/` are all git-ignored, so cloning the repo gets you code and nothing
else. Task 0.1's verification and every smoke test in §7 require a populated index.

### Scraper: use `ingestion/scraper_v2.py`, not the originals

The original scrapers cannot give you complete coverage in reasonable time.
`github_scraper.py` walks the repository's **entire** merged/closed PR history (100k+ PRs) with no
label or path filter and screens client-side in `_pr_touches_kubelet()`, so it keeps whichever
`MAX_PRS` PRs it happens to reach first. It also requests tens of thousands of GraphQL nodes per page
against a 5,000 point/hour budget, and has **no rate-limit handling at all** — `raise_for_status()` at
line 74 kills a multi-hour run on the first 403. Its issue query also passes only
`KUBELET_LABELS[0]`, so `component/kubelet`-only issues are never fetched.

`scraper_v2.py` moves the expensive enumeration off the API:

| Stage | How |
|---|---|
| Commits | Blobless clone + `git log -- pkg/kubelet`. Complete, instant, zero API calls. |
| PRs | PR numbers read out of merge-commit messages; unmatched commits resolved via `associatedPullRequests`; those specific PRs fetched by number, batched 12 per request with GraphQL aliases. |
| Issues | Date-sliced label search across **all** configured labels (auto-subdividing when a window exceeds GitHub's hard 1,000-result cap), unioned with `fixes #N` back-references from the PRs — which catches kubelet issues that were never labelled. |

It also trims per-PR node counts (reviews 50→20, review comments 30→10, comments 100→50), which is
where most of the original's point cost went.

There is no `MAX_PRS` decision to make any more: you get everything that touched the path, once.
That matters for Phase 1 — a golden set built on a partial, order-dependent corpus measures a system
you cannot reproduce.

> **Worth a `DECISIONS.md` entry in Phase 2:** *replaced API pagination with a blobless clone plus
> targeted fetch — partial coverage in many hours became complete coverage in under two, by moving the
> enumeration step off the rate-limited API entirely.*

### Steps

Steps 1–4 are **already done** in this checkout: `venv/` (Python 3.13), dependencies installed, Qdrant
running as the `repomind-qdrant` container on `:6333`, `.env` scaffolded, and `db/setup.py` run —
SQLite tables created and the `kubernetes_kubernetes` collection live at 384 dimensions.

Note `docker-compose` is not installed on this machine; Qdrant was started with plain `docker run`:

```bash
docker run -d --name repomind-qdrant -p 6333:6333 \
  -v "$PWD/qdrant_storage:/qdrant/storage" qdrant/qdrant
```

**5. Add your two API keys to `.env`.** Nothing below runs without them.

- `GROQ_API_KEY` — https://console.groq.com
- `GITHUB_TOKEN` — classic token with `public_repo` scope is simplest for a public repo. A
  fine-grained token needs **Issues: Read-only** and **Pull requests: Read-only**, not just Contents.

**6. Scrape.** One command, resumable, safe to re-run — every stage skips files already on disk:

```bash
nohup ./venv/bin/python ingestion/scraper_v2.py all > scrape.log 2>&1 &
tail -f scrape.log
```

**Measured on this checkout** (commits stage already run):

| | Result |
|---|---|
| Blobless clone | 262 MB, ~45 s |
| Commits touching `pkg/kubelet` | 13,562 total — **6,272 mainline**, 7,290 intra-PR churn |
| PR numbers derived from commit messages | **6,268** (6.6× what the old scraper found) |
| Commits still needing API resolution | **4** (1 request, down from 212) |
| PR fetch | 523 batched requests, ~35,000 points → **~7 h** |

`--first-parent` is the default: the 7,290 non-mainline commits are "address review feedback" /
"rebase" / "fix lint" commits belonging to PRs already identified by their merge commit. Indexing them
filled over half the commit corpus with work-in-progress noise and would have cost ~200 wasted API
requests. Set `COMMITS_FIRST_PARENT=false` to index the full set as a Phase 1 ablation row.

The PR stage runs ~7 hours because GitHub prices GraphQL on **requested capacity, not returned data** —
nested connections multiply, so `reviews(30){comments(15)}` reserves 450 node slots per PR. It is
unattended and fully resumable (every stage skips files already on disk), so rate-limit sleeps cost
wall-clock only. Stages can be run individually: `commits`, `prs`, `issues`.

Every connection carries `totalCount`, which is free, so the run ends with a **truncation report** —
exactly which PRs exceeded their limits, written to `data/raw/kubernetes_kubernetes/truncated_prs.json`.
If that list is short, page those few by hand; if it is long, build pagination knowing it is justified.
Either way the loss is measured rather than silent.

**7. Index.**

```bash
./venv/bin/python index_small.py
```

Embeds every chunk with MiniLM on CPU — roughly 10–30 minutes depending on corpus size.

### An open question worth deferring to measurement

Roughly 13,000 commit messages is the bulk of the corpus by document count, and most kubelet commit
messages are terse (`Fix typo`, `Update bazel`, `bump dependency`). They may be diluting retrieval rather
than helping it — short low-content documents are exactly what BM25 over-rewards.

**Don't decide this by intuition.** Index them, and make *with commits / without commits* one of the rows
in the Phase 1 ablation table. If they don't earn their place, dropping them is a measured decision you
can explain, which is worth more than either keeping or cutting them on a hunch.

### Verify

```bash
# raw JSON on disk
for d in issues prs commits; do echo -n "$d: "; ls data/raw/kubernetes_kubernetes/$d 2>/dev/null | wc -l; done

# vectors in Qdrant
curl -s localhost:6333/collections/kubernetes_kubernetes | python3 -m json.tool | grep points_count

# bm25 corpus in SQLite
sqlite3 ./db/repomind.db "SELECT source_type, COUNT(*) FROM chunks GROUP BY source_type;"

# end to end
./venv/bin/python -c "from retrieval.pipeline import retrieve; c,s,r = retrieve('How does kubelet handle pod eviction?','kubernetes_kubernetes'); print(f'score={s:.3f} chunks={len(c)}')"
```

The last command must return a non-zero score and at least one chunk before you start Task 0.1.

### Not a commit

Nothing here is committed — `data/`, `db/` and `.env` are all git-ignored, by design. This task produces
local state, not repository changes.

---

## 0.1 — Fix hybrid search: BM25 currently contributes nothing

### What's wrong

`retrieval/pipeline.py:93`

```python
fused_ids  = _rrf(vec_hits, bm25_hits)
vec_map    = {h['id']: h for h in vec_hits}
candidates = [vec_map[i] for i in fused_ids if i in vec_map]   # ← bug
```

`_rrf()` correctly fuses the two ranked lists. The next line then filters the fused result down to
**only IDs that vector search already returned**. Any document BM25 found that the embedding search
missed is computed, ranked, and thrown away.

### Why it matters

BM25 exists to catch what embeddings are bad at: exact identifiers (`eviction_manager.go`), flag names
(`--eviction-hard`), error strings, API field names. Embeddings smear those into "generally related
eviction content." Right now BM25 can only *reorder* documents vector search already found — which is
the one thing you don't need it for.

This is also the single most defensible thing you'll fix in Phase 0, because it's a real correctness bug
in the part of the system you'd describe as the interesting part.

### The fix

Add a hydration helper to `retrieval/pipeline.py`. BM25 returns bare chunk IDs with no payload, so
fetch the missing ones from Qdrant by point ID:

```python
def _hydrate(ids: list[str], collection: str) -> dict[str, dict]:
    """Fetch payloads for fused ids that vector search didn't already return."""
    if not ids:
        return {}
    try:
        points = qdrant.retrieve(
            collection_name=collection,
            ids=ids,
            with_payload=True,
        )
        return {str(p.id): {'id': p.id, 'score': None, **p.payload} for p in points}
    except Exception:
        return {}
```

Then rewrite the candidate assembly in `retrieve()`:

```python
    fused_ids  = _rrf(vec_hits, bm25_hits)
    chunk_map  = {str(h['id']): h for h in vec_hits}
    missing    = [i for i in fused_ids if i not in chunk_map]
    chunk_map.update(_hydrate(missing, collection))
    candidates = [chunk_map[i] for i in fused_ids if i in chunk_map]
    best_score = vec_hits[0]['score'] if vec_hits else 0.0
    top        = rerank(rewritten, candidates[:20], top_n=7) if candidates else []
```

Note the `str()` normalisation on both sides — Qdrant returns UUID point IDs and SQLite stores them as
text; mismatched types here would silently reproduce the original bug.

### Known follow-on: `best_score` semantics

`best_score` is still the top **vector** score, and it's the only input to the confidence gate. Once
BM25 can contribute, a question answered entirely by a BM25-only chunk can score low and trigger the
fallback anyway.

**Don't fix this now.** Changing retrieval and the confidence gate in the same commit means you can't
attribute the effect of either. Write it down, and let the Phase 1 threshold sweep tell you what the
gate should actually key off.

### Verify

```bash
python - <<'EOF'
from retrieval.pipeline import retrieve
from retrieval.bm25_search import bm25_search
from retrieval.vector_search import vector_search

q = "eviction_manager.go pod ranking"
v = {str(h['id']) for h in vector_search(q, 'kubernetes_kubernetes', k=20)}
b = {str(h['id']) for h in bm25_search(q, k=20)}
print("bm25-only ids:", len(b - v))          # must be > 0 for this test to mean anything

chunks, score, rewritten = retrieve(q, 'kubernetes_kubernetes')
ids = {str(c['id']) for c in chunks}
print("bm25-only ids that survived:", len(ids & (b - v)))
EOF
```

Before the fix the second number is always `0`. After the fix it should be non-zero for at least some
identifier-shaped queries.

**Record the before/after.** Run an informal recall check on ~10 questions now and after the fix — you
want the delta for Phase 1, so you can report what the bug was costing rather than just that it existed.

### Commit

```
fix(retrieval): let BM25-only hits reach the reranker

RRF fused vector and BM25 rankings, then filtered candidates to ids
present in the vector result — discarding every document BM25 found
that embeddings missed. Hydrate missing payloads from Qdrant by id.
```

---

## 0.2 — Resolve the dead link-expansion feature

### What's wrong

The README advertises link expansion ("Map file paths to related PRs/issues"). The chain is broken at
the first link:

- `ingestion/chunker.py::chunk_code_file()` is **never called** — not by `index_small.py`, not by
  `workers/tasks.py`.
- So no chunk ever has `source_type='code'`.
- So `retrieval/pipeline.py:54 expand_with_links()` — which only acts on `source_type == 'code'` —
  never does anything.
- So `code_issue_links`, populated by `ingestion/linker.py`, is **written and never read**.

### Why it matters

This is the highest-risk item in Phase 0. A bug is forgivable; documentation that overstates the system
is not, and it's exactly what gets probed when an interviewer picks the most interesting-sounding
feature and asks you to walk through it.

### The fix — cut it

Building it properly means indexing Go source, which is a multi-week project and pure scope creep.
Remove the claim instead:

1. **`README.md`** — delete the "Link Expansion" bullet from the Retrieval Pipeline section.
2. **`retrieval/pipeline.py`** — delete `expand_with_links()` (lines 54–81) and `_fetch_by_number()`
   (lines 39–52, its only caller). In `retrieve()`, replace line 96 with `return top, best_score, rewritten`.
3. **`ingestion/chunker.py`** — delete `chunk_code_file()`.
4. **`ingestion/linker.py`** — delete the file, and remove its invocation from `setup.sh` and from
   `workers/tasks.py::full_index_repo`.
5. **`db/schema.sql`** — drop the `code_issue_links` table and its three indexes.
6. **`agent.md`** — update §4.1, §4.2 and §9 to match.

Once `expand_with_links` is gone, the module-level `db` connection at `retrieval/pipeline.py:15` has no
remaining users — delete it too, which also retires the thread-safety landmine noted in `agent.md` §9.

> **Alternative, if you'd rather keep it:** PR chunks already know which files they touch (it's in the
> chunk prefix), so you could store `files` in the PR payload and expand *PR → linked issues* without
> ever indexing code. That's a real feature in about half a day. But it's optional — cutting is the
> honest, cheap move, and choosing to ship less that fully works is itself the judgment being assessed.

### Verify

```bash
grep -rn "expand_with_links\|chunk_code_file\|code_issue_links\|linker" \
  --include=*.py --include=*.md --include=*.sh --include=*.sql . | grep -v PHASE0.md
# expect: no hits
```

### Commit

```
refactor: remove unimplemented link-expansion path

chunk_code_file() was never called, so no code chunks existed, so
expand_with_links() never fired and code_issue_links was written but
never read. Removed the dead path and the README claim it supported.
```

---

## 0.3 — Close the chat-history authorization hole

### What's wrong

`api/routes/auth.py:119,133,147` and `api/routes/chat.py:56` take `user_id` **directly from the
client** with no authentication:

```python
@router.get('/history')
def get_history(user_id: int):          # ← no token, no check
```

```bash
curl 'http://localhost:8000/api/history?user_id=1'        # reads any user's history
curl -X DELETE 'http://localhost:8000/api/history?user_id=1'   # deletes it
```

The `sessions` table exists and `create_session()` issues tokens, but only `/api/me` ever validates
one. This is a textbook IDOR, and it sits directly under a README heading that advertises an
"Authentication System."

### The fix

**Step 1 — add `api/deps.py`:**

```python
import os, sqlite3
from fastapi import Header, HTTPException

DB_PATH = os.getenv('DATABASE_PATH', './db/repomind.db')


def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def current_user(authorization: str = Header(None)) -> dict:
    """Resolve a bearer token to a user. 401 if missing, invalid or expired."""
    if not authorization or not authorization.lower().startswith('bearer '):
        raise HTTPException(status_code=401, detail='Not authenticated')
    token = authorization.split(' ', 1)[1].strip()
    conn = _db()
    try:
        row = conn.execute(
            '''SELECT u.id, u.username, u.is_admin
               FROM sessions s JOIN users u ON u.id = s.user_id
               WHERE s.token = ? AND s.expires_at > datetime("now")''',
            (token,)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(status_code=401, detail='Invalid or expired session')
    return {'user_id': row['id'], 'username': row['username'], 'is_admin': bool(row['is_admin'])}


def optional_user(authorization: str = Header(None)) -> dict | None:
    """Same, but returns None instead of raising — for endpoints that allow anonymous use."""
    try:
        return current_user(authorization)
    except HTTPException:
        return None
```

**Step 2 — history endpoints (`api/routes/auth.py`):**

```python
from fastapi import Depends
from ..deps import current_user

@router.get('/history')
def get_history(user: dict = Depends(current_user)):
    uid = user['user_id']
    ...
```

Apply the same change to `add_message` and `clear_history`. Drop the `user_id` parameter entirely from
all three — it should be impossible to pass.

**Step 3 — chat (`api/routes/chat.py`):** anonymous chat is a supported flow in the UI, so use
`optional_user`:

```python
@router.post('/chat')
async def chat(req: ChatRequest, user: dict | None = Depends(optional_user)):
    uid = user['user_id'] if user else None
```

Replace every `req.user_id` at lines 56, 57, 102, 103, 104 with `uid`.

**Step 4 — `api/models.py:11`:** delete `user_id: int | None = None` from `ChatRequest`.

**Step 5 — move `/api/me` off the query string.** `api/routes/auth.py:95` takes `?token=`; tokens in
URLs leak into server logs, browser history and `Referer` headers. Change it to
`def get_me(user: dict = Depends(current_user))` and return the user directly.

**Step 6 — frontend (`repomind-ui/src/App.jsx`):** send the header everywhere instead of the id.

```js
const authHeaders = () => {
  const token = localStorage.getItem('token')
  return token ? { Authorization: `Bearer ${token}` } : {}
}
```

- `/api/me` — `fetch(`${API_URL}/api/me`, { headers: authHeaders() })`, drop `?token=`
- `loadChatHistory` — drop `?user_id=`, add `headers: authHeaders()`; it no longer needs an argument
- `clearChat` — same
- `send()` — add `...authHeaders()` to the headers object and remove `user_id` from the payload

### Verify

```bash
# anonymous read is now rejected
curl -s -o /dev/null -w '%{http_code}\n' 'http://localhost:8000/api/history'     # 401

# with a real token it works
TOKEN=$(curl -s -X POST localhost:8000/api/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"demo","password":"demo"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')
curl -s -H "Authorization: Bearer $TOKEN" localhost:8000/api/history

# a forged/expired token is rejected
curl -s -o /dev/null -w '%{http_code}\n' -H 'Authorization: Bearer nope' localhost:8000/api/history  # 401
```

Then click through the UI: log in, send a message, reload the page, confirm history returns; log out and
confirm anonymous chat still works.

### Commit

```
fix(security): derive user identity from session token, not request body

/api/history and /api/chat accepted a client-supplied user_id with no
authentication, allowing any caller to read or delete another user's
chat history. Added a bearer-token dependency and removed user_id from
the request surface.
```

---

## 0.4 — Replace unsalted SHA-256 password hashing

### What's wrong

`api/routes/auth.py:23`

```python
def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()
```

No salt, no work factor. Identical passwords produce identical hashes, and the whole table is
rainbow-table-recoverable in seconds.

### The fix

Add `bcrypt` to `requirements.txt` (use it directly rather than through `passlib`, which has known
friction with `bcrypt` 4.x):

```python
import bcrypt

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

def verify_password(password: str, stored: str) -> bool:
    if stored.startswith('$2'):                       # bcrypt
        return bcrypt.checkpw(password.encode(), stored.encode())
    return hashlib.sha256(password.encode()).hexdigest() == stored   # legacy
```

**Transparent migration.** Rather than wiping the users table, upgrade legacy hashes on next successful
login — in `login()`, after verification succeeds:

```python
        if not user['password_hash'].startswith('$2'):
            conn.execute('UPDATE users SET password_hash = ? WHERE id = ?',
                         (hash_password(req.password), user['id']))
            conn.commit()
```

Worth doing even on a demo database: it's ten lines, it's the correct way to roll a hashing scheme in
production, and it's a good thing to be able to describe.

### Verify

```bash
curl -s -X POST localhost:8000/api/register -H 'Content-Type: application/json' \
  -d '{"username":"bcrypttest","password":"hunter2"}'
sqlite3 ./db/repomind.db "SELECT substr(password_hash,1,7) FROM users WHERE username='bcrypttest'"
# expect: $2b$12
```

Register the same password as two different users and confirm the stored hashes differ.

### Commit

```
fix(security): hash passwords with bcrypt, migrate legacy sha256 on login
```

---

## 0.5 — Collapse the duplicated generation path

### What's wrong

`generation/generator.py::generate()` is dead code. `api/routes/chat.py:52-118` reimplements the entire
flow inline — threshold gate, `SYSTEM_PROMPT.format()`, message assembly, `verify_citations`,
`dedupe_sources` — because it needs to stream tokens rather than return a completed string.

Two copies of your core logic. They will drift, and a reviewer opening both files sees it immediately.

### The fix

Extract the shared stages so streaming and non-streaming differ only in how they obtain the answer text.
In `generation/generator.py`:

```python
def should_answer(best_score: float, chunks: list[dict]) -> bool:
    return bool(chunks) and best_score >= THRESHOLD


def build_messages(question: str, chunks: list[dict], repo: str,
                   history: list[dict]) -> list[dict]:
    system = SYSTEM_PROMPT.format(repo=repo, context=format_context(chunks))
    msgs   = [{'role': m['role'], 'content': m['content']} for m in history[-6:]]
    msgs.append({'role': 'user', 'content': question})
    return [{'role': 'system', 'content': system}] + msgs


def finalize(answer: str, chunks: list[dict], best_score: float) -> dict:
    """Post-process a completed answer: insufficient-context check, citation
    verification, source dedupe. Shared by both the streaming and blocking paths."""
    if answer.strip().startswith('INSUFFICIENT_CONTEXT'):
        return {'answer': answer.replace('INSUFFICIENT_CONTEXT:', '').strip(),
                'sources': [], 'is_fallback': True, 'best_score': best_score}

    v = verify_citations(answer, chunks)
    return {
        'answer':            v.get('verified_answer', answer),
        'sources':           dedupe_sources(chunks),
        'is_fallback':       False,
        'best_score':        best_score,
        'citations_valid':   v.get('valid', True),
        'invalid_citations': v.get('invalid_citations', []),
        'verification_ran':  v.get('verification_ran', True),
    }
```

`generate()` becomes `should_answer` → `build_messages` → one blocking call → `finalize`.

`chat.py` becomes `should_answer` → `build_messages` → stream the deltas → `finalize` on the accumulated
text → emit as the `done` frame. The route should own **only** SSE framing and history persistence.

While you're in there, `chat.py:58,60` use Pydantic v1's deprecated `.dict()` — change to
`.model_dump()`.

### Verify

Both paths must produce the same `sources`, `citations_valid` and `invalid_citations` for the same
question. Ask one question through the API and the same one through `generate()` directly, and diff the
metadata.

### Commit

```
refactor(generation): single source of truth for prompt build and verification

chat.py duplicated generator.py's entire flow to support streaming.
Extracted should_answer/build_messages/finalize so both paths share it.
```

---

## 0.6 — Hygiene batch

Small items, one commit. Each is individually trivial and collectively they're the difference between a
repo that looks maintained and one that doesn't.

**a. SQLite thread safety.** `ingestion/embedder.py:12` opens a module-level connection on the import
thread; `chat.py` runs retrieval via `run_in_executor`. Pass `check_same_thread=False`:

```python
db = sqlite3.connect(os.getenv('DATABASE_PATH', './db/repomind.db'), check_same_thread=False)
```

(The `pipeline.py:15` connection should already be gone via 0.2.)

**b. Pin `requirements.txt`.** Currently every line is bare. Unpinned `transformers` or
`sentence-transformers` will break the build on some future release, and Phase 3 needs reproducible
Docker builds. Pin to what you're actually running: `pip freeze`, then curate down to direct
dependencies with `==` versions.

**c. Make CORS configurable.** `api/main.py:8` hard-codes `http://localhost:5173`, which will block your
own deployed frontend in Phase 3:

```python
origins = [o.strip() for o in os.getenv('ALLOWED_ORIGINS', 'http://localhost:5173').split(',') if o.strip()]
app.add_middleware(CORSMiddleware, allow_origins=origins, ...)
```

Add `ALLOWED_ORIGINS` to `.env.example`.

**d. Stop `verify_citations` failing silently.** `generation/generator.py` catches
`(json.JSONDecodeError, Exception)` and returns the **unverified** answer marked `valid: True` — so
verification failures are invisible and your anti-hallucination guarantee quietly doesn't apply. At
minimum log it, and return a distinguishable flag:

```python
    except Exception as e:
        print(f'[verify] verification failed, returning unverified answer: {e}')
        return {'valid': True, 'verified_answer': answer,
                'invalid_citations': [], 'verification_ran': False}
```

Thread `verification_ran` through `finalize()` (0.5) and into the API response. You'll want this number
in Phase 1 — "verification failed to parse on N% of answers" is a real limitation to report.

**e. Complete `.env.example`.** `api/routes/admin.py` reads `ADMIN_USERNAME` and `ADMIN_PASSWORD`
(defaulting to `admin`/`admin`) and the README documents them, but they're absent from the template.
Add them, plus `ALLOWED_ORIGINS` from (c).

**f. Frontend title.** `repomind-ui/index.html:6` still says `<title>ui</title>`. Make it
`RepoMind — design decisions in kubernetes/kubelet`.

**g. Remove `MAX_CHUNK_TOKENS` from `.env.example`** — nothing reads it; `chunker.py:3` hard-codes
`MAX_TOKENS = 500`. Either wire it up or delete it. Deleting is fine.

### Commit

```
chore: pin deps, configurable CORS, thread-safe sqlite, env template fixes
```

---

## 7. Final smoke test

Run all of this against a fresh start before you call Phase 0 done.

```bash
# 1. clean rebuild works end to end
./venv/bin/python db/setup.py

# 2. api starts and answers
uvicorn api.main:app --port 8000 &
curl -s localhost:8000/health                                  # {"status":"ok"}

# 3. anonymous chat works, logged-out
curl -s -N -X POST localhost:8000/api/chat \
  -H 'Content-Type: application/json' \
  -d '{"question":"How does kubelet handle pod eviction?","repo":"kubernetes/kubernetes","history":[]}' | head -5

# 4. history is protected
curl -s -o /dev/null -w 'unauthenticated history: %{http_code}\n' localhost:8000/api/history   # 401

# 5. out-of-scope question refuses
curl -s -X POST localhost:8000/api/chat \
  -H 'Content-Type: application/json' \
  -d '{"question":"How does the etcd raft implementation handle leader election?","repo":"kubernetes/kubernetes","history":[]}'
# expect is_fallback: true

# 6. no dead references remain
grep -rn "expand_with_links\|chunk_code_file\|code_issue_links" --include=*.py --include=*.md . | grep -v PHASE0.md
```

Then reread `README.md` line by line and confirm every sentence is true of the code as it now stands.

---

## Done checklist

- [ ] 0.0 Corpus scraped and indexed; end-to-end retrieval returns chunks
- [ ] 0.1 BM25-only hits reach the reranker; before/after recall delta recorded for Phase 1
- [ ] 0.2 Link expansion removed from code, README, `agent.md`, schema and `setup.sh`
- [ ] 0.3 `user_id` no longer accepted from clients anywhere; frontend sends bearer tokens
- [ ] 0.4 bcrypt hashing with transparent legacy migration on login
- [ ] 0.5 One prompt-build and verification path shared by streaming and blocking
- [ ] 0.6 Hygiene batch complete
- [ ] Smoke tests in §7 pass
- [ ] `README.md` contains no claim the code doesn't deliver
- [ ] Branch merged to `main`

---

## What comes next

Phase 1 builds the evaluation harness — golden set, negative set, the ablation table, and the threshold
sweep. That phase is what makes the project resume-worthy; Phase 0 is what makes Phase 1's numbers mean
something. Don't start measuring until this branch is merged.
