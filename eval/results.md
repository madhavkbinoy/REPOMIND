# Evaluation results

Corpus 113,047 chunks. Golden set **37 human-reviewed** questions; negative set 22 out-of-scope.
Auto-generated tables live in [`metrics.md`](metrics.md) — this file is the analysis and is not
overwritten by the harness.

---

## The benchmark's job was finding bugs, not producing numbers

The first run said BM25 beat the embedding model by **0.20 recall**, inverting the usual assumption.
That is a publishable-sounding story ("keyword search wins on technical text"). Investigating it
instead found three bugs and two stacked artifacts.

### Artifact 1 — the embedder was reading a third of each chunk

`all-MiniLM-L6-v2` reads **256 tokens**; the chunker was emitting a mean of **671**, with 88% of
chunks over the limit and one reaching 56,086. Three defects compounded: a budget double the model's
limit, an accumulate loop that never *split* an oversized unit, and a metadata prefix grown to
**221 of 256 tokens** — leaving 25 for content.

| | Before | After | Δ |
|---|---:|---:|---:|
| **Vector only** | 0.640 | **0.820** | **+0.180** |
| BM25 only | 0.840 | 0.840 | **0.000** |

**BM25 did not move by a thousandth.** It reads full text from SQLite and was never truncated, so a
fix aimed at the embedder had to move vector recall and leave BM25 exactly where it was. That
control is what turned a plausible story into a confirmed diagnosis.

### Artifact 2 — the question set had never been read

Human review rejected **13 of 50**: six about kube-proxy/scheduler/apiserver (they entered the corpus
legitimately via PR back-references), three with lexical overlap above 0.85, and four leaking their
own source — one named two internal function symbols verbatim, two were dependency-bump trivia.

| Configuration | 50 unreviewed | **37 reviewed** |
|---|---:|---:|
| Vector only | 0.820 | **0.838** |
| BM25 only | 0.840 | **0.811** |
| RRF hybrid | 0.920 | 0.919 |
| RRF + cross-encoder | 0.920 | 0.919 |
| RRF + cross-encoder, no rewrite | 0.940 | **0.973** |

Stratified by how much vocabulary each question shares with its source:

| Stratum | mean overlap | Vector R@10 | BM25 R@10 |
|---|---:|---:|---:|
| Low overlap | 0.55 | **0.778** | **0.778** |
| High overlap | 0.74 | **0.895** | 0.842 |

**Exactly tied** where overlap is low. "BM25 beats embeddings on this corpus" was a truncation bug
worth ~0.18 plus an unvetted question set worth the rest — never a property of the retrievers.

**Hybrid still earns its place, for a better reason.** The retrievers sit within 0.03 of each other,
yet fusion reaches 0.919: they fail on *different* questions.

### Bug 3 — 62% of the index was tokenizer-mangled

Fixing the chunk budget introduced something worse. `truncate_to_tokens` did encode → slice →
decode, and `all-MiniLM-L6-v2` is **uncased**, so decoding returned lowercase with punctuation
re-spaced:

```
[ISSUE #100005 - CLOSED]   ->   [ issue # 100005 - closed ]
```

Every chunk over budget — **62.4% of the index** — was stored that way, and the citation key stopped
matching `r'#(\d+)'` because of the inserted space. Two thirds of chunks presented an identifier the
model could not copy and the verifier could not parse.

Fixed by slicing the original string at token-aligned character offsets. A test now asserts every
produced piece is a verbatim substring of its input.

> A tokenizer is a **measuring instrument, not a transformation**. Use it to decide where to cut;
> never let its output become the text you store.

---

## Threshold

The inherited `0.40` was **inert** — identical coverage and leakage anywhere from 0.20 to 0.45.

It was then raised to 0.60 on a sweep reporting 92% coverage. **That sweep measured the wrong
quantity:** it computed the gate as `max(score)` over reranked chunks, while `retrieve()` gates on
`vec_hits[0]['score']`. The two differ on *every* question. Re-swept against the production gate on
the reviewed set, **0.60 is the knee** — 91.9% coverage, 18.2% leak; past it the trade inverts.

The gate is the first of **three** refusal layers, so leakage here is not the hallucination rate.

---

## Candidate pool — a tested non-change

With `TOP_N=18` the reranker sees ~40 candidates and keeps 18. The obvious optimisation is to
retrieve more. Measured, that is wrong: recall is flat or worse at k=40 and k=60. `ms-marco-MiniLM-L-6-v2`
is small, and its precision degrades as distractors multiply. `k=20` stays — validated, not inherited.

---

## Caveats

1. **Query rewriting** measures negative (+0.033 recall when removed) but the construction bias
   penalises paraphrasing, so the measurement is biased against it.
2. **The threshold was tuned on the set it is reported against.** Mild overfitting; a held-out split
   would fix it.
3. **n=37 is small.** Differences under ~5 points are noise.
4. **Ground truth is single-document** — a question answerable from several scores a miss on all but
   one. Every number is a lower bound.

---

## Generation-side results

Measured over 14 answered in-scope and 21 out-of-scope questions — full write-up in
[`citation_results.md`](citation_results.md).

| | Before fixes | After |
|---|---:|---:|
| Answers emitting ≥1 citation | 30% | **93%** |
| Citations grounded in retrieved context | — | **17/17 = 100%** |
| Citations surviving verification | 0/2 | **16/17 = 94%** |
| Out-of-scope refused (all layers) | — | **20/21 = 95.2%** |
| Verifier failed to parse (fails open) | — | **7.1%** |

Stopped at 45/59: the free tier refills ~8,300 tokens/hour against ~5,400 per generation, and the
percentages had been stable across five consecutive checks. n=14 establishes that 30% → 93% is real
and that grounding holds; it does not pin the rate to two significant figures.

---

## Latest generated tables

### Retrieval (n=37, k=10)

| Configuration | Recall@10 | MRR |
|---|---:|---:|
| Vector only | 0.838 | 0.618 |
| BM25 only | 0.811 | 0.605 |
| RRF hybrid | 0.919 | 0.767 |
| RRF + cross-encoder | 0.919 | 0.792 |
| RRF + rerank, no rewrite | 0.973 | 0.865 |


### Refusal behaviour

| Set | n | Outcome |
|---|---:|---|
| Out-of-scope | 22 | **68.2%** refused at the gate *(superseded: 95.2% end to end — see above)* |
| In-scope | 37 | **94.6%** answered |

Refusal here is the retrieval gate only. The model can also decline with `INSUFFICIENT_CONTEXT` after generation, so the end-to-end refusal rate is at least this high.


### Confidence threshold sweep

| Threshold | In-scope answered | Out-of-scope leaked |
|---:|---:|---:|
| 0.20 | 100.0% | 45.5% |
| 0.25 | 100.0% | 45.5% |
| 0.30 | 100.0% | 45.5% |
| 0.35 | 100.0% | 45.5% |
| 0.40 | 100.0% | 45.5% |
| 0.45 | 100.0% | 45.5% |
| 0.50 | 100.0% | 40.9% |
| 0.55 | 97.3% | 31.8% |  ← current
| 0.60 | 91.9% | 18.2% |
| 0.65 | 81.1% | 9.1% |
| 0.70 | 59.5% | 4.5% |
| 0.75 | 45.9% | 4.5% |
| 0.80 | 10.8% | 0.0% |

"Leaked" means an out-of-scope question cleared the gate and reached the model — where `INSUFFICIENT_CONTEXT` is the remaining defence.


### Is BM25 winning, or is the question set leaking?

| Stratum | n | mean overlap | Vector R@10 | BM25 R@10 | BM25 lead |
|---|---:|---:|---:|---:|---:|
| Low overlap | 18 | 0.55 | 0.778 | 0.778 | +0.000 |
| High overlap | 19 | 0.74 | 0.895 | 0.842 | -0.053 |

BM25's lead does not survive on low-overlap questions (+0.000); it is driven by vocabulary shared with the source document.
