# RepoMind — full project walkthrough

Everything about this project in one document: the problem, how the system works end to end, every
significant decision and why, what went wrong, what was achieved and to what degree, and what is
still open.

---

# 1. The problem

**`git blame` tells you who changed a line and when. It never tells you why.**

In a codebase the size of Kubernetes, the reasoning behind a design decision does not live in the
code. It lives in a 2019 argument spread across four GitHub issues, two pull request reviews and an
enhancement proposal — written by people who have since moved on, and effectively unsearchable.

Concretely: *"Why does kubelet reject new pods when a node is under disk pressure?"* The answer
exists. Finding it by hand means reading roughly forty issue threads spanning six years, because
GitHub search matches keywords, not reasoning, and the relevant discussion rarely contains the words
you would think to search for.

This matters because the alternative to finding the answer is **guessing** — and a plausible wrong
explanation of why a design exists propagates. Someone repeats it in a code review, and now the wrong
rationale is institutional knowledge.

**Scope chosen:** `kubernetes/kubernetes`, the kubelet subsystem — everything that ever touched
`pkg/kubelet`, plus the sig-node enhancement proposals. Narrow enough to index completely, rich
enough that the questions are genuinely hard.

---

# 2. What the system does

Answers questions about kubelet design from the project's own discussion, **with citations**, and
**refuses when it doesn't know**.

The refusal is not politeness. It is the core design constraint, and it drove most of the
architecture — see §5.

---

# 3. How it works

## 3.1 Corpus — 14,655 documents, 113,047 chunks

| Source | Documents | Chunks |
|---|---:|---:|
| Pull requests (descriptions, reviews, inline review comments, comments) | 6,245 | 77,332 |
| Issue threads | 1,987 | 28,082 |
| Commit messages (mainline) | 6,272 | 6,272 |
| KEPs (sig-node enhancement proposals) | 126 | 930 |
| sig-node community docs | 25 | 431 |

The PR and issue sets are **complete** for the subsystem, not sampled.

## 3.2 Ingestion — enumeration in git, content from the API

The naive approach is to page through the repository's pull requests and keep the ones touching
`pkg/kubelet`. That means walking 100,000+ PRs, and you still end up with whichever ones you reached
first — a sample, not a corpus, and a different one each run.

`ingestion/scraper_v2.py` moves the expensive enumeration step off the API entirely:

| Stage | Method | Cost |
|---|---|---|
| Commits | Blobless clone, `git log --first-parent -- pkg/kubelet` | 262 MB, 48 s, **zero API calls** |
| PR discovery | PR numbers parsed from merge-commit messages | free — 6,268 found, only **4** needed the API |
| PR fetch | Fetched by number, batched 12 per aliased GraphQL query | ~2,200 points, ~20 min |
| Issues | Date-sliced label search **plus** `fixes #N` back-references from PRs | — |
| KEPs | `git clone kubernetes/enhancements`, split on `##` headings | zero API calls |

Three things this buys:

- **6,245 PRs instead of 943** — 6.6× the coverage of the original scraper, and reproducible.
- **Back-references nearly doubled the issue corpus.** Label search on `area/kubelet` found 889;
  `fixes #N` references from PR bodies found ~1,098 more. **Over half the kubelet issues were never
  labelled.** Relying on labels alone would have silently lost the majority, with no error to notice.
- **Truncation is measured, not assumed.** Every GraphQL connection requests `totalCount` (free), so
  the scrape ends with a report of exactly which PRs had discussion clipped. A second pass re-fetches
  only those at higher limits. Final state: **0 PRs clipped.**

**`--first-parent` matters more than it looks.** 13,562 commits touch the path, but 7,290 are
intra-PR branch commits — "rebase", "fix lint", "address review feedback". They belong to PRs already
captured by their merge commit. Including them filled over half the commit corpus with
work-in-progress noise *and* would have cost ~200 wasted API round-trips resolving them.

## 3.3 Corpus filtering — 65% of PR comments are noise

Kubernetes is heavily automated. Measured across 12,016 sampled PR comments:

| | share |
|---|---:|
| Bot-authored (`k8s-ci-robot`, `codecov`, …) | 19.9% |
| Prow slash commands (`/lgtm`, `/approve`, `/retest`) | 27.9% |
| Sub-60-character replies ("+1", "ping", "done") | 17.2% |
| **Substantive discussion** | **35.0%** |

Only 35% carries discussion, though it holds 49% of the text. The rest produced thousands of
near-identical `/lgtm` chunks — high-frequency, low-content strings that match many queries and
inform none of them. Filtering took the index from 627k chunks to 431k, and a later prefix fix
brought it to 113k.

## 3.4 Chunking — the genuinely hard part

An issue thread is **not prose**. It is several people disagreeing across time, and *the disagreement
is the signal*. Fixed-size splitting severs a rebuttal from the claim it rebuts, and the resulting
chunk reads as consensus when it was an objection. One-chunk-per-comment loses the thread entirely:
*"I agree with the above, but only for guaranteed pods"* is meaningless alone.

The approach: accumulate conversation units (body, each review, each inline comment, each comment)
until a **256-token** budget is hit, then flush with a **32-token overlap** carried into the next
chunk. Units larger than the budget are split rather than emitted whole.

Two details that turned out to be load-bearing:

- **256 tokens is not a taste decision.** It is `all-MiniLM-L6-v2`'s `max_seq_length`. See §6.2 for
  what happened when it wasn't.
- **The metadata prefix lives inside the embedded text**, not just in the payload:
  `[ISSUE #84403 - CLOSED] / Title: … / Labels: …`. A chunk stripped of its issue number cannot be
  cited, and citation is the product. It is kept to ≤64 tokens — Kubernetes PRs carry ~10 process
  labels and very long file paths, and an unbudgeted header once reached 221 of the 256 tokens.

KEP sections are chunked differently — one chunk per `##` heading, no splitting. An "Alternatives"
section is already a self-contained argument.

## 3.5 Retrieval

```
question
  → rewrite            qwen/qwen3.8-27b (cheap, no reasoning overhead)
  → vector search      Qdrant, MiniLM 384-d cosine, k=20
  → BM25 search        rank-bm25 over full chunk text in SQLite, k=20
  → RRF fusion         reciprocal rank fusion, k=60
  → hydrate            BM25-only hits fetched from Qdrant by id
  → cross-encoder      ms-marco-MiniLM-L-6-v2, keep top 18
  → 18 chunks ≈ 4,600 tokens of context
```

**Why hybrid:** embeddings smear exact identifiers. A question about `eviction_manager.go` maps into
a region containing "generally eviction-related content" — right in spirit, wrong in practice. Flag
names (`--eviction-hard`), error strings and API fields hit the same failure. Lexical matching
handles those; semantic search handles paraphrase; neither covers both.

**Why a cross-encoder:** bi-encoders compress a document into one vector before ever seeing the
query, which is what makes them fast enough for 113k chunks and also what makes them imprecise at the
very top. Only 18 chunks reach the model, so precision *at that boundary* determines the answer.

**Why 18 and not 7:** this is a context-**volume** decision, not a count. At 256-token chunks, 18
gives ~4,600 tokens. See §6.3.

## 3.6 Generation and verification

```
18 chunks → strict system prompt → openai/gpt-oss-120b (streamed)
          → citation verification → unsupported citations stripped → answer
```

The system prompt imposes seven rules: every sentence traceable to a chunk; an inline citation per
claim; no citing loosely-related sources; state what the context does *not* cover; no inference; emit
`INSUFFICIENT_CONTEXT` when it cannot answer; never use training knowledge.

Each context block opens `Cite as (#84403) | <url>` — **the label is the citation key**. Sources
without a number (commits, community docs) get a deliberately *non-numeric* key like
`(#commit-1a2b3c4d)`: a positional integer there would be indistinguishable from an issue number to
the verifier's `r'#(\d+)'`, i.e. a fabricated citation by construction.

Verification gathers **every** retrieved chunk for each cited document and asks a smaller model
(`gpt-oss-20b`) whether the chunk supports the specific claim. Unsupported citations are stripped
before the answer is displayed.

**Models are routed by task**, measured rather than assumed:

| Task | Model | Why |
|---|---|---|
| Answering | `openai/gpt-oss-120b` | Seven strict rules; needs the strong model |
| Verification | `openai/gpt-oss-20b` | Constrained classification → JSON. 3–5× faster, equally accurate on test cases |
| Query rewriting | `qwen/qwen3.8-27b` | Trivial, runs every query. 7 tokens, no reasoning overhead |

`gpt-oss-20b` spends 175–364 tokens *reasoning* about a five-word query rewrite. Qwen just does it.

---

# 4. The three refusal layers

**Why refusing beats answering** — this is domain reasoning, not general caution. If you ask why a
design exists and get a fluent, plausible, wrong answer, you cannot tell it is wrong; that is exactly
the knowledge you lacked. An unanswered question sends you to the source. A confidently wrong one
does not.

| Layer | Mechanism | Out-of-scope refusals |
|---|---|---:|
| 1. Confidence gate | top vector score < `CONFIDENCE_THRESHOLD` (0.60) | 18/21 |
| 2. Cross-encoder | every candidate scores ≤ 0 | **2/21** |
| 3. The model | emits `INSUFFICIENT_CONTEXT` | 0/21 |
| | **Total** | **20/21 = 95.2%** |

**The layers are not redundant.** Two questions cleared the confidence gate and were stopped by the
cross-encoder — the gate alone would have passed them. Layer 3 shows 0 on the negative set because
the first two usually catch out-of-scope questions first; it fires on *in-scope* questions where
retrieval succeeded but the chunks don't actually answer the question.

The threshold was chosen by sweeping it, not by feel. The inherited 0.40 was **inert** — identical
coverage and leakage anywhere from 0.20 to 0.45. 0.60 is the knee: 91.9% coverage, 18.2% gate
leakage; past it the trade inverts.

---

# 5. Results

## 5.1 Retrieval — 37 human-reviewed questions

| Configuration | Recall@10 | MRR |
|---|---:|---:|
| Vector only | 0.838 | 0.618 |
| BM25 only | 0.811 | 0.605 |
| RRF hybrid | 0.919 | 0.767 |
| RRF + cross-encoder | 0.919 | 0.792 |
| RRF + cross-encoder, no query rewrite | **0.973** | **0.865** |

**Hybrid is the largest single win.** The two retrievers sit within 0.03 of each other alone, yet
fusion reaches 0.919 — they fail on *different* questions. That is a stronger argument for fusion
than either being superior.

**The cross-encoder behaves exactly as a reranker should:** recall unchanged (it reorders a fixed
candidate set and cannot retrieve anything new), MRR up 0.025.

Stratified by how much vocabulary each question shares with its source document:

| Stratum | mean overlap | Vector R@10 | BM25 R@10 |
|---|---:|---:|---:|
| Low overlap | 0.55 | **0.778** | **0.778** |
| High overlap | 0.74 | **0.895** | 0.842 |

**Exactly tied** where overlap is low — which is the honest comparison, since questions drafted from
a document inherit its vocabulary and that flatters keyword search.

## 5.2 Citations and refusal — end to end

| | Before fixes | After |
|---|---:|---:|
| Answers emitting ≥1 citation | 30% | **93%** |
| Citations grounded in retrieved context | — | **17/17 = 100%** |
| Citations surviving verification | 0/2 — both stripped | **16/17 = 94%** |
| Out-of-scope refused (all layers) | — | **20/21 = 95.2%** |
| Verifier failed to parse (fails open) | — | **7.1%** |

**The model never invented a source.** 17 citations, every one pointing at a document actually in its
context. That is the failure mode that would matter most — a fabricated source is indistinguishable
from a real one to a reader.

## 5.3 A tested non-change

With 18 chunks reaching the model, the reranker sees ~40 candidates and keeps 18. The obvious
optimisation is to retrieve more. Measured, that is **wrong**:

| k | pool | Recall@10 | Recall@18 | MRR |
|---:|---:|---:|---:|---:|
| **20 (current)** | 40 | **0.880** | **0.940** | **0.761** |
| 40 | 80 | 0.880 | 0.880 | 0.755 |
| 60 | 120 | 0.860 | 0.900 | 0.747 |

Every metric flat or worse as the pool grows. `ms-marco-MiniLM-L-6-v2` is small and its precision
degrades as distractors multiply. `k=20` stays — validated rather than inherited. "More candidates is
better" is a property of *strong* rerankers, not of reranking.

---

# 6. What went wrong — eight bugs, and how they were found

**Every significant bug had the same shape: two components silently disagreeing about a number, each
locally correct.** These are *interface* bugs — implicit contracts between pipeline stages that
nobody wrote down, so nothing could check them. They are invisible to code review by construction.

**Three of the eight were in the measurement code itself.**

## 6.1 RRF computed BM25 rankings, then discarded them

`retrieve()` fused vector and BM25 rankings, then filtered candidates with `if i in vec_map` —
dropping every document BM25 found that the embedding search missed. The entire reason to run BM25
was thrown away one line after computing it.

After the fix, on `PLEG relist latency`, **4 of the 7 chunks reaching the model** came from BM25-only
hits. Before: zero, always.

## 6.2 The embedder was reading a third of each chunk

| | |
|---|---|
| `all-MiniLM-L6-v2` max_seq_length | **256 tokens** |
| Chunker target | 500 tokens |
| Actual mean chunk produced | **671 tokens** |
| Chunks over the model limit | **88%** |

Three defects compounded: the budget was double the model's limit; the accumulate loop never *split*
an oversized unit (one chunk reached 56,086 tokens); and the metadata prefix had grown to **221 of
256 tokens**, leaving 25 for content.

**This produced a plausible wrong conclusion.** The first evaluation said *BM25 beats embeddings by
0.20 recall* — a believable story ("keyword search wins on technical text"). It was tempting to write
up. Instead:

| | Before | After | Δ |
|---|---:|---:|---:|
| **Vector only** | 0.640 | **0.820** | **+0.180** |
| BM25 only | 0.840 | 0.840 | **0.000** |

**BM25 did not move by a thousandth** — it reads full text from SQLite and was never truncated. That
control is what turned a plausible story into a confirmed diagnosis.

## 6.3 Fixing chunk size starved the context

`rerank(top_n=7)` was calibrated when chunks were ~671 tokens — about 4,600 tokens of context.
Halving chunks to 256 cut that to **1,787** without changing a line of generation code. The parameter
is expressed as a *count*; what matters is *volume*.

The symptom was easy to misread: the model began answering `INSUFFICIENT_CONTEXT` to **6 of 15
in-scope questions — and in 5 of those the ground-truth document was sitting in the context it
received.** Without the measurement that looks like an over-strict prompt. It was a starved one.

## 6.4 The verifier judged citations on a quarter of the evidence

`verify_citations` built its evidence with `chunk_map[num] = text[:800]` **inside a loop**, so a
document contributing four retrieved chunks was judged on the last one alone, truncated. Claims
supported by an earlier chunk were reported unsupported and **stripped from the answer**.

Same drift: written when `top_n=7` meant ~1 chunk per document reached the model. At 18, a single
issue routinely contributes 4. And the failure mode was invisible, because a stripped citation looks
exactly like a model that failed to cite.

## 6.5 62% of the index was tokenizer-mangled

`truncate_to_tokens` did encode → slice ids → decode. `all-MiniLM-L6-v2` is an **uncased** model, so
decoding returned lowercase with punctuation re-spaced:

```
[ISSUE #100005 - CLOSED]   →   [ issue # 100005 - closed ]
```

Every chunk over budget — **62.4% of the index** — was stored that way. The citation key stopped
matching `r'#(\d+)'` because of the inserted space, so two thirds of chunks presented an identifier
the model could not copy and the verifier could not parse. This was the dominant cause of the 30%
citation rate, and it was introduced while fixing 6.2.

> A tokenizer is a **measuring instrument, not a transformation.** Use it to decide where to cut;
> never let its output become the text you store.

## 6.6 Two competing citation schemes

`format_context` labelled chunks `[1]`, `[2]` while the prompt demanded `(#84403)` — a number buried
inside the chunk text. The positional index was parsed by **nothing**: the verifier extracts
`#(\d+)`, the UI highlights `(#N)`. Pure noise competing with the real key.

## 6.7 *(harness)* Citations counted after the verifier stripped them

The eval computed citation counts from the **post-verification** answer, so a citation the verifier
removed vanished from the count. This conflates two opposite failures — "the model didn't cite" and
"the verifier rejected the citation" — and would have reported the citation fix as ineffective when
it had worked.

## 6.8 *(harness)* The threshold was tuned on a quantity production never computes

The sweep computed the gate as `max(score)` over reranked chunks. `retrieve()` gates on
`vec_hits[0]['score']`. **They differ on every question**, with the harness running 0.05–0.15 high.
The reported "92% coverage, 13.6% leakage" at 0.60 was really **86% / 27.3%** — it was refusing one
in seven in-scope questions before the model ever saw them.

## 6.9 And the golden set itself was wrong

The 50 LLM-drafted evaluation questions had never been read by a human. Review rejected **13**:

- 6 about kube-proxy, the scheduler or apiserver — they entered the corpus legitimately via PR
  back-references, but are not kubelet design questions
- 3 with lexical overlap above 0.85
- 4 leaking their own source — one named two internal function symbols verbatim; two were
  dependency-bump trivia

That **flipped the vector-vs-BM25 ordering outright** (0.820/0.840 → 0.838/0.811). So "BM25 beats
embeddings on this corpus" was two artifacts stacked: a truncation bug worth ~0.18, and an unvetted
question set worth the rest. Never a property of the retrievers.

A judgement worth keeping: two questions both named internal functions, but `IsLikelyNotMountPoint`
surfaces in kubelet logs during real debugging while `makeHostsMount` only appears if you have read
the source. One is a question an engineer would ask; the other is leaked from its answer. **No regex
makes that call.**

---

# 7. To what degree is it solved

**Solved and measured:**

- Retrieval finds the right document in **91.9%** of cases (97.3% at the 18-chunk cutoff the model
  actually sees)
- **93%** of answers carry citations; **100%** of those citations are grounded in genuinely retrieved
  documents
- **95.2%** of out-of-scope questions are refused, across three independent layers
- The corpus is **complete** for the subsystem rather than sampled

**Partially solved:**

- **Citation *support*** — grounding is measured objectively; whether the cited text actually
  *supports* the claim is judged by an LLM verifier and has not been human-scored. Grading the grader
  requires a person.
- **The verifier fails open on 7.1% of answers** — roughly one in fourteen ships unverified. Flagged
  in the response and surfaced in the UI, but it is a real limit on the guarantee.
- **n=14 answered** for the citation metrics — enough to establish 30% → 93% is real, not enough to
  state the rate to two significant figures. The free-tier quota refills ~8,300 tokens/hour against
  ~5,400 per generation.

**Not solved / known open:**

- **Query rewriting measures negative** (+0.033 recall when removed) and costs an LLM call per
  question — but the eval's construction bias penalises paraphrasing, so the measurement is biased
  against it. Needs a human-written question set to settle.
- **The gate uses raw cosine, not the reranker's judgement.** `vec_hits[0]['score']` comes from a
  bi-encoder that never saw query and chunk together; the cross-encoder does, is a far better
  relevance judge, and its verdict is already computed and then discarded for gating.
- **BM25 holds all 113k chunks in memory** and never refreshes — chunks indexed after startup are
  invisible to keyword search until restart. SQLite FTS5 would fix both; it changes retrieval, so it
  needs measuring as an ablation.
- **The negative set needs the review the golden set got.** One "leaked" question —
  *"Why does Kubernetes use a declarative API instead of imperative commands?"* — may have been
  answered correctly from genuinely relevant kubelet discussion.
- **Commit messages are the weakest source.** Untested whether they help retrieval or dilute it.
- **The threshold was tuned on the set it is reported against** — mild overfitting; a held-out split
  would fix it.

---

# 8. Explaining this to an interviewer

## The 90-second version

1. **Problem** — design rationale in large OSS projects is buried in issue threads. `git blame` gives
   you who and when, never why.
2. **Approach** — index the discussion around one subsystem, retrieve hybrid, rerank, answer only
   with citations.
3. **The hard part** — making it refuse. A confident wrong answer about why a design exists is worse
   than no answer, because it propagates.
4. **Evidence** — the ablation table, 95.2% refusal, 100% citation grounding.
5. **Limits** — one subsystem, one repo, and the open questions above.

## The strongest story you have

> *"My evaluation said BM25 beat my embedding model by 0.20 recall. That's a publishable-sounding
> result — keyword search winning on technical text. I investigated instead of writing it up, and
> found the embedder was reading 256 tokens while my chunker emitted 671, so it was seeing a third of
> each chunk. Fixing it moved vector recall from 0.64 to 0.82 and left BM25 at exactly 0.840 — which
> is the control that proved the diagnosis, since BM25 reads full text and was never truncated. Then
> a human review of my question set flipped the ordering entirely. On clean questions the two are
> exactly tied."*

Very few portfolio projects can tell that story, and it demonstrates the thing interviews are
actually probing for: **an unexpected result is a hypothesis about your own code before it is a
finding about the world.**

## Likely questions

**Why hybrid retrieval?** Point at the ablation. Then the concrete failure: a question naming
`eviction_manager.go` that vector search smeared into generic "eviction stuff". And the honest
finding — the two retrievers are near-tied individually; fusion wins because they fail on *different*
questions.

**How do you know it doesn't hallucinate?** 95.2% refusal on a held-out negative set across three
layers, and 100% citation grounding — the model never cited a document it wasn't shown. Then name the
limit: the verifier fails open on 7.1% of answers, and I know that because I measured it.

**Why 0.60 for the threshold?** The sweep. And the more interesting answer: the inherited 0.40 was
inert — identical behaviour from 0.20 to 0.45 — so the "confidence gate" was doing nothing and every
refusal was coming from the other two layers.

**What was the worst bug?** The tokenizer round-trip. 62% of the index stored as lowercase with
re-spaced punctuation, which broke the citation key. Introduced by me, while fixing a different bug,
and only found because a payload looked wrong in an unrelated check.

**What would you do differently?** Build the evaluation before the architecture — and treat the
harness as production code. Three of the eight bugs were in my measurement code, and a wrong harness
doesn't fail loudly; it reports confident numbers about a system that doesn't exist.

**How would you scale to 100 repos?** Per-repo collections (already the naming convention), FTS5 or
sparse vectors instead of a resident BM25 index, async indexing behind a real job queue, Postgres
once writes go concurrent.

---

# 9. Running it

```bash
cp .env.example .env          # add GROQ_API_KEY and GITHUB_TOKEN
docker run -d -p 6333:6333 -v "$PWD/qdrant_storage:/qdrant/storage" qdrant/qdrant
./setup.sh                    # venv, deps, schema, scrape, index
uvicorn api.main:app --port 8000
cd repomind-ui && npm install && npm run dev
```

**Evaluation:**

```bash
python eval/build_golden.py generate    # draft questions from the corpus
python eval/build_golden.py fix         # regenerate mechanically defective ones
python eval/build_golden.py review      # human pass — do not skip this
python eval/build_golden.py accept
python eval/run_eval.py all             # retrieval ablations (no LLM calls if cached)
python eval/run_generation.py run       # citations + refusal (rate-limited, resumable)
```

**Tests:** `pytest tests/ -q` — 36 tests, each pinning behaviour that either caused a real bug here
or would fail invisibly if it regressed. Four assert *relationships* rather than values
(`MAX_TOKENS <= EMBED_MAX_TOKENS`, and that constant against the real model), because that is the
class of bug that accounted for most of §6.

---

# 10. Document map

| File | What it holds |
|---|---|
| `README.md` | The pitch: problem, results, how it works |
| `PROJECT.md` | This document — the full walkthrough |
| `DECISIONS.md` | Every design decision, the alternative rejected, and why |
| `agent.md` | Architecture brief for working in the codebase |
| `eval/results.md` | Retrieval analysis and the bug narrative |
| `eval/citation_results.md` | Citation and refusal measurements |
| `eval/metrics.md` | Auto-generated tables (regenerated each run) |
| `DEPLOY.md` | Deployment procedure |
| `PHASE0.md` | The remediation plan this work followed |
