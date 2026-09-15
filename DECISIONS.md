# Design decisions

Why this system is built the way it is, including the things that turned out to be wrong.

---

## 1. Hybrid retrieval, not pure vector search

**Decision:** run BM25 alongside vector search and fuse with Reciprocal Rank Fusion.

**Alternative rejected:** embeddings alone, which is the default for most RAG systems and is simpler.

Embeddings smear exact identifiers. A question about `eviction_manager.go` gets mapped into a region
of vector space containing "generally eviction-related content" — which is right in spirit and wrong
in practice, because the person asking wants the discussion about *that file*. The same failure hits
flag names (`--eviction-hard`), error strings, and API field names. Lexical matching handles those
precisely, and semantic search handles paraphrase; neither covers both.

**Measured** on the 37-question human-reviewed golden set: vector-only recall **0.838**, BM25-only
**0.811**, fused **0.919**. The two retrievers are within 0.03 of each other alone, yet fusion gains
0.08 over the better of them — they fail on *different* questions. That is a stronger argument for
hybrid search than either being superior, and it is the argument that survived three rounds of the
numbers moving.

Split by how much vocabulary each question shares with its source document:

| Stratum | Vector R@10 | BM25 R@10 |
|---|---:|---:|
| Low overlap | 0.778 | 0.778 |
| High overlap | 0.895 | 0.842 |

**Exactly tied** where lexical overlap is low. See §4a for why an earlier version of this document
claimed BM25 won by 0.20.

---

## 2. A cross-encoder reranker, despite the latency

**Decision:** rerank the fused candidates with `cross-encoder/ms-marco-MiniLM-L-6-v2` and keep the
top 7.

**Alternative rejected:** pass the top-k fused results straight to the model.

Bi-encoders compress a document into a single vector before ever seeing the query, which is what
makes them fast enough to search 113,000 chunks — and also what makes them imprecise at the very top
of the ranking. Only 7 chunks reach the model, so precision *at that boundary* determines the answer.
A cross-encoder scores each (query, chunk) pair jointly and is far better at that last sort.

It also turned out to be a second refusal path. The question *"How does the etcd raft implementation
handle leader election?"* scored **0.415** — above the then-current 0.40 confidence threshold — but the reranker
assigned every candidate a negative score and dropped them all, so the system refused anyway. The
threshold alone would have let that one through.

---

## 3. Metadata baked into the chunk text, not just the payload

**Decision:** every chunk begins with a header — `[ISSUE #84403 - CLOSED]`, `[KEP-2400 - implemented]`
— that is part of the embedded text, not only of the stored payload.

**Alternative rejected:** keep metadata purely in Qdrant's payload, where it's cheaper.

A chunk stripped of its issue number cannot be cited, and citation is the product. Putting the header
in the text means it is embedded (so "KEP" or "issue" in a query has something to match), it survives
into the model's context, and the model can cite without a separate lookup.

---

## 4. Refusing beats answering

**Decision:** three independent refusal paths, and a citation verifier that strips unsupported claims.

**Alternative rejected:** always answer, and let the user judge.

This is domain-specific reasoning, not general caution. If you ask why a design exists and get a
plausible, fluent, wrong answer, you cannot tell it is wrong — that is exactly the knowledge you
lacked. Worse, you will repeat it in a code review, and the wrong rationale propagates. An
unanswered question sends you to the source. A confidently wrong one does not.

The three layers catch genuinely different failures, verified on out-of-scope questions:

| Question | Score | Refused by |
|---|---|---|
| "How do I make sourdough bread?" | 0.355 | confidence gate |
| "How does etcd raft handle leader election?" | 0.415 | reranker — all candidates scored ≤ 0 |
| "What algorithm does kube-scheduler use for bin packing?" | 0.549 | the model, via `INSUFFICIENT_CONTEXT` |

**The gate's threshold was chosen by sweep, not by feel.** The inherited 0.40 turned out to be inert
— identical coverage and leakage anywhere from 0.20 to 0.45, meaning every refusal in practice came
from the other two layers. Re-swept against the production gate (an earlier sweep measured a quantity
`retrieve()` never computes) on the human-reviewed set, **0.60 is the knee: 91.9% coverage, 18.2%
leakage past the gate.** Past 0.60 the trade inverts.

Measured end to end, all three layers fire and none is redundant: of 21 out-of-scope questions, 18
were stopped by the gate and **2 by the cross-encoder after clearing the gate** — 95.2% total.

**Known weakness:** verification fails open. If the verifier's JSON can't be parsed, the answer is
returned unverified rather than blocked — a verifier outage would otherwise take the whole system
down. It is flagged with `verification_ran: false` and the UI surfaces it, but the failure rate is
currently unmeasured. That's a Phase 1 number.

---

## 4a. Chunk size is set by the embedding model, not by taste

**Decision:** `chunker.MAX_TOKENS = EMBED_MAX_TOKENS` (256), with a test asserting they agree.

**What went wrong first:** the budget was 500 while `all-MiniLM-L6-v2` reads 256. Everything past 256
was silently discarded at encode time, so the embedder saw roughly the first third of each chunk while
BM25 — reading full text from SQLite — saw all of it. Mean chunk was 671 tokens; 88% exceeded the
model limit; one reached 56,086.

This produced a plausible-looking wrong conclusion: *BM25 beats embeddings on this corpus by 0.20
recall*. It was tempting to accept, because "keyword search wins on technical text" is a believable
story. Fixing the truncation moved vector recall 0.640 → 0.820 and left BM25 at exactly 0.840 — the
control that proved the diagnosis, since BM25 reads full text from SQLite and was never truncated.

A later human review of the golden set removed 13 questions (off-topic, or leaking their own source)
and flipped the ordering completely: **vector 0.838, BM25 0.811**. So "BM25 beats embeddings on this
corpus" was two artifacts stacked — a truncation bug worth ~0.18, and an unvetted question set worth
the remainder. Neither was a property of the retrievers.

Two related defects surfaced with it. The accumulate-until-full loop never *split* an oversized unit,
so one long comment became one enormous chunk; and the metadata prefix had grown to 221 of the 256
tokens, leaving 25 for content. The prefix now keeps only design-relevant labels (`area/`, `kind/`,
`sig/`, not `lgtm`/`size/L`) and file basenames rather than full paths.

**The lesson worth keeping:** an unexpected benchmark result is a hypothesis about your code, not a
finding about the world. The eval's most valuable output was not a number — it was a bug.


## 4b. Filtering the corpus, once its composition was visible

**Decision:** drop bot-authored comments, Prow slash commands, and sub-40-character replies.

**Alternative rejected:** index everything, on the principle that filtering risks losing signal.

Correct chunk sizing made the corpus size honest for the first time — 627,592 chunks — which prompted
a look at what was in it. Of 12,016 sampled PR comments: 19.9% bot-authored, 27.9% Prow commands
(`/lgtm`, `/approve`, `/retest`), 17.2% sub-60-character replies. Only **35% carried discussion**,
though they held 49% of the text.

The rest were producing thousands of near-identical `/lgtm` chunks — high-frequency, low-content
strings that match many queries and inform none of them. Kubernetes is an unusually automated
repository, so this is more extreme here than it would be elsewhere, but the principle generalises:
know what is in the index before tuning retrieval over it.


## 4c. Context volume is a token budget, not a chunk count

**Decision:** `RETRIEVAL_TOP_N=18`, configurable, replacing a hardcoded `top_n=7`.

Fixing the chunk/embed mismatch (§4a) improved retrieval and quietly broke generation.
`top_n=7` was calibrated when chunks were ~671 tokens — about 4,700 tokens of context.
Halving chunks to 256 cut that to **1,787** without changing a single line of the
generation code. The parameter is expressed as a count; the thing that matters is volume.

The symptom was subtle and would have been easy to misread: the model began answering
`INSUFFICIENT_CONTEXT` to **6 of 15 in-scope questions — and in 5 of those the
ground-truth document was sitting in the context it received.** Read without the
measurement, that looks like an over-strict prompt. It was a starved one.

`TOP_N=18` restores ~4,633 tokens and draws them from 11–12 distinct documents rather
than 6, so the model sees the same volume across more sources, every chunk fully embedded.

**The general lesson:** a parameter tuned against one configuration is not a constant.
Halving chunk size changed the meaning of every downstream number expressed in chunks.


## 4d. The citation system had two compounding defects

**Measured symptom:** only 3 of 10 answers carried any citation, in a system whose entire
premise is cited, grounded answers.

**Defect 1 — two competing numbering schemes.** `format_context` labelled each chunk with a
positional `[1]`, `[2]`, while rule 2 of the prompt demanded `(#84403)` — a number buried
further down inside the chunk text. The model saw the positional label first and often
emitted neither. Worse, the positional index was consumed by *nothing*: the verifier extracts
`#(\d+)` and the UI highlights `(#N)`. It was pure noise competing with the real key.

The fix makes the label *be* the key: each block now opens `Cite as (#84403) | <url>`, and the
prompt demands exactly that string. Sources without a number (commits, community docs) get a
deliberately **non-numeric** key like `(#commit-1a2b3c4d)` — a positional integer there would
be indistinguishable from an issue number to the verifier, i.e. a fabricated citation by
construction.

**Defect 2 — the verifier judged citations on a quarter of the evidence.** `verify_citations`
built its evidence with `chunk_map[num] = text[:800]` inside a loop, so a document
contributing four retrieved chunks was judged on the **last one alone**, truncated. A claim
supported by an earlier chunk was reported unsupported and stripped from the answer.

This is the same interface drift as §4a and §4c: the verifier was written when `top_n=7` meant
roughly one chunk per document reached the model. At `TOP_N=18` a single issue routinely
contributes four. The verifier never adapted, and the failure mode was invisible because a
stripped citation looks exactly like a model that failed to cite.

**Both failures look identical from the outside**, which is why the fix required separating
them in measurement: how often the model *emits* a citation, how often the cited document was
*in context* (grounding), and how often a citation *survives* verification. Conflating them —
which the eval harness originally did, by counting citations in the post-verification text —
hid the real problem and would have reported the citation fix as ineffective.


## 4e. Never round-trip text through a tokenizer

**Decision:** `truncate_to_tokens` and `split_by_tokens` slice the original string at token-aligned
character offsets, and a test asserts every produced piece is a verbatim substring of its input.

**What went wrong:** the obvious implementation is encode → slice ids → decode. `all-MiniLM-L6-v2`
is an **uncased** model, so `decode()` returns lowercase with punctuation re-spaced:

```
[ISSUE #100005 - CLOSED]      ->      [ issue # 100005 - closed ]
```

**62.4% of the index was stored in that mangled form** — every chunk that exceeded the token budget.
Two failures followed, and neither announced itself:

1. **The citation key stopped being extractable.** `r'#(\d+)'` requires digits immediately after the
   hash; the inserted space breaks it. Two thirds of chunks presented an identifier the model could
   not cleanly copy and the verifier could not parse.
2. **The model was shown mangled lowercase prose** and asked to quote design rationale from it.

This was very likely the dominant cause of the low citation rate — more so than the duplicate
numbering in §4d, which was fixed first and produced a smaller improvement.

The lesson is narrow and worth stating exactly: a tokenizer is a **measuring instrument**, not a
transformation. Use it to decide *where* to cut; never let its output become the text you store.


## 5. Chunking multi-author argument threads

**Decision:** accumulate conversation units (body, each review, each inline comment, each comment)
until the 256-token budget is hit, then flush with a 32-token overlap carried into the next chunk.
Units larger than the budget are split rather than emitted whole — see §4a for why that matters.

**Alternative rejected:** fixed-size splitting, or one chunk per comment.

This is the hardest part of the system and the least obvious. An issue thread is not prose — it is
several people disagreeing across time, and **the disagreement is the signal**. Fixed-size splitting
severs a rebuttal from the claim it rebuts, and the resulting chunk reads as consensus when it was
an objection. One-chunk-per-comment loses the thread entirely: "I agree with the above, but only for
guaranteed pods" is meaningless alone.

Unit accumulation keeps each speaker's turn intact, the overlap preserves the join, and the header
prefix keeps every fragment attributable. It is still imperfect — a long argument spanning six
comments will split somewhere — which is why the reranker sees a wide candidate pool rather than a
narrow one.

KEP sections are chunked differently: one chunk per `##` heading, no splitting. An "Alternatives"
section is already a self-contained argument, so imposing a token window on it would do damage that
issue threads require.

---

## 6. Deriving the corpus from a git clone instead of the API

**Decision:** enumerate via a blobless clone; fetch content via GraphQL only for what the clone
identified.

**Alternative rejected:** paginate GitHub's PR list and filter client-side (the original approach).

The old scraper walked the repository's entire merged/closed PR history — 100,000+ PRs — to find the
few thousand touching `pkg/kubelet`, then stopped at whatever `MAX_PRS` was. That is both slow and
*order-dependent*: you get a sample, not a corpus, and re-running gives you a different one.

Git already knows exactly which commits touched a path, for free. A blobless clone is 262 MB and 48
seconds, and `git log --first-parent -- pkg/kubelet` yields 6,272 mainline commits whose merge
messages contain 6,268 PR numbers. Only **4** commits needed the API to resolve, down from 8,458
before the `--first-parent` change.

Result: 6,245 PRs versus the old scraper's 943, and the set is reproducible.

**`--first-parent` matters more than it looks.** 13,562 commits touch the path, but 7,290 are
intra-PR branch commits — "rebase", "fix lint", "address review feedback". They belong to PRs already
captured by their merge commit. Including them filled over half the commit corpus with
work-in-progress noise *and* would have cost ~200 wasted API round-trips resolving them.

---

## 7. Issues from back-references, not just labels

**Decision:** union label search with `fixes #N` / `closes #N` references parsed from PR bodies.

**Alternative rejected:** label search alone, which is the obvious approach.

Label search on `area/kubelet` found **889** issues. Back-references from the PRs found **~1,098
more**. Over half the kubelet issues in the corpus were never labelled — they were just fixed by a
kubelet PR. Relying on labels would have silently lost the majority, with no error to notice.

Also worth recording: `component/kubelet`, configured as a second label, matches **zero** issues. It
was dead config inherited from an assumption nobody checked.

---

## 8. Measuring truncation instead of guessing at limits

**Decision:** request `totalCount` on every GraphQL connection, report what got clipped, then
re-fetch only those documents at higher limits.

**Alternative rejected:** picking "generous enough" limits and hoping.

GitHub prices GraphQL on **requested capacity, not returned data**, and nested connections multiply:
`reviews(first:50){comments(first:30)}` reserves 1,500 node slots per PR to hold what is typically
ten actual comments. So limits are a real cost decision — but truncation is silent, and the PRs most
likely to exceed any limit are the heavily-argued ones that matter most.

`totalCount` is free. Requesting it everywhere turned an assumption into a report: reviews clipped on
320 PRs, review comments on 51, comments on 114 — out of 6,245. A second pass over just those, at
much higher limits, cost roughly 150 points and brought the count still-clipped to **0**.

**I also got the cost model badly wrong**, which is part of why measuring mattered. I estimated 5.6
points per PR from a naive node count and predicted a 7-hour scrape. Measured: **0.33 points per PR**,
and the whole stage ran in ~20 minutes. GitHub prices on actual backend work, not the arithmetic I
assumed. The depth-versus-speed tradeoff I had carefully reasoned about did not exist.

---

## 9. Routing models by task

**Decision:** `qwen/qwen3.8-27b` for query rewriting, `openai/gpt-oss-120b` for answering and
verification, both configurable.

**Alternative rejected:** one model everywhere (which is what the project did originally).

This decision was forced. The hardcoded `llama-3.3-70b-versatile` was **retired by Groq** — all four
call sites returned 404, and nothing in retrieval or generation could run. Re-hardcoding a
replacement would have set up the identical failure, so the model became configuration.

Choosing the replacement surfaced something worth knowing: the `gpt-oss` family are **reasoning
models**. They emit reasoning tokens before content, and the original `max_tokens=100` on query
rewriting was consumed entirely by reasoning, returning an **empty string**. That empty string fed
straight into `vector_search("")` — a meaningless embedding and zero BM25 scores — making every
question fall back. A total retrieval failure that looks exactly like a missing corpus.

Measured on the rewrite task: `gpt-oss-20b` spends 175–364 tokens reasoning about a five-word query
rewrite; `qwen3.8-27b` does it in 7 tokens with no reasoning at all. Rewriting is trivial and runs on
every single question, so it gets the cheap model. Answering has to follow seven strict citation
rules, so it gets the strong one.

`max_tokens` was raised at every call site, and an empty rewrite now logs and falls back to the raw
question rather than silently returning `""`.

---

## 10. One generation path, not two

**Decision:** `should_answer` / `build_messages` / `finalize` are shared; the streaming route owns
only SSE framing.

**Alternative rejected:** keeping the streaming implementation separate, which is how it started.

The chat route had reimplemented the entire flow inline — threshold gate, prompt assembly,
verification, source dedupe — because it needed to stream rather than return a string. Two copies of
the core logic, and they had already drifted: **the streaming path never checked for
`INSUFFICIENT_CONTEXT`**, so a model refusal was served as a normal answer with sources attached.

Collapsing them exposed a worse bug. The frontend set `content: answer` from the *accumulated
streamed tokens* rather than `data.answer` from the done frame. Citation verification ran on every
response, computed a corrected answer with unsupported citations stripped — and the UI threw it away
and rendered the unverified text. The third anti-hallucination layer was inert on the only path the
UI uses.

Both bugs existed because the same logic lived in two places. That is the argument for the refactor,
better than any appeal to cleanliness.

---

## Things deliberately not built

- **Link expansion** (code → PR → issue traversal). Shipped as a README feature but never executed:
  the code-chunking function was never called, so no code chunks existed, so the expansion never
  fired and its link table was written and never read. Removed rather than completed — building it
  properly means indexing and parsing Go source, which is a different project.
- **Celery / Redis.** Wired up, but nothing consumes the queue and no worker is started. Left in
  place, excluded from deployment.
- **Multi-repo.** A few code paths are parameterised by repo name. It is not multi-tenant and
  pretending otherwise would be the kind of overclaim this document exists to prevent.
