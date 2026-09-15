# RepoMind

**`git blame` tells you who changed a line and when. It never tells you why.**

In a codebase the size of Kubernetes, the reasoning behind a design lives in thousands of GitHub
issue threads, pull request reviews, and enhancement proposals — written years ago, by people who
have moved on, and effectively unsearchable. When you need to know *why* kubelet rejects pods under
disk pressure, the answer exists. It is buried in a 2019 argument across four issues and a KEP.

RepoMind indexes that discussion and answers questions from it, with citations, and refuses when it
doesn't know.

**Scope:** `kubernetes/kubernetes`, the kubelet subsystem — everything that ever touched
`pkg/kubelet`, plus the sig-node enhancement proposals.

📖 **[PROJECT.md](PROJECT.md)** — full walkthrough: the problem, how every stage works, all eight
bugs and how they were found, what was achieved and to what degree, and what is still open.

---

## Example

> **Q: Why does kubelet evict pods under disk pressure?**
>
> Kubelet evicts pods when the node is under disk pressure because the node-level "DiskPressure"
> condition signals that the disk resource is exhausted and must be reclaimed; the eviction design
> treats disk as a best-effort resource for every QoS class, so when pressure is observed the kubelet
> must free space by rejecting new pods and evicting existing ones **(#84403)**.
>
> **Sources** · issue #84403 · PR #99095 · PR #84415 · issue #54314 · PR #38836

Answering that by hand means reading roughly forty issue threads spanning six years.

Ask it something outside the index — etcd's raft implementation, kube-scheduler's bin packing — and
it says so rather than guessing.

---

## What's indexed

| Source | Documents | Chunks |
|---|---:|---:|
| Pull requests (descriptions, reviews, inline review comments, comments) | 6,245 | 77,351 |
| Issue threads | 1,987 | 28,084 |
| Commit messages (mainline) | 6,272 | 6,272 |
| KEPs (sig-node enhancement proposals) | 126 | 930 |
| sig-node community docs | 25 | 431 |
| **Total** | **14,655** | **113,047** |

The PR and issue sets are *complete* for the subsystem, not sampled — see
[Scraping](#scraping-how-the-corpus-is-built) for why that's harder than it sounds.

---

## Does it work?

Measured against a 37-question human-reviewed golden set (ground truth = the document the answer lives in) and a
22-question out-of-scope set. Full method and caveats in [`eval/results.md`](eval/results.md).

| Configuration | Recall@10 | MRR |
|---|---:|---:|
| Vector only | 0.838 | 0.618 |
| BM25 only | 0.811 | 0.605 |
| RRF hybrid | 0.919 | 0.767 |
| RRF + cross-encoder | 0.919 | 0.792 |
| RRF + cross-encoder, no query rewrite | **0.973** | **0.865** |

**Hybrid retrieval earns its place.** The two retrievers sit within 0.03 of each other alone
(0.838 / 0.811) yet fusing them reaches 0.919 — they fail on *different* questions, which is a
better argument for fusion than either being stronger.

**The reranker behaves as a reranker should** — recall unchanged (it reorders a fixed candidate set),
MRR up 0.013. Only 7 chunks reach the model, so that reordering is the point.

### The evaluation's most useful result was finding a bug

The first run said BM25 beat the embedding model by 0.20 recall. Instead of writing that up as
"embeddings underperform on technical text", it was worth asking why — and the answer was that
`all-MiniLM-L6-v2` reads **256 tokens** while the chunker was emitting a mean of **671**. The embedder
was seeing roughly the first third of every chunk. BM25, reading full text from SQLite, saw all of it.

| Configuration | Before fix | After fix |
|---|---:|---:|
| **Vector only** | 0.640 | **0.820** |
| BM25 only | 0.840 | **0.840** |

*(measured on the original 50-question set, so the truncation fix and the question review stay separable)*

**BM25 did not move at all.** That's the control: a fix aimed at the embedder should move vector
recall and leave BM25 exactly where it was. Vector recall rose 0.180 and the original finding
reversed — vector now *beats* BM25 on the high-lexical-overlap half of the set.

Two further problems surfaced on the way: the chunker never split an oversized unit (one comment
reached 56,086 tokens), and the metadata prefix had grown to **221 of the 256 tokens**, leaving 25
for actual discussion. Fixing all three took the index from 627k malformed chunks to 113k correct ones.

**Corpus noise, once visible:** only **35%** of Kubernetes PR comments are substantive — 19.9% are
bot-authored, 27.9% are Prow slash commands (`/lgtm`, `/approve`), 17.2% are sub-60-character replies.
Those are now filtered.

**The confidence threshold was inert.** The original 0.40 gave identical coverage and leakage anywhere
from 0.20 to 0.45 — every refusal came from the other two layers. An intermediate sweep reported 92%
coverage at 0.60, but it measured `max(score)` over reranked chunks while the system gates on
`vec_hits[0]['score']` — a quantity production never computes. Re-swept against the real gate on the
reviewed set, **0.60** gives **91.9% coverage, 18.2% leakage past the gate**.

### Citations and refusal, measured end to end

Over 21 out-of-scope and 14 answered in-scope questions:

| Layer | Out-of-scope refusals |
|---|---:|
| 1. Confidence gate | 18/21 |
| 2. Cross-encoder (no chunk scored > 0) | 2/21 |
| 3. Model (`INSUFFICIENT_CONTEXT`) | 0/21 |
| **Total** | **20/21 = 95.2%** |

The layers aren't redundant — two questions cleared the confidence gate and were stopped by the
cross-encoder. The single question that got through is probably a flaw in the negative set rather
than the system; see [`eval/citation_results.md`](eval/citation_results.md).

**Citations:** 93% of answers carry one (up from 30% before two bugs were fixed), and **100% of 17
citations were grounded** in a document actually retrieved — the model never invented a source.
Verification fails open on **7.1%** of answers, which is the honest size of the hole in that
guarantee.

---

## How it works

```
                    ┌──────────── INGESTION (offline) ────────────┐
  blobless clone ──▶ git log -- pkg/kubelet ──▶ commits
         │                     │
         │                     └─▶ merge messages ──▶ PR numbers ──▶ GraphQL ──▶ PRs
         │                                                              │
         │                                          label search ───────┴──▶ issues
         │                                        + fixes #N back-refs
  enhancements ────▶ KEP sections ──────────────────────────────────┐
                                                                     ▼
                              token-aware chunking (256 tok = model limit, 32 overlap,
                               metadata prefix baked into the chunk text)
                                                     │
                                  ┌──────────────────┴──────────────────┐
                                  ▼                                     ▼
                            Qdrant (384-d)                    SQLite (BM25 corpus)


                    ┌──────────── RETRIEVAL (per query) ───────────┐
  question ──▶ LLM rewrite ──┬─▶ vector search (k=20) ──┐
                             └─▶ BM25 search   (k=20) ──┴─▶ RRF fusion
                                                              │
                                        hydrate BM25-only hits from Qdrant
                                                              │
                                              CrossEncoder rerank ──▶ top 7


                    ┌──────────── GENERATION ──────────────────────┐
  top 7 + strict prompt ──▶ LLM (streamed) ──▶ citation verification ──▶ answer
                                                       │
                                        invalid citations stripped before display
```

### Refusing is a feature

A confident wrong explanation of *why* a design exists is worse than no answer, because the reader
can't tell it's wrong and will repeat it. Three independent layers can refuse:

1. **Confidence gate** — top vector score below `CONFIDENCE_THRESHOLD` (0.60, chosen by sweep), or no chunks survive,
   and the question is answered with a fallback and logged as out-of-scope.
2. **Reranker** — the CrossEncoder drops chunks scoring ≤ 0, so a question can clear the threshold
   and still end up with nothing worth answering from.
3. **The model itself** — the system prompt requires `INSUFFICIENT_CONTEXT` when the retrieved text
   doesn't actually answer the question.

Then every citation in a surviving answer is checked against the chunk it cites, and unsupported ones
are removed before the answer reaches the screen.

---

## Scraping: how the corpus is built

The naive approach — page through the repository's pull requests and keep the ones touching
`pkg/kubelet` — requires walking 100,000+ PRs and still yields whichever ones you happened to reach
first. `ingestion/scraper_v2.py` moves the expensive enumeration step off the API entirely:

| Stage | Method | Cost |
|---|---|---|
| Commits | Blobless clone, `git log --first-parent -- pkg/kubelet` | 262 MB, 48 s, **zero API calls** |
| PR discovery | PR numbers parsed from merge-commit messages | free — 6,268 numbers, only 4 needed the API |
| PR fetch | Fetched by number, batched 12 per GraphQL query | ~2,200 points, ~20 min |
| Issues | Date-sliced label search **plus** `fixes #N` back-references from PRs | — |
| KEPs | `git clone kubernetes/enhancements`, split on `##` headings | zero API calls |

Two things this buys that matter:

- **Back-references nearly doubled the issue corpus.** Label search found 889 issues; PR
  back-references found ~1,098 more. Over half the kubelet issues were never labelled `area/kubelet`.
- **Truncation is measured, not assumed.** Every GraphQL connection requests `totalCount` (which is
  free), so the scrape ends with a report of exactly which PRs had discussion clipped. A second pass
  re-fetches only those at higher limits. Current status: **0 PRs clipped**.

`--first-parent` is deliberate: 13,562 commits touch `pkg/kubelet`, but 7,290 are intra-PR branch
commits ("rebase", "address review feedback") belonging to PRs already captured by their merge commit.

---

## Stack

| Layer | Choice |
|---|---|
| Answering + citation verification | Groq · `openai/gpt-oss-120b` |
| Query rewriting | Groq · `qwen/qwen3.8-27b` |
| Embeddings | `all-MiniLM-L6-v2` (384-d, cosine) |
| Reranking | `cross-encoder/ms-marco-MiniLM-L-6-v2` |
| Vector store | Qdrant |
| Keyword search | BM25 (`rank-bm25`) over SQLite |
| API | FastAPI, SSE streaming |
| Frontend | React 19 + Vite |

Models are routed by task. Query rewriting is trivial and runs on every question, so it uses a model
with no reasoning-token overhead; answering and verification need stronger instruction-following.
Both are configurable via `GROQ_MODEL` / `GROQ_MODEL_FAST`.

---

## Setup

**Requires:** Python 3.11+, Docker, Node 18+, a Groq API key, and a GitHub token
(classic, `public_repo` scope).

```bash
cp .env.example .env         # then fill in GROQ_API_KEY and GITHUB_TOKEN
docker compose up -d qdrant  # or: docker run -d -p 6333:6333 -v "$PWD/qdrant_storage:/qdrant/storage" qdrant/qdrant
./setup.sh                   # venv, deps, schema, scrape, index
```

Then, in two terminals:

```bash
uvicorn api.main:app --reload --port 8000
cd repomind-ui && npm install && npm run dev
```

Open http://localhost:5173.

### Verify

```bash
curl http://localhost:8000/health

python -c "from retrieval.pipeline import retrieve; c,s,_ = retrieve('How does kubelet handle pod eviction?','kubernetes_kubernetes'); print(f'score={s:.3f} chunks={len(c)}')"
```

Scraping takes 20–30 minutes and is fully resumable — every stage skips files already on disk.
Indexing embeds 67k chunks on CPU and takes a further 5–10 minutes.

---

## Layout

```
ingestion/
  scraper_v2.py     clone-driven scrape: commits → PRs → issues (+ deep re-fetch pass)
  kep_scraper.py    sig-node KEPs and community docs, from git
  chunker.py        token-aware chunking for issue/PR/commit threads
  embedder.py       MiniLM embedding → Qdrant + SQLite
  token_utils.py    tokenizer helpers
retrieval/
  vector_search.py  Qdrant search + id hydration
  bm25_search.py    BM25 over the SQLite corpus
  reranker.py       CrossEncoder
  pipeline.py       rewrite → hybrid → RRF → rerank
generation/
  prompts.py        system prompt, fallback message
  generator.py      shared stages: should_answer / build_messages / finalize
api/
  main.py  models.py  deps.py
  routes/           chat (SSE), auth, admin, index, webhook
repomind-ui/        React frontend
db/                 schema + Qdrant collection setup
index_small.py      chunk everything and upsert
```

---

## Tests

```bash
pytest tests/ -q     # 22 tests, ~18s
```

Not aiming for coverage. Each test pins behaviour that either caused a real bug here or
would fail invisibly: RRF must not discard BM25-only hits, every chunk must carry its
citation metadata, `INSUFFICIENT_CONTEXT` must convert to a fallback, context numbering must
start at 1. One test guards the eval set itself — it fails if any golden question references
"this PR", since such a question can't be asked by someone who hasn't already found the answer.

Runs on every push via `.github/workflows/tests.yml`.

---

## Documents

| File | What it holds |
|---|---|
| **[PROJECT.md](PROJECT.md)** | Full walkthrough — architecture, results, every bug, open questions |
| [DECISIONS.md](DECISIONS.md) | Each design decision, the alternative rejected, and why |
| [eval/results.md](eval/results.md) | Retrieval analysis and the bug narrative |
| [eval/citation_results.md](eval/citation_results.md) | Citation and refusal measurements |
| [agent.md](agent.md) | Architecture brief for working in the codebase |
| [DEPLOY.md](DEPLOY.md) | Deployment procedure |

---

## Known limitations

- **The golden set is LLM-drafted and not human-reviewed.** Mechanical checks caught and regenerated
  10 self-referential questions; the remaining 40 have not been read by a person.
- **Query rewriting may be hurting retrieval** and costs an LLM call per question. The eval's
  construction bias penalises paraphrasing, so this needs a human-written question set to settle.
- **Commit messages are the weakest source** in the corpus — most kubelet commits are terse. Whether
  they help retrieval or dilute it is untested; `COMMITS_FIRST_PARENT` toggles it.
- **BM25 loads all 113,068 chunks into memory** on first query (~2.2 s) and never refreshes, so chunks
  indexed after the API starts are invisible to keyword search until restart.
- **Citation verification fails open.** If the verifier's response can't be parsed, the answer is
  returned unverified with `verification_ran: false`, and the UI says so.
- **Single subsystem, single repo.** Nothing here is multi-tenant despite a few parameterised paths.
- **No test suite yet.**

---

## License

MIT
