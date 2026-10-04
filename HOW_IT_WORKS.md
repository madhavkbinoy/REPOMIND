# How RepoMind works — in depth

A learning document. It explains what the system is, what it solves, every mechanism inside it, and
the concepts you need to defend any part of it in conversation.

`PROJECT.md` is the narrative (problem, results, bugs, how to pitch it). **This is the mechanics.**

---

# Part 1 — What it is and what it solves

## The problem, stated precisely

When you work on a large codebase you constantly hit questions of the form *"why is this like this?"*

`git blame` answers **who** and **when**. It does not answer **why**. The *why* was argued out years
ago in a GitHub issue thread, settled in a pull request review, and formalised in an enhancement
proposal — and none of that is searchable in any useful way, because:

- **GitHub search matches keywords, not reasoning.** The discussion that explains disk-pressure
  eviction may never use the phrase "disk pressure eviction."
- **The answer is distributed.** It's spread across four issues, two PR reviews and a KEP, with each
  piece meaningless alone.
- **The participants are gone.** Kubernetes contributors from 2019 have moved on.

The practical consequence: you guess. And a plausible wrong explanation of why a design exists
**propagates** — you repeat it in a code review, and the wrong rationale becomes institutional
knowledge. Nobody can tell it's wrong, because not knowing was the original problem.

## What RepoMind does

It indexes the *discussion* around one subsystem and answers questions from it, **with citations**,
and **refuses when the answer isn't in the corpus**.

**Scope:** `kubernetes/kubernetes`, the kubelet subsystem — every PR, issue and commit that ever
touched `pkg/kubelet`, plus the sig-node enhancement proposals. 14,655 documents → 113,047 chunks.

## Why that scope

Narrow enough to index **completely** rather than sample, and rich enough that the questions are
genuinely hard. Completeness matters more than it sounds: a sampled corpus can't support "the answer
isn't here" as a claim, because you don't know what you left out.

## The design constraint that shaped everything

**Refusing beats answering.** If you ask why a design exists and get a fluent, confident, wrong
answer, you cannot detect that it's wrong — that's exactly the knowledge you lacked. An unanswered
question sends you to the source. A confidently wrong one sends you to a code review to repeat it.

Almost every architectural choice below follows from taking that seriously: three independent refusal
layers, citations on every claim, a verifier that strips unsupported ones, and a prompt that forbids
using the model's own training knowledge.

---

# Part 2 — Concepts you need

If you can explain these five things clearly, you can defend the whole system.

## 2.1 Embeddings (dense / vector / semantic search)

An **embedding model** maps text to a fixed-length vector of numbers. Here: `all-MiniLM-L6-v2`
produces **384 floats** per chunk. Texts with similar *meaning* land near each other in that
384-dimensional space, so similarity becomes geometry — cosine of the angle between two vectors.

```
"kubelet refuses pods when disk fills"  →  [0.03, -0.11, 0.42, …]   384 numbers
"node rejects workloads on low storage" →  [0.04, -0.09, 0.39, …]   nearby vector
```

**What it's good at:** paraphrase. The query and the document need not share a single word.

**What it's bad at:** exact tokens. `eviction_manager.go` gets mapped into a region meaning "roughly,
eviction-related stuff." The model has no concept of that *specific file*. Same failure for flag
names (`--eviction-hard`), error strings and API field names — the things engineers actually type.

**The mechanism to understand:** the embedding is computed **before** the query exists. That's what
makes it fast — you precompute 113,047 vectors once, then a query is one vector comparison against
an index. It's also what makes it imprecise: the model compressed the chunk into 384 numbers without
knowing what would be asked of it.

## 2.2 BM25 (sparse / lexical / keyword search)

BM25 scores documents by term overlap with the query, weighting each term by:

- **Term frequency** — more occurrences in the document is better, with diminishing returns
- **Inverse document frequency** — rare terms carry more signal than common ones
- **Length normalisation** — a long document shouldn't win just by containing more words

It has **no concept of meaning.** `eviction_manager.go` is a string to match, which is precisely why
it succeeds where embeddings fail. A rare token like `--eviction-hard` has very high IDF, so a
document containing it scores strongly.

**Why both:** they fail on *different* questions. Measured on this corpus, vector-only recall is
0.838 and BM25-only is 0.811 — nearly identical alone — yet fusing them reaches **0.919**. That gap
is the whole argument for hybrid search, and it's a better argument than "one is stronger."

## 2.3 Reciprocal Rank Fusion (RRF)

Two retrievers return two ranked lists with **incomparable scores** — cosine similarity (0 to 1) and
a BM25 score (unbounded, here up to ~26). You cannot add or average them meaningfully.

RRF throws the scores away and uses only **rank**:

```python
score(doc) = Σ  1 / (k + rank)        k = 60, rank starting at 0
          over each list the doc appears in
```

From `retrieval/pipeline.py`:

```python
def _rrf(vec_hits, bm25_hits, k=60):
    scores = {}
    for rank, h in enumerate(vec_hits):
        scores[h['id']] = scores.get(h['id'], 0) + 1 / (k + rank + 1)
    for rank, h in enumerate(bm25_hits):
        scores[h['id']] = scores.get(h['id'], 0) + 1 / (k + rank + 1)
    return sorted(scores, key=lambda x: -scores[x])
```

**Why it works:** a document both retrievers rank highly accumulates two contributions and beats one
that only one retriever found. Agreement between independent methods is evidence.

**Why `k=60`:** it flattens the curve. Without it, rank 1 would be worth 1.0 and rank 2 worth 0.5 —
the top result would dominate. With `k=60`, rank 1 scores 1/61 and rank 2 scores 1/62: nearly equal,
so *appearing in both lists* matters more than *being first in one*. That is the behaviour you want
from a fusion step. (60 is the value from the original RRF paper; untuned here, and flagged as such.)

## 2.4 Cross-encoders vs bi-encoders

This distinction comes up in every RAG interview.

| | Bi-encoder (the embedding model) | Cross-encoder (the reranker) |
|---|---|---|
| Input | Query and document **separately** | Query and document **together**, as one sequence |
| Output | A vector each; compare with cosine | A single relevance score for the pair |
| When computed | Documents embedded **ahead of time** | Must run **per query, per document** |
| Cost | Search 113k chunks in ~30 ms | ~700 ms for 34 pairs |
| Precision | Lower | Higher |

A bi-encoder must compress a document into a vector *without knowing the query*. A cross-encoder
reads both at once, so it can attend to the specific words of the query against the specific words of
the document. That's strictly more information, and strictly more expensive.

**The architecture that follows:** use the cheap, imprecise method to narrow 113,047 → 40, then spend
the expensive, precise method on those 40. This is the standard retrieve-then-rerank pattern, and the
reason it exists is purely this cost/precision asymmetry.

Here the reranker is `cross-encoder/ms-marco-MiniLM-L-6-v2`. It outputs an **unbounded logit** where
the **sign is meaningful** — positive means relevant, negative means not. That property is load-bearing;
see §7.2.

## 2.5 Why RAG rather than fine-tuning

An interviewer may ask why not fine-tune a model on the corpus.

- **Citations.** RAG can point at the source document. A fine-tuned model has absorbed the text into
  weights and cannot tell you where an answer came from. For this project citation *is* the product.
- **Refusal.** A retrieval step gives an explicit signal — nothing relevant came back — that you can
  gate on. A fine-tuned model has no equivalent; it will always produce fluent text.
- **Freshness.** New issues appear daily. Re-indexing is minutes; re-training is not.
- **Scale of data.** 113k chunks is far too little to fine-tune usefully and exactly right for retrieval.

---

# Part 3 — Stage 1: Ingestion

**Goal:** get every document that touched `pkg/kubelet`, completely and reproducibly.

**Files:** `ingestion/scraper_v2.py`, `ingestion/kep_scraper.py`

## 3.1 The naive approach and why it fails

The obvious method: page through the repository's pull requests via the GitHub API and keep the ones
touching `pkg/kubelet`. This fails three ways:

1. kubernetes/kubernetes has **100,000+ PRs**. You walk all of them to find a few thousand.
2. GitHub's GraphQL API bills by **requested node count**, and nested connections multiply —
   `reviews(first:50){comments(first:30)}` reserves 1,500 node slots per PR to hold what is typically
   ten actual comments. Against a 5,000-points/hour budget that's brutal.
3. You stop at whatever cap you set, so you get **whichever PRs you reached first** — a sample, and a
   different one each run. Not reproducible.

## 3.2 The approach used: enumerate in git, fetch from the API

The insight: **git already knows which commits touched a path, for free.**

```bash
git clone --filter=blob:none --no-checkout https://github.com/kubernetes/kubernetes.git
git log --first-parent --format='…' -- pkg/kubelet
```

`--filter=blob:none` is a **blobless clone**: it downloads all commits and trees but no file
contents. 262 MB, 48 seconds. You need the trees because `git log -- <path>` must diff them to decide
which commits touched the path; you don't need the blobs because you're never reading file contents.

That yields the **complete** commit set. Then merge-commit messages carry the PR numbers:

```
Merge pull request #141041 from lukaszwojciechowski/fix-PLR-CPU-realloc
```

**Measured: 6,268 PR numbers extracted from commit messages, zero API calls.** Only **4** commits
needed `associatedPullRequests` to resolve. The old scraper found 943 PRs.

### Why `--first-parent` matters

13,562 commits touch `pkg/kubelet`, but only **6,272 are on the mainline**:

| | count | what they are |
|---|---:|---|
| Mainline merges | 6,237 | one per merged PR |
| Mainline direct commits | 35 | squash-merged, carry `(#N)` suffixes |
| **Non-mainline** | **7,290** | *inside* PR branches: "rebase", "fix lint", "address review feedback" |

Those 7,290 belong to PRs already identified by their merge commit. Including them would have filled
over half the commit corpus with work-in-progress noise **and** cost ~200 wasted API round-trips
trying to resolve them.

## 3.3 Fetching the PRs

Those specific PRs are fetched **by number**, batched 12 per request using GraphQL aliases:

```graphql
query {
  repository(owner:"kubernetes", name:"kubernetes") {
    p0: pullRequest(number:109932) { ...prFields }
    p1: pullRequest(number:110021) { ...prFields }
    …
  }
}
```

One HTTP round-trip, twelve PRs. Measured cost: **0.33 points per PR** — the whole stage is ~2,200
points and ~20 minutes.

### Truncation is measured, not assumed

Every connection requests `totalCount`, which is **free** (it doesn't count toward node cost). So the
run ends with a report of exactly which PRs had discussion clipped:

```
reviews         clipped on 320 PRs
review_comments clipped on  51 PRs
comments        clipped on 114 PRs
```

A second pass (`stage_deep`) re-fetches **only those** at much higher limits. Final state: **0 PRs
clipped.** This is the pattern worth taking from the whole project — make the loss visible, then
spend effort only where it's real.

## 3.4 Issues: two sources, unioned

```
1. Date-sliced label search   →   889 issues
2. `fixes #N` back-references from PR bodies   →   ~1,098 more
                                  ────────────
                                  1,987 total
```

**Over half the kubelet issues were never labelled `area/kubelet`.** They were simply fixed by a
kubelet PR. Relying on labels alone would have lost the majority silently, with no error to notice.

The **date slicing** exists because GitHub Search caps every query at **1,000 results**, no matter
the pagination. So the search is sliced by `created:YYYY-MM-DD..YYYY-MM-DD` windows, and any window
returning >1,000 is **recursively subdivided**. Without this you quietly get 1,000 issues and
believe you're done — the single most common way people under-scrape GitHub.

Also measured: `component/kubelet`, configured as a second label, matches **zero** issues. Dead config
inherited from an assumption nobody checked.

## 3.5 KEPs — the highest-value source

Kubernetes Enhancement Proposals are design documents in a **fixed template**. Across the 126
sig-node KEPs:

```
## Motivation      124 KEPs
## Design Details  116 KEPs
## Alternatives    109 KEPs      ← rejected designs, written down, deliberately
## Drawbacks       103 KEPs
```

**"Alternatives" is a section where maintainers record what they considered and did not do.** For a
system whose entire purpose is surfacing design rationale, that's the highest-density source
available — and it's pre-structured. Pure git clone, no API, no token.

Templated sections (Table of Contents, Release Signoff Checklist, Production Readiness Review
Questionnaire) are skipped: they're identical boilerplate across 120 documents and would bury real
content.

## 3.6 Corpus filtering — 65% of PR comments are noise

Kubernetes is heavily automated. Measured over 12,016 sampled PR comments:

| | share |
|---|---:|
| Bot-authored (`k8s-ci-robot`, `codecov`, …) | 19.9% |
| Prow slash commands (`/lgtm`, `/approve`, `/retest`) | 27.9% |
| Sub-60-character replies ("+1", "ping", "done") | 17.2% |
| **Substantive discussion** | **35.0%** |

Only 35% carries discussion, though it holds 49% of the text. The rest produced thousands of
near-identical `/lgtm` chunks — **high-frequency, low-content strings that match many queries and
inform none of them.** Filtering is in `ingestion/chunker.py::is_substantive`.

---

# Part 4 — Stage 2: Chunking

**Goal:** split documents into retrievable units without destroying meaning.

**Files:** `ingestion/chunker.py`, `ingestion/token_utils.py`

## 4.1 Why chunking is the hard part here

An issue thread is **not prose.** It is several people disagreeing across time, and *the disagreement
is the signal.* Two naive approaches both fail:

**Fixed-size splitting** severs a rebuttal from the claim it rebuts. The resulting chunk reads as
consensus when it was an objection — actively misleading, worse than missing.

**One chunk per comment** loses the thread. *"I agree with the above, but only for guaranteed pods"*
is meaningless in isolation.

## 4.2 The algorithm

```
units = [issue body, review 1, inline comment, …, comment N]   # conversation turns, in order
                     ↓ filter: drop bots, slash commands, <40 chars
                     ↓ split any single unit that exceeds the budget
accumulate units into a buffer until adding the next would exceed 256 tokens
    → flush buffer as a chunk
    → seed the next buffer with the last 32 tokens of the flushed one (overlap)
each flushed chunk gets the metadata prefix prepended
```

Three properties make it work:

- **Units are conversation turns**, so a speaker's contribution is never cut mid-thought unless it
  alone exceeds the budget.
- **The 32-token overlap** preserves the join, so a claim at the end of one chunk and its rebuttal at
  the start of the next share context.
- **Oversized units are split**, not emitted whole. This was a bug — the loop originally appended an
  oversized unit regardless of size, and one chunk reached **56,086 tokens**.

KEPs are chunked differently: **one chunk per `##` section, no splitting.** An "Alternatives" section
is already a self-contained argument; imposing a token window would do damage that issue threads
require.

## 4.3 The 256-token budget is not a taste decision

```python
MAX_TOKENS = EMBED_MAX_TOKENS   # 256 -- all-MiniLM-L6-v2's max_seq_length
OVERLAP    = 32
```

`all-MiniLM-L6-v2` has `max_seq_length = 256`. Text beyond that is **silently truncated at encode
time** — no error, no warning. This caused the project's most consequential bug:

| | |
|---|---|
| Chunker target (before) | 500 tokens |
| Actual mean chunk produced | **671 tokens** |
| Model's limit | **256 tokens** |
| Chunks over the limit | **88%** |

So the embedder was seeing roughly **the first third of each chunk**, while BM25 — reading full text
from SQLite — saw all of it. Vector recall was depressed from 0.820 to 0.640, which looked like
"embeddings underperform on technical text."

A test now asserts `MAX_TOKENS <= EMBED_MAX_TOKENS`, and a second asserts that constant against the
*actual* model, so swapping embedders can't silently reintroduce the mismatch.

## 4.4 The metadata prefix lives inside the embedded text

Every chunk begins with:

```
[ISSUE #84403 - CLOSED]
Title: why kubelet admits guaranteed pods when under disk pressure?
Labels: area/kubelet, kind/bug
---
<the actual discussion>
```

**Why inside the text rather than only in the payload:**

1. **It gets embedded**, so a query mentioning "issue" or "KEP" has something to match.
2. **It survives into the model's context**, so the model can cite without a separate lookup.
3. A chunk stripped of its issue number **cannot be cited**, and citation is the product.

**But the prefix costs budget.** It is capped at **64 tokens**, because Kubernetes PRs carry ~10
process labels and very long file paths, and an unbudgeted header once reached **221 of the 256
tokens** — leaving 25 for actual discussion. The fix keeps only design-relevant labels (`area/`,
`kind/`, `sig/`, not `lgtm`/`size/L`/`approved`) and file **basenames** rather than full paths.

## 4.5 Never round-trip text through a tokenizer

The most subtle bug in the project. The obvious way to truncate:

```python
ids = tokenizer.encode(text)
return tokenizer.decode(ids[:max_tokens])     # ← destroys the text
```

`all-MiniLM-L6-v2` is **uncased**. Decoding returns lowercase with punctuation re-spaced:

```
[ISSUE #100005 - CLOSED]   →   [ issue # 100005 - closed ]
```

**62.4% of the index was stored that way.** The citation key stopped matching `r'#(\d+)'` — the
inserted space breaks it — so two thirds of chunks presented an identifier the model could not copy
and the verifier could not parse.

The fix uses **offset mapping** to slice the original string:

```python
enc = _tokenizer(text, add_special_tokens=False, return_offsets_mapping=True)
cut = enc['offset_mapping'][max_tokens - 1][1]   # end char of the last kept token
return text[:cut]                                 # a verbatim prefix
```

> **A tokenizer is a measuring instrument, not a transformation.** Use it to decide *where* to cut;
> never let its output become the text you store.

---

# Part 5 — Stage 3: Indexing

**Goal:** make 113,047 chunks searchable two different ways.

**Files:** `ingestion/embedder.py`, `db/setup.py`, `db/qdrant_client_factory.py`

## 5.1 Two stores, one corpus

Every chunk is written **twice**:

```
chunk ──┬──▶ embed with MiniLM ──▶ Qdrant   (384-d vector + payload)
        └──▶ full text          ──▶ SQLite  (bm25_index table)
```

| | Qdrant | SQLite |
|---|---|---|
| Holds | 384-d vector + payload (text, metadata) | full chunk text |
| Answers | "semantically nearest" | "best term overlap" |
| Index | HNSW approximate nearest neighbour | in-memory BM25 built at startup |

Note that **both** hold the text. Qdrant's payload carries it so retrieval can return chunks without
a second lookup; SQLite holds it because BM25 needs the raw tokens.

**This duplication is what made the truncation bug diagnosable.** The embedder truncated at 256
tokens; SQLite did not. So when the fix moved vector recall by +0.180 and left BM25 at *exactly*
0.840, that was proof — a fix aimed at the embedder had to move one and not the other.

## 5.2 Writing the index

`upsert_chunks` processes in batches of 100:

```python
vecs = embed_texts([c['text'] for c in batch])     # one model call for 100 chunks
for chunk, vec in zip(batch, vecs):
    pid = str(uuid.uuid4())                        # the id shared across both stores
    points.append(PointStruct(id=pid, vector=vec, payload={...}))
    db.execute('INSERT OR REPLACE INTO chunks    VALUES (?,…)', (pid, …))
    db.execute('INSERT OR REPLACE INTO bm25_index VALUES (?,?)', (pid, chunk['text']))
qdrant.upsert(collection_name=collection, points=points)
```

The **UUID is the join key**. BM25 returns bare ids, and `fetch_by_ids` uses them to pull payloads
from Qdrant. Both stores must agree on it, which is why ids are normalised with `str()` at every
boundary — Qdrant returns UUID objects, SQLite stores text.

Batching matters: embedding 100 texts in one call is far faster than 100 calls, because the model
processes them as a padded batch on one forward pass.

## 5.3 Qdrant configuration

```
size     384          # must equal the embedding model's output dimension
distance Cosine       # angle, not magnitude -- standard for sentence embeddings
points   113,047
payload indexes: source_type, state, number, labels
```

`size` and the model are a hard contract: a mismatch is a runtime error, and changing the embedding
model to one with different dimensions requires recreating the collection and re-embedding everything.

The payload indexes exist so filtered queries don't degrade to full scans. (They are currently
**unused** — a documented open item.)

## 5.4 Three deployment modes behind one factory

`db/qdrant_client_factory.py` is the single place that decides how to reach Qdrant:

| Mode | Env | Use |
|---|---|---|
| Server | `QDRANT_HOST` / `QDRANT_PORT` | development |
| **Embedded** | `QDRANT_PATH` | **free deployment — no server process** |
| Cloud | `QDRANT_URL` + `QDRANT_API_KEY` | hosted |

Embedded mode runs the engine **in-process** against a directory. Measured: **34 ms/query at 113,047
points, 566 MB on disk.** Qdrant warns against local mode above 20,000 points, but the cross-encoder
and the LLM take *seconds* — 34 ms is nowhere near the bottleneck. That measurement is what makes a
$0 deployment possible.

Three modules previously constructed their own client with host/port hardcoded, so supporting a
second deployment shape meant editing all three and keeping them in agreement.

---

# Part 6 — Stage 4: Retrieval

**Goal:** turn a question into the ~18 chunks most likely to contain the answer.

**File:** `retrieval/pipeline.py`

```
question
  │
  ├─▶ rewrite_query()          qwen3.8-27b — one LLM call
  │
  ├─▶ vector_search(k=20)      Qdrant cosine, score_threshold 0.25
  ├─▶ bm25_search(k=20)        in-memory BM25 over SQLite corpus
  │
  ├─▶ _rrf()                   fuse the two rankings
  ├─▶ fetch_by_ids()           hydrate BM25-only hits from Qdrant
  │
  └─▶ rerank(top_n=18)         cross-encoder scores (query, chunk) jointly
          │
          ▼
    18 chunks ≈ 4,674 tokens of context
```

## 6.1 Query rewriting

```python
REWRITE_PROMPT = '''
Rewrite this question as a short search query optimised for finding
GitHub issues and PRs about design decisions and architectural rationale.
Return ONLY the rewritten query, nothing else.
'''
```

The intent: a conversational question ("Why does kubelet reject pods when the disk fills up?")
contains filler that dilutes both the embedding and the BM25 term set. A compressed query
("kubelet disk pressure pod rejection") should retrieve better.

**It measures negative.** Removing it improves recall 0.919 → 0.973. Two honest caveats:

1. The golden questions were drafted *from* their source documents, so they share vocabulary with
   them. Rewriting paraphrases, destroying exactly that overlap — the eval is **biased against
   rewriting**.
2. It costs an LLM call on every single question.

It remains in place, documented as an open question, because acting on a confounded measurement is
how you make a system worse while believing you improved it.

### One failure mode worth knowing

`max_tokens` was originally 100. The replacement model (`gpt-oss-20b`) is a **reasoning model** — it
emits reasoning tokens before content — so the entire budget went to reasoning and `content` came
back **empty**. That empty string fed into `vector_search("")` and `bm25_search("")`: a meaningless
embedding and zero BM25 scores, making every question fall back. A total retrieval failure that looks
exactly like a missing corpus.

Fixed two ways: `max_tokens` raised to 512, and an empty response now **falls back to the raw
question** rather than returning `''`.

## 6.2 Vector search

```python
results = qdrant.query_points(collection, query=vec, limit=20,
                              with_payload=True, score_threshold=0.25)
```

`score_threshold=0.25` drops hits below 0.25 cosine similarity — a floor that prevents complete
junk entering the candidate pool. It's configurable via `VECTOR_SCORE_THRESHOLD` and **untuned**.

Failures are **logged, not swallowed.** This used to return `[]` on any exception, which made a
Qdrant outage indistinguishable from "no results" — the system would silently degrade to BM25-only
with no signal. It now distinguishes "collection missing" from "query failed" and says which.

## 6.3 BM25 search

The BM25 index is built **in memory on first query** by reading the entire `bm25_index` table:

```python
rows   = db.execute('SELECT chunk_id, text FROM bm25_index').fetchall()
corpus = [r[1].lower().split() for r in rows]
_index = BM25Okapi(corpus)
```

113,047 documents, ~2.9 seconds to load, then held resident. Two consequences, both documented:

- **Memory.** The full corpus text lives in RAM — the binding constraint on deployment sizing.
- **Staleness.** Chunks indexed *after* process start are invisible to keyword search until restart.
  `invalidate()` exists to clear the cache; SQLite FTS5 would remove the resident corpus entirely,
  but it changes retrieval behaviour and so must be measured as an ablation first.

Tokenisation is `text.lower().split()` — crude, and it is why BM25 sees `eviction_manager.go` as one
token, which is exactly the behaviour wanted.

## 6.4 Fusion and hydration

```python
fused_ids = _rrf(vec_hits, bm25_hits)

chunk_map = {str(h['id']): h for h in vec_hits}
missing   = [i for i in fused_ids if i not in chunk_map]
for c in fetch_by_ids(missing, collection):        # ← BM25 returns bare ids
    chunk_map[str(c['id'])] = c

candidates = [chunk_map[i] for i in fused_ids if i in chunk_map]
```

**The hydration step is essential and was once missing.** BM25 returns only chunk ids — no text, no
metadata. The original code filtered candidates with `if i in vec_map`, which discarded **every
BM25-only document**: RRF computed the fusion and the next line threw half of it away. The entire
reason to run BM25 was being discarded one line after computing it.

Measured on the trace below: **14 of 34 candidates are BM25-only.** Before the fix, all 14 were lost.

## 6.5 Reranking

```python
scores = _ce.predict([(query, c['text']) for c in candidates])   # joint scoring
ranked = sorted(zip(scores, candidates), key=lambda x: -x[0])
kept   = [c for score, c in ranked[:top_n] if score > MIN_RERANK_SCORE]
```

Scores all 34 candidate pairs, sorts, keeps the top 18 that clear zero.

**`TOP_N = 18` is a context-volume decision, not a count.** At 256-token chunks, 18 chunks ≈ 4,600
tokens. The previous value of 7 was calibrated when chunks were ~671 tokens — halving chunk size cut
the model's context from ~4,600 to **1,787 tokens** without changing a line of generation code, and
the model began answering `INSUFFICIENT_CONTEXT` to in-scope questions **whose answers were sitting in
the context it received.** Read without the measurement, that looks like an over-strict prompt.

### A tested non-change

The obvious optimisation is to retrieve more candidates so the reranker has more to choose from.
Measured, it's wrong:

| k per retriever | pool | Recall@10 | MRR |
|---:|---:|---:|---:|
| **20 (current)** | 40 | **0.880** | **0.761** |
| 40 | 80 | 0.880 | 0.755 |
| 60 | 120 | 0.860 | 0.747 |

Flat or worse. `ms-marco-MiniLM-L-6-v2` is a small cross-encoder, and its precision degrades as
distractors multiply. "More candidates is better" is a property of *strong* rerankers, not of
reranking. A larger reranker might invert this — a testable prediction, not an assumption.

---

# Part 7 — Stage 5: Generation and verification

**Files:** `generation/generator.py`, `generation/prompts.py`, `api/routes/chat.py`

## 7.1 The prompt

Seven rules, each targeting a specific failure:

| Rule | Prevents |
|---|---|
| 1. Every sentence traceable to a chunk | plausible-sounding synthesis |
| 2. Inline citation per claim, copied verbatim from the `Cite as` line | uncitable answers |
| 3. Don't cite a related-but-different source | "a source about admission is not a source about eviction ordering" |
| 4. State what the context does *not* cover | overclaiming from partial information |
| 5. No inference — "the context states X" not "the context implies X" | extrapolation |
| 6. Emit `INSUFFICIENT_CONTEXT` when it can't answer | fluent non-answers |
| 7. Never use training knowledge | the model answering from Kubernetes it already knows |

Rule 7 is the one people miss. Without it the model answers from pretraining, produces something
correct-sounding and *uncitable*, and the whole grounding apparatus becomes theatre.

## 7.2 The citation key — one label, one meaning

```python
def format_context(chunks):
    for c in chunks:
        parts.append(f'Cite as {citation_key(c)} | {src}\n{c["text"]}\n')
```

Producing:

```
Cite as (#84403) | https://github.com/kubernetes/kubernetes/issues/84403
[ISSUE #84403 - CLOSED]
Title: …
```

**The label *is* the citation key.** It was previously a positional index — `[1]`, `[2]` — while the
prompt demanded `(#84403)`, a number buried further down inside the chunk text. The model saw `[1]`
first and frequently emitted neither. And the positional index was parsed by **nothing**: the
verifier extracts `#(\d+)`, the UI highlights `(#N)`. Pure noise competing with the real key.

**Unnumbered sources get deliberately non-numeric keys:**

```python
(#commit-1a2b3c4d)        # commits
(#doc-kubelet-eviction)   # community docs
```

A positional integer there would be **indistinguishable from an issue number** to the verifier's
`r'#(\d+)'` — a fabricated citation by construction. The leading letter guarantees the numeric
extractor skips it, so the answer degrades to *"unverified but honest"* rather than *"verified
against the wrong document."*

## 7.3 Streaming

`api/routes/chat.py` streams via Server-Sent Events:

```
data: {"token": "Kubelet"}
data: {"token": " evicts"}
…
data: {"done": true, "answer": "<verified>", "sources": [...], "citations_valid": true, …}
```

**The streamed tokens are unverified by construction** — verification needs the complete answer.
So the `done` frame carries the *verified* answer, and the frontend must prefer it.

It didn't. `App.jsx` set `content: answer` from the accumulated streamed tokens rather than
`data.answer`. **Citation verification ran on every response, computed a corrected answer, and the UI
threw it away.** The third anti-hallucination layer was inert on the only path the UI uses.

## 7.4 Citation verification

```python
cited_numbers = set(int(n) for n in re.findall(r'#(\d+)', answer))
```

Then three outcomes:

**No citations** → `valid: True`, nothing to check.

**Cited a document never retrieved** → `valid: False`, caught **without an LLM call**. This is the
hard-hallucination check: the model invented a source. Objective, conclusive, free.

**Otherwise** → gather **every** retrieved chunk for each cited document and ask `gpt-oss-20b`
whether the chunk supports the specific claim, returning JSON.

### The bug that made this backwards

```python
chunk_map[num] = c.get('text', '')[:800]    # ← inside a loop
```

A document contributing four retrieved chunks was judged on **the last one alone**, truncated to 800
chars. Claims supported by an earlier chunk were reported unsupported and **stripped from the
answer** — a false rejection by construction, worsening as `TOP_N` grew from 7 to 18.

The fix groups all chunks per document under a shared budget:

```python
grouped.setdefault(num, []).append(c.get('text', ''))
per_doc   = max(VERIFY_CHARS // max(len(grouped), 1), 1200)
chunk_map = {num: '\n---\n'.join(texts)[:per_doc] for num, texts in grouped.items()}
```

### It fails open, deliberately

If the verifier's JSON can't be parsed, the answer is returned **unverified** rather than blocked —
a verifier outage shouldn't take the system down. But it's flagged:

```python
return {'valid': True, 'verified_answer': answer, 'verification_ran': False}
```

**Measured: 7.1% of answers ship unverified.** That is the honest size of the hole in the guarantee,
and the UI surfaces it.

---

# Part 8 — The three refusal layers

| Layer | Mechanism | Where | Out-of-scope refusals |
|---|---|---|---:|
| 1. Confidence gate | `best_score < CONFIDENCE_THRESHOLD` | `should_answer()` | 18/21 |
| 2. Cross-encoder | every candidate scores ≤ 0 → `rerank()` returns `[]` | `reranker.py` | **2/21** |
| 3. The model | emits `INSUFFICIENT_CONTEXT` | prompt rule 6 | 0/21 |
| | | **Total** | **20/21 = 95.2%** |

```python
def should_answer(best_score, chunks):
    return bool(chunks) and best_score >= THRESHOLD
```

Note this collapses **two** conditions. `bool(chunks)` is layer 2 — if the reranker dropped
everything, there is nothing to answer from regardless of score. Attributing them separately matters:
a question scoring **0.677** (well above the gate) with zero surviving chunks was refused by the
*cross-encoder*, and the metrics originally labelled it a gate refusal, erasing layer 2's entire
contribution.

**The layers are not redundant.** Two out-of-scope questions cleared the confidence gate and were
stopped by the cross-encoder. Layer 3 shows 0 on the negative set because the first two usually catch
out-of-scope questions first; it fires on *in-scope* questions where retrieval succeeded but the
chunks don't actually answer what was asked.

## 8.1 The gate's signal is its weak point

`best_score = vec_hits[0]['score']` — the **raw cosine similarity of the top vector hit**.

That comes from a bi-encoder that never saw the query and the chunk together. The **cross-encoder
does** see them jointly, is a far better relevance judge, and its verdict is **already computed** —
then discarded for gating purposes. Gating on reranker score is the clearest available improvement,
documented and deliberately unimplemented because it changes behaviour and needs measuring.

## 8.2 The threshold was chosen by sweeping, and the sweep was wrong twice

The inherited `0.40` was **inert**: identical coverage and leakage anywhere from 0.20 to 0.45. The
"confidence gate" was doing nothing and every refusal came from the other two layers.

An intermediate sweep reported 92% coverage at 0.60. **It measured the wrong quantity** — `max(score)`
over reranked chunks, while `retrieve()` gates on `vec_hits[0]['score']`. The two differ on *every*
question. Re-swept against the production gate on the reviewed question set:

| Threshold | In-scope answered | Out-of-scope leaked past gate |
|---:|---:|---:|
| 0.55 | 97.3% | 31.8% |
| **0.60** | **91.9%** | **18.2%** |
| 0.65 | 81.1% | 9.1% |

0.60 is the knee: −5.4 coverage buys −13.6 leakage, and past it the trade inverts.

---

# Part 9 — A complete request trace

One real question, measured end to end.

### Question
> *"Why does kubelet evict pods under disk pressure?"*

### 1. Rewrite — 1 LLM call, `qwen3.8-27b`
```
"kubelet disk pressure pod eviction rationale"
```

### 2. Two searches, in parallel conceptually
```
vector : 20 hits in  923 ms, top cosine 0.7619
bm25   : 20 hits in 2476 ms, top score 26.33
overlap:  6 shared · 14 bm25-only · 14 vector-only
```
**Only 6 of 20 agree.** That 14/14 disagreement is the empirical case for hybrid search.

### 3. RRF fusion
```
34 unique ids  (20 + 20 − 6 overlap)
```

### 4. Hydration
```
14 BM25-only payloads fetched from Qdrant in 64 ms
```
These 14 were silently discarded before the fix.

### 5. Rerank
```
34 candidates → 18 kept, in 688 ms
```

### 6. Context assembled
```
4,674 tokens · 16,040 chars · 12 distinct documents
types: 9 issues, 8 PRs, 1 commit
keys : (#27199) (#38836) (#45896) (#54314) (#73572) (#105358) …
```

### 7. Generation — 1 LLM call, `gpt-oss-120b`, streamed
```
Kubelet evicts pods when the node is under disk pressure because the node-level
"DiskPressure" condition signals that the disk resource is exhausted and must be
reclaimed (#99095). The eviction manager ranks pods and evicts the greediest first
to free space (#27199). …
```

### 8. Verification — 1 LLM call, `gpt-oss-20b`
Groups all retrieved chunks per cited document, checks support, strips unsupported citations.

### 9. Response
```json
{"done": true, "answer": "<verified>", "sources": [6 deduped],
 "is_fallback": false, "citations_valid": true, "verification_ran": true,
 "best_score": 0.7619}
```

**Totals: 3 LLM calls, ~5,400 tokens, ~4 s of retrieval + generation latency.**

Note the cost shape: retrieval is ~4 seconds of *local* compute, dominated by BM25's 2.5 s and the
cross-encoder's 0.7 s. Vector search is 0.9 s. The 34 ms embedded-Qdrant figure from §5.4 is the
query itself; the rest is embedding the query and Python overhead.

---

# Part 10 — The API and data model

## 10.1 Endpoints

| Method | Path | Notes |
|---|---|---|
| GET | `/health` | liveness |
| POST | `/api/chat` | SSE stream, or plain JSON on gate refusal. Rate-limited per IP |
| POST | `/api/register` · `/api/login` | returns a bearer token, 7-day session |
| GET | `/api/me` | resolves the token to a user |
| GET/POST/DELETE | `/api/history` | per-user chat history, **identity from the token** |
| GET/DELETE | `/api/admin/queries` | out-of-scope query analytics |
| POST | `/api/index` · `/api/webhook/github` | Celery-backed, no worker deployed |

### The authorization bug worth knowing

These endpoints originally took `user_id` **as a request parameter with no authentication**:

```bash
curl 'http://localhost:8000/api/history?user_id=5'           # any user's history
curl -X DELETE 'http://localhost:8000/api/history?user_id=5'  # delete it
```

The `sessions` table existed and issued tokens, but only `/api/me` ever validated one. Fixed with a
bearer-token dependency (`api/deps.py`), and `user_id` **removed from the request surface** so it
cannot be passed at all. The fix isn't "check the token" — it's "make the insecure call impossible to
express."

## 10.2 Chunk payload schema

Identical in Qdrant payload and throughout retrieval:

```python
{
  'text':        str,          # the chunk, prefix included
  'source_type': str,          # 'issue' | 'pr' | 'commit' | 'kep' | 'community'
  'repo':        str,
  'number':      int | None,   # issue/PR/KEP number — None for commits & docs
  'title':       str,
  'url':         str,
  'labels':      list[str],
  'state':       str,
  'chunk_index': int,          # position within its source document
  'file_path':   str | None,
}
```

`number` being `None` is what drives the non-numeric citation keys in §7.2.

## 10.3 SQLite tables

| Table | Purpose |
|---|---|
| `chunks` | metadata mirror of Qdrant, keyed by the shared UUID |
| `bm25_index` | `chunk_id → text`, the full BM25 corpus |
| `users` · `sessions` | auth; bcrypt hashes, 7-day tokens |
| `chat_history` | per-user conversation persistence |
| `out_of_scope_queries` | upsert-by-text counter — questions the system couldn't answer |

`out_of_scope_queries` is the interesting one: **a product feedback loop.** Questions the system
fails on tell you what to index next.

---

# Part 11 — How it's evaluated

**Files:** `eval/build_golden.py`, `eval/run_eval.py`, `eval/run_generation.py`

## 11.1 The golden set

37 questions, each with a known ground-truth document. Built semi-automatically: pick documents with
substantial discussion, have an LLM draft a question answerable only from that document, record the
document id as truth.

**The LLM writes the question; it does not decide the answer.** Ground truth is a document id, which
is a fact rather than a judgement. That keeps the labels trustworthy even though generation is
automated.

Then **mechanical checks** reject self-referential questions (*"Why does **this PR**…"* — unaskable
by someone who hasn't already found the answer), and then **a human reads all of them.**

### The human review mattered more than any code change

It rejected **13 of 50**: six about kube-proxy/scheduler/apiserver (in the corpus legitimately via PR
back-references, but not kubelet design questions), three with lexical overlap above 0.85, and four
leaking their own source.

That **flipped the project's headline finding.** Vector/BM25 went from 0.820/0.840 to **0.838/0.811**
— the ordering reversed.

A judgement no regex could make: two questions both named internal functions, but
`IsLikelyNotMountPoint` **surfaces in kubelet logs during real debugging** while `makeHostsMount`
appears only if you've read the source. One is a question an engineer would ask; the other is leaked
from its answer.

## 11.2 Metrics

**Recall@k** — does a chunk from the ground-truth document appear in the top k? Measures *finding*.

**MRR** (mean reciprocal rank) — `1/rank` of the first correct hit, averaged. Measures *ranking*. A
reranker should move MRR and leave recall alone, since it reorders a fixed set and cannot retrieve
anything new. Observed exactly that: recall unchanged at 0.919, MRR 0.767 → 0.792.

**Correct-refusal rate** on a negative set of 22 deliberately out-of-scope questions.

**Citation grounding** — of emitted `(#N)`, how many refer to a document actually in context.
Objective, no judgement needed. **Measured 17/17 = 100%.**

## 11.3 Controlling for the eval's own bias

Questions drafted *from* a document inherit its vocabulary, which flatters keyword search. Rather
than disclaim that, the harness **measures** it: every question carries a `lexical_overlap` score and
the set is split at the median.

| Stratum | mean overlap | Vector R@10 | BM25 R@10 |
|---|---:|---:|---:|
| Low overlap | 0.55 | **0.778** | **0.778** |
| High overlap | 0.74 | **0.895** | 0.842 |

**Exactly tied** where overlap is low. And the control that makes it trustworthy: **vector recall is
identical across both strata**, exactly as it should be if embeddings are insensitive to literal word
overlap. The metric moves BM25 and leaves vector flat, so it measures what it claims.

## 11.4 Results

| | |
|---|---|
| Recall@10, hybrid + reranker | **0.919** |
| Recall@18 (what reaches the model) | **0.940** |
| Answers emitting ≥1 citation | **93%** (from 30%) |
| Citations grounded in retrieved context | **17/17 = 100%** |
| Out-of-scope refused, all layers | **20/21 = 95.2%** |
| Verifier fails open | **7.1%** |

---

# Part 12 — The lesson the project actually taught

**Nine bugs. Every one was two components silently disagreeing about a number, each locally correct.**

| | said | but | |
|---|---|---|---|
| chunker | 500 tokens | embedder read | 256 |
| prefix | grew to 221 | of a budget of | 256 |
| `top_n=7` | calibrated at 671-token chunks | chunks became | 256 tokens |
| verifier | saw 1 chunk/doc | model saw | 4 |
| threshold `0.40` | inherited from | nothing — it was inert | |
| RRF | computed BM25 ranks | then a filter discarded them | |

These are **interface bugs** — implicit contracts between pipeline stages that nobody wrote down, so
nothing could check them. They are invisible to code review *by construction*, because each stage
reads correctly in isolation.

**Three of the nine were in the measurement code.** A wrong harness doesn't fail loudly; it reports
confident numbers about a system that doesn't exist. The threshold was tuned against a quantity
production never computes. Citations were counted *after* the verifier stripped them, conflating "the
model didn't cite" with "the verifier rejected it" — opposite failures that look identical.

That's why the tests assert **relationships** rather than values:

```python
assert MAX_TOKENS <= EMBED_MAX_TOKENS
assert EMBED_MAX_TOKENS <= SentenceTransformer('all-MiniLM-L6-v2').max_seq_length
```

The second checks a hand-written constant against the *actual model*, so swapping embedders cannot
silently reintroduce the mismatch.

> **An unexpected benchmark result is a hypothesis about your own code before it is a finding about
> the world.** Every time a surprising number was treated as a *result*, it was wrong. Every time it
> was treated as a *symptom*, it found a bug.

---

# Part 13 — Known open items

Stated plainly, because naming them reads as more rigorous than pretending they don't exist.

| | Status |
|---|---|
| **Query rewriting measures negative** | Confounded by eval bias; needs human-written questions |
| **Gate uses cosine, not reranker score** | Best available improvement; changes behaviour → needs measuring |
| **BM25 resident in memory, stale until restart** | FTS5 fixes both; changes retrieval → needs an ablation |
| **Commit messages may dilute retrieval** | Untested; the ablation row is written |
| **Citation *support* unmeasured** | Grounding is objective and measured; support needs human scoring |
| **Negative set unreviewed** | One "leak" may be a set flaw — the golden set had 13 |
| **Threshold tuned on its own test set** | Mild overfitting; a held-out split would fix it |
| **Payload indexes unused** | Built in Qdrant, never queried |
| **Celery / webhook path untested** | Code exists, no worker ever started |

---

# Part 14 — Interview concept checklist

Be able to explain each of these in two or three sentences:

**Retrieval**
- Why hybrid rather than vector-only — and that the retrievers are *near-tied individually* yet fusion gains 0.08
- What RRF does and why it uses rank rather than score; why `k=60` flattens the curve
- Bi-encoder vs cross-encoder: when each is computed, and why that dictates retrieve-then-rerank
- Why retrieving *more* candidates made results worse here

**Chunking**
- Why 256 tokens is dictated by the model, not chosen
- Why conversation threads are harder to chunk than prose, and what the overlap preserves
- Why the metadata prefix sits inside the embedded text, and why it must be budgeted

**Generation**
- Why the citation label and the citation key must be the same string
- Why unnumbered sources get non-numeric keys
- Why the streamed tokens are unverified and the `done` frame carries the verified answer
- Why the verifier fails open, and what 7.1% means

**Refusal**
- The three layers, and a concrete case where each fired
- Why `should_answer` collapses two conditions, and why attributing them separately mattered
- Why the gate's signal (raw cosine) is its weak point

**Evaluation**
- Recall vs MRR, and why a reranker should move only one
- How the lexical-overlap bias was *measured* rather than disclaimed, and what the control was
- Why the human review of the question set overturned the headline finding

**The meta-point**
- Nine bugs, all interface mismatches; three in the measurement code
- Why tests assert relationships rather than values
