import logging, os
from sentence_transformers import CrossEncoder

log = logging.getLogger(__name__)
# See vector_search.py: configures the root handler only if nobody else has, at WARNING,
# and raises just this module's logger to LOG_LEVEL.
if not logging.getLogger().handlers:
    logging.basicConfig(level=logging.WARNING, format='%(levelname)s %(name)s: %(message)s')
log.setLevel(os.getenv('LOG_LEVEL', 'INFO'))

_ce = CrossEncoder('cross-encoder/ms-marco-MiniLM-L-6-v2')

# Pair budget check (the class of bug that cost 0.18 recall in the embedder): this model
# truncates the (query, chunk) pair at 512 tokens, and chunks are capped at 256 by
# ingestion/chunker.MAX_TOKENS -- which is itself set by MiniLM's 256-token limit -- so a
# query of up to ~250 tokens still fits whole. Verified: _ce.max_length == 512, corpus
# p95 == max == 256 tokens. If either number moves, they must be rechecked together.

# How many chunks survive reranking. Defaults to the same env var the only caller reads
# (retrieval/pipeline.py::TOP_N), so the default and the call site cannot disagree. The
# previous default of 5 was a stale copy of a value the pipeline had already moved to 18
# after measuring that 7 chunks x 256 tokens starved the model's context -- any future
# caller taking the default would silently have got the pre-fix behaviour back.
TOP_N = int(os.getenv('RETRIEVAL_TOP_N', 18))

# MIN_RERANK_SCORE does two jobs and they are not the same job:
#
#   1. CONTEXT SELECTION -- of the candidates that are relevant, which are worth spending
#      the model's context on. This is a precision knob.
#   2. REFUSAL -- if *every* candidate scores <= this, rerank() returns [], and the caller
#      refuses (generation/generator.py::should_answer requires chunks as well as score).
#      This is refusal layer 2 of 3 and it fires independently of CONFIDENCE_THRESHOLD:
#      "How does the etcd raft implementation handle leader election?" scored 0.415 at the
#      confidence gate -- above the then-current 0.40 -- and was refused only here, because
#      the cross-encoder scored every candidate negative (DECISIONS.md sections 2 and 4).
#
# So this constant answers "is anything here relevant at all?" and "which of the relevant
# things do we keep?" with a single number, and a change intended for one silently moves
# the other: raising it to tighten context would also make the system refuse more often.
# See the report / DECISIONS.md for the proposed split into two thresholds -- deliberately
# not implemented, because separating them changes refusal behaviour and must be measured
# against the out-of-scope set (currently 86.4% correctly refused) first.
#
# 0.0 is the value the eval/results.md baseline was measured with and stays the default;
# the env var exists so it can be swept like CONFIDENCE_THRESHOLD was, not so it can drift.
MIN_RERANK_SCORE = float(os.getenv('MIN_RERANK_SCORE', 0.0))


def rerank(query: str, candidates: list[dict], top_n: int = TOP_N) -> list[dict]:
    """Score (query, chunk) pairs jointly and keep the best `top_n` that clear the floor.

    Returning fewer than `top_n` -- including zero -- is a designed outcome, not a
    failure: see MIN_RERANK_SCORE above. An empty return means "nothing relevant", which
    the caller turns into a refusal, so it is logged.
    """
    if not candidates:
        return []

    # A candidate with no text is a hydration failure upstream (fetch_by_ids returning a
    # payload-less point), not a bad match. Scoring it against an empty string keeps the
    # call from raising KeyError, and the warning names the real problem.
    blank = sum(1 for c in candidates if not c.get('text'))
    if blank:
        log.warning('rerank: %d of %d candidates carry no text -- upstream hydration is dropping payloads',
                    blank, len(candidates))

    scores = _ce.predict([(query, c.get('text') or '') for c in candidates])
    ranked = sorted(zip(scores, candidates), key=lambda x: -x[0])

    # `ranked` is sorted descending, so slicing before filtering is equivalent to
    # filtering before slicing: everything below the first score <= MIN_RERANK_SCORE is
    # also <= MIN_RERANK_SCORE. This is not the "rank then throw the ranking away" bug.
    kept = [c for score, c in ranked[:top_n] if score > MIN_RERANK_SCORE]

    if not kept:
        log.info('rerank: all %d candidates scored <= %.3f (best %.4f) for %r -- refusing at the reranker, '
                 'not at the confidence gate', len(candidates), MIN_RERANK_SCORE, float(ranked[0][0]), query[:120])
    else:
        log.debug('rerank: kept %d/%d (asked %d, best %.4f, worst kept %.4f, floor %.3f)',
                  len(kept), len(candidates), top_n, float(ranked[0][0]),
                  float(ranked[len(kept) - 1][0]), MIN_RERANK_SCORE)
    return kept
