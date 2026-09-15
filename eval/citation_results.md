# Citation and refusal results

Measured end to end over **14 answered in-scope questions** and **21 out-of-scope questions**,
on the 37-question human-reviewed golden set. Stopped at 45/59 — the Groq free tier refills
~8,300 tokens/hour against ~5,400 per generation, so the remaining questions were ~9 hours of
waiting for numbers that had been stable across five consecutive checks.

---

## Citations

| | Before fixes | After |
|---|---:|---:|
| Answers emitting ≥1 citation | 3/10 = **30%** | **13/14 = 93%** |
| Citations grounded in retrieved context | — | **17/17 = 100%** |
| Citations surviving verification | 0/2 — both stripped | **16/17 = 94%** |

**Grounding held at 100% across all 17 citations.** The model never cited a document it had
not been shown. That is the failure mode that would matter most — a fabricated source is
indistinguishable from a real one to a reader — and it did not occur once.

Two defects produced the original 30%, and neither was the model's unwillingness to cite:

1. **Duplicate numbering.** `format_context` labelled each chunk `[1]`, `[2]` while the prompt
   demanded `(#84403)` — a number buried inside the chunk text. The positional index was parsed
   by nothing. The label is now the citation key: `Cite as (#84403) | <url>`.
2. **62% of the index was tokenizer-mangled.** `truncate_to_tokens` round-tripped through an
   *uncased* tokenizer, so `[ISSUE #100005 - CLOSED]` was stored as `[ issue # 100005 - closed ]`
   — and `r'#(\d+)'` no longer matches across the inserted space. Two thirds of chunks presented
   a citation key the model could not copy and the verifier could not parse.

The second was by far the larger cause, and was itself introduced while fixing the chunk-size
bug. See `DECISIONS.md` §4d and §4e.

---

## The three refusal layers

| Layer | Out-of-scope refusals |
|---|---:|
| 1. Confidence gate (`score < 0.60`) | 18/21 |
| 2. Cross-encoder (no chunk scored > 0) | **2/21** |
| 3. Model (`INSUFFICIENT_CONTEXT`) | 0/21 |
| **Total** | **20/21 = 95.2%** |

All three fire, and layer 2 is not redundant: **two questions cleared the confidence gate and
were stopped by the cross-encoder.** The gate alone would have passed them. This was invisible
until the harness was fixed to attribute refusals to the correct layer — it had been counting
both as "gate".

Layer 3 shows 0 on the negative set but fires on in-scope questions (2 observed), which is the
expected shape: by the time a genuinely out-of-scope question reaches the model, the first two
layers have usually already stopped it.

### The one that got through

> *"Why does Kubernetes use a declarative API instead of imperative commands?"* — score 0.627,
> answered with 2 citations.

Probably a flaw in the **negative set**, not the system. It is in that set for not being
kubelet-specific, but the declarative/reconciliation model is genuinely argued about in kubelet
issues, so the retrieved discussion may legitimately answer it. Reading the two citations would
settle it — the same class of problem the golden-set review caught, now on the other side.

---

## Verifier health

| Metric | Value |
|---|---:|
| Verification ran successfully | 13/14 = **92.9%** |
| Failed to parse — answer returned unverified | **1 = 7.1%** |
| Answers where the verifier rejected a citation | 2 |

The verifier **fails open** by design: an unparseable response returns the answer rather than
blocking it, so a verifier outage cannot take the system down. **7.1% is the size of that hole**
— roughly one answer in fourteen ships unverified. It is flagged as `verification_ran: false`
and the UI says so, but it is a real limit on the guarantee and should be stated rather than
rounded away.

---

## Caveats

1. **n=14 answered.** Enough to establish that 30% → 93% is real and that grounding holds, not
   enough to state a rate to two significant figures.
2. **Citation *support* is unmeasured.** Grounding (is the cited document in context?) is
   objective and measured. Whether the cited text actually *supports* the claim is judged by an
   LLM verifier, and grading the grader requires a human.
   `python eval/run_generation.py sample` writes a hand-scoring worksheet.
3. **The negative set needs the same review the golden set got** — see the leaked question above.

---

## Raw generated output

### Citation grounding (golden set)

Objective check: does each cited `#N` refer to a document that was actually in the
model's context? A citation to something never retrieved is an invented source.

| Metric | Value |
|---|---:|
| Answers generated | 14 / 24 |
| Citations emitted | 17 |
| **Grounded in retrieved context** | **17/17 = 100.0%** |
| Answers where every citation is grounded | 13/13 = 100.0% |
| Answers with no citation at all | 1 |
| Citations surviving verification | 16/17 = 94.1% |

These are three separate failure modes and conflating them hides the real one:
the model failing to cite, citing something it was never shown (grounding), and
citing a real source that does not support the claim (verification strips it).

### End-to-end refusal (negative set)

| Layer | Refusals |
|---|---:|
| 1. Confidence gate (score < threshold) | 18/21 |
| 2. Cross-encoder (no chunk scored > 0) | 2/21 |
| 3. Model (`INSUFFICIENT_CONTEXT`) | 0/21 |
| **Total refused** | **20/21 = 95.2%** |
| Answered anyway | 1 |

- *"Why does Kubernetes use a declarative API instead of imperative commands?"* — score 0.627, 2 citations

### Verifier health

The verifier fails open by design: an unparseable response returns the answer
unverified rather than blocking it. This is the size of that hole.

| Metric | Value |
|---|---:|
| Verification ran successfully | 13/14 = 92.9% |
| **Failed to parse (answer returned unverified)** | **1 = 7.1%** |
| Answers where the verifier rejected a citation | 2 |
