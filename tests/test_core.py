"""
Tests for the logic that is easy to break silently.

Deliberately not aiming for coverage. Each test here pins down behaviour that either
caused a real bug in this codebase or would fail invisibly if it regressed -- the
kind of thing that produces wrong answers rather than a stack trace.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ---------------------------------------------------------------- RRF fusion

def test_rrf_rewards_agreement_between_retrievers():
    """A document both retrievers rank highly must beat one only vector found."""
    from retrieval.pipeline import _rrf

    vec  = [{'id': 'both'}, {'id': 'vec_only'}]
    bm25 = [{'id': 'both'}, {'id': 'bm25_only'}]
    assert _rrf(vec, bm25)[0] == 'both'


def test_rrf_includes_bm25_only_documents():
    """Regression: fusion previously discarded every BM25-only hit downstream.

    _rrf itself always returned them -- the bug was the filter after it. This pins
    the contract so a future 'optimisation' can't quietly drop them again.
    """
    from retrieval.pipeline import _rrf

    fused = _rrf([{'id': 'v'}], [{'id': 'b'}])
    assert set(fused) == {'v', 'b'}


def test_rrf_handles_empty_retriever():
    from retrieval.pipeline import _rrf

    assert _rrf([], [{'id': 'b'}]) == ['b']
    assert _rrf([{'id': 'v'}], []) == ['v']
    assert _rrf([], []) == []


# ------------------------------------------------------------ chunk metadata

def _issue(number=123, body='body text', comments=(), title='A title'):
    return {
        'number': number, 'title': title, 'body': body, 'state': 'CLOSED',
        'author': {'login': 'alice'},
        'labels': {'nodes': [{'name': 'area/kubelet'}]},
        'comments': {'nodes': [{'author': {'login': 'bob'}, 'body': c} for c in comments]},
    }


def test_every_chunk_carries_its_citation_metadata():
    """A chunk that loses its number cannot be cited, and citation is the product."""
    from ingestion.chunker import chunk_issue

    for c in chunk_issue(_issue(number=84403), 'kubernetes/kubernetes'):
        assert c['number'] == 84403
        assert c['source_type'] == 'issue'
        assert '84403' in c['url']
        assert '[ISSUE #84403' in c['text'], 'header must be inside the embedded text'


def test_long_thread_splits_into_multiple_chunks():
    from ingestion.chunker import chunk_issue

    long_comments = ['word ' * 400 for _ in range(4)]
    chunks = chunk_issue(_issue(comments=long_comments), 'kubernetes/kubernetes')
    assert len(chunks) > 1
    assert [c['chunk_index'] for c in chunks] == list(range(len(chunks)))


def test_empty_issue_produces_no_chunks():
    from ingestion.chunker import chunk_issue

    assert chunk_issue(_issue(body='', comments=()), 'kubernetes/kubernetes') == []


# ------------------------------------------------------- PR number extraction

@pytest.mark.parametrize('message, expected', [
    ('Merge pull request #141041 from user/branch\n\nFix CPU realloc', {141041}),
    ('Add warning for invalid static pod priority (#136705)', {136705}),
    ('Merge pull request #1 from a/b', {1}),
])
def test_pr_numbers_parsed_from_commit_messages(message, expected):
    from ingestion.scraper_v2 import pr_numbers_from_commits

    found, unresolved = pr_numbers_from_commits([{'message': message}])
    assert found == expected
    assert unresolved == []


def test_commit_without_a_pr_reference_is_reported_unresolved():
    """Unresolved commits cost an API call each, so they must not be silently dropped."""
    from ingestion.scraper_v2 import pr_numbers_from_commits

    found, unresolved = pr_numbers_from_commits([{'message': 'fix typo', 'oid': 'abc123'}])
    assert found == set()
    assert unresolved == ['abc123']


# ------------------------------------------------------------ the answer gate

@pytest.mark.parametrize('score, chunks, expected', [
    (0.9, [{'x': 1}], True),
    (0.1, [{'x': 1}], False),
    (0.9, [],         False),   # reranker dropped everything
    (0.0, [],         False),
])
def test_should_answer_requires_both_score_and_chunks(score, chunks, expected):
    from generation.generator import should_answer, THRESHOLD

    assert should_answer(score, chunks) is (expected and score >= THRESHOLD)


def test_insufficient_context_is_converted_to_a_fallback():
    """The streaming path once missed this, serving a refusal as a normal answer."""
    from generation.generator import finalize

    out = finalize('INSUFFICIENT_CONTEXT: nothing about eviction ordering', [], 0.9)
    assert out['is_fallback'] is True
    assert out['sources'] == []
    assert not out['answer'].startswith('INSUFFICIENT_CONTEXT')


def test_answer_without_citations_skips_verification():
    from generation.generator import verify_citations

    out = verify_citations('No numbers here at all.', [])
    assert out['valid'] is True
    assert out['verification_ran'] is True


def test_citation_to_a_chunk_we_never_retrieved_is_invalid():
    from generation.generator import verify_citations

    out = verify_citations('As decided in (#999).', [{'number': 111, 'text': 'unrelated'}])
    assert out['valid'] is False


# ------------------------------------------------------------ source handling

def test_dedupe_sources_collapses_chunks_of_one_document():
    from generation.generator import dedupe_sources

    chunks = [{'source_type': 'issue', 'number': 1, 'title': 't', 'url': 'u', 'file_path': None}
              for _ in range(5)]
    assert len(dedupe_sources(chunks)) == 1


def test_dedupe_sources_caps_the_list():
    from generation.generator import dedupe_sources

    chunks = [{'source_type': 'issue', 'number': i, 'title': 't', 'url': 'u', 'file_path': None}
              for i in range(20)]
    assert len(dedupe_sources(chunks)) <= 6


def test_context_label_is_the_citation_key_not_a_positional_index():
    """The measured bug: the context carried a positional `[1]` label on the line
    above the `[ISSUE #84403]` header, so two numbering schemes competed and the
    model cited neither in 7 of 10 answers. Nothing downstream ever parsed `[N]`.

    The contract now: the label the model reads IS the citation it must emit.
    """
    import re
    from generation.generator import format_context

    ctx = format_context([
        {'text': '[ISSUE #84403 - CLOSED]\nbody', 'url': 'u1',
         'number': 84403, 'source_type': 'issue'},
        {'text': '[PR #91169 - MERGED]\nbody', 'url': 'u2',
         'number': 91169, 'source_type': 'pr'},
    ])

    assert re.findall(r'Cite as (\(#\S+?\)) \|', ctx) == ['(#84403)', '(#91169)']
    assert '[1]' not in ctx and '[2]' not in ctx, 'no competing numbering scheme'


def test_context_label_is_what_verify_citations_extracts():
    """Closes the loop: the label, the format the prompt demands, and the
    anti-hallucination regex must all agree on the same string."""
    import re
    from generation.generator import citation_key

    key = citation_key({'number': 84403, 'source_type': 'issue'})
    assert re.findall(r'#(\d+)', key) == ['84403']


def test_prompt_demonstrates_a_key_the_context_can_actually_produce():
    """Tripwire against prompt/formatter drift: the worked example in rule 2 must
    be a literal key `citation_key` emits, not an illustrative near-miss."""
    from generation.prompts import SYSTEM_PROMPT
    from generation.generator import citation_key

    assert citation_key({'number': 84403, 'source_type': 'issue'}) in SYSTEM_PROMPT
    assert 'INSUFFICIENT_CONTEXT' in SYSTEM_PROMPT, 'refusal rule must survive edits'


@pytest.mark.parametrize('chunk', [
    {'text': '[COMMIT 1a2b3c4d]\nmsg', 'number': None, 'source_type': 'commit',
     'url': 'https://github.com/k/k/commit/1a2b3c4d5e6f7890aabb'},
    {'text': '[sig-node/kubelet-eviction.md - community-doc]\nbody', 'number': None,
     'source_type': 'community', 'title': 'kubelet eviction - Design',
     'url': 'https://github.com/kubernetes/community/blob/master/sig-node/x.md'},
])
def test_numberless_chunk_never_yields_a_numeric_citation(chunk):
    """Commits and community docs have no issue number. A numeric label there
    would resolve to no retrieved chunk -- a fabricated citation manufactured by
    the formatter itself. Their key must carry nothing `#(\\d+)` can extract, and
    an answer citing it must not be flagged as an invented source."""
    import re
    from generation.generator import citation_key, format_context, verify_citations

    key = citation_key(chunk)
    assert key.startswith('(#') and key.endswith(')')
    assert re.findall(r'#(\d+)', key) == [], f'{key} looks like an issue number'

    assert re.findall(r'Cite as (\S+) \|', format_context([chunk])) == [key]

    # No numeric citation -> no LLM call, and no "cited something never retrieved".
    out = verify_citations(f'The source says X {key}.', [chunk])
    assert out['valid'] is True and out['invalid_citations'] == []


# ------------------------------------------------------------- eval integrity

def test_lexical_overlap_bounds():
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'eval'))
    from build_golden import lexical_overlap

    assert lexical_overlap('kubelet eviction disk pressure', 'kubelet eviction disk pressure') == 1.0
    assert lexical_overlap('completely different vocabulary', 'kubelet eviction') == 0.0


def test_golden_set_questions_are_self_contained():
    """A question naming 'this PR' can't be asked by someone who hasn't found the answer,
    so it measures nothing. This guards the eval set itself against regressing."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'eval'))
    from build_golden import defects

    golden = Path(__file__).resolve().parent.parent / 'eval' / 'golden.jsonl'
    if not golden.exists():
        pytest.skip('golden set not built')

    bad = [r['question'] for r in map(json.loads, golden.open())
           if r.get('accepted') is not False and defects(r['question'])]
    assert not bad, f'{len(bad)} defective questions, e.g. {bad[:2]}'


# ------------------------------------------- chunk / embedder size agreement

def test_chunk_budget_matches_the_embedding_model():
    """The bug this guards: chunker targeted 500 tokens while the embedder read 256,
    so 88% of chunks were silently truncated at encode time and vector search saw
    only their opening third. These two numbers must never drift apart again."""
    from ingestion.chunker import MAX_TOKENS
    from ingestion.token_utils import EMBED_MAX_TOKENS

    assert MAX_TOKENS <= EMBED_MAX_TOKENS


def test_declared_limit_matches_the_actual_model():
    """EMBED_MAX_TOKENS is a hand-written constant; this checks it against reality,
    so swapping the embedding model can't silently reintroduce the mismatch."""
    from sentence_transformers import SentenceTransformer
    from ingestion.token_utils import EMBED_MAX_TOKENS

    assert EMBED_MAX_TOKENS <= SentenceTransformer('all-MiniLM-L6-v2').max_seq_length


@pytest.mark.parametrize('builder', ['issue', 'pr', 'commit'])
def test_no_chunk_exceeds_the_limit_however_pathological_the_input(builder):
    from ingestion.chunker import chunk_issue, chunk_pr, chunk_commit, MAX_TOKENS
    from ingestion.token_utils import count_tokens

    giant = 'kubelet eviction manager ranks pods by qos and usage. ' * 2000
    if builder == 'commit':
        chunks = chunk_commit({'oid': 'a' * 40, 'message': giant,
                               'author': {'name': 'x'}, 'committedDate': '2024'}, 'k/k')
    else:
        doc = {'number': 1, 'title': 'T', 'body': giant, 'state': 'OPEN',
               'author': {'login': 'a'}, 'labels': {'nodes': []},
               'comments': {'nodes': [{'author': {'login': 'b'}, 'body': giant}]},
               'reviews': {'nodes': []}, 'files': {'nodes': []}}
        chunks = (chunk_issue if builder == 'issue' else chunk_pr)(doc, 'k/k')

    assert chunks
    assert max(count_tokens(c['text']) for c in chunks) <= MAX_TOKENS


def test_oversized_single_unit_is_split_not_emitted_whole():
    """One long comment used to become one oversized chunk: the accumulate loop
    appended a unit whole regardless of size. It must now split."""
    from ingestion.chunker import chunk_issue

    doc = {'number': 1, 'title': 'T', 'body': '', 'state': 'OPEN',
           'author': {'login': 'a'}, 'labels': {'nodes': []},
           'comments': {'nodes': [{'author': {'login': 'b'}, 'body': 'word ' * 3000}]}}
    assert len(chunk_issue(doc, 'k/k')) > 5


def test_verifier_sees_every_chunk_of_a_cited_document():
    """Regression: verify_citations did `chunk_map[num] = text[:800]` inside a loop, so a
    document contributing four retrieved chunks was judged on the last one alone. Claims
    supported by an earlier chunk were stripped as unsupported -- a false rejection that
    worsened as TOP_N grew from 7 to 18."""
    from generation.generator import verify_citations

    chunks = [
        {'number': 111, 'text': '[ISSUE #111] Preamble about unrelated setup.'},
        {'number': 111, 'text': '[ISSUE #111] Kubelet rejects pods under disk pressure '
                                'because disk is best-effort for every QoS class.'},
    ]
    out = verify_citations(
        'Kubelet rejects pods under disk pressure as disk is best-effort (#111).', chunks)
    assert out['valid'] is True, 'evidence in a non-final chunk must still count'


def test_citation_key_never_fabricates_a_number():
    """Unnumbered sources must not get a positional integer -- it would be
    indistinguishable from an issue number to the verifier's r'#(\\d+)'."""
    import re
    from generation.generator import citation_key

    for chunk in [{'number': None, 'source_type': 'commit',
                   'url': 'https://github.com/k/k/commit/abcdef1234'},
                  {'number': None, 'source_type': 'community', 'title': 'Kubelet eviction'}]:
        key = citation_key(chunk)
        assert not re.fullmatch(r'\(#\d+\)', key), f'{key} looks like an issue citation'
        assert key.startswith('(#') and key.endswith(')')


def test_truncation_preserves_the_original_text():
    """Regression: truncate_to_tokens used to encode->slice->decode. all-MiniLM-L6-v2 is
    an UNCASED model, so decode() returned lowercase with punctuation re-spaced --
    `[ISSUE #100005 - CLOSED]` became `[ issue # 100005 - closed ]`. That broke the
    citation key (r'#(\\d+)' no longer matches across the inserted space) and fed the
    model mangled prose to quote from. 62% of the index was affected."""
    import re
    from ingestion.token_utils import truncate_to_tokens, count_tokens

    text = '[ISSUE #100005 - CLOSED]\nTitle: Memory manager fails. ' + 'detail. ' * 300
    out  = truncate_to_tokens(text, 256)

    assert out.startswith('[ISSUE #100005 - CLOSED]'), 'case and spacing must survive'
    assert re.findall(r'#(\d+)', out) == ['100005'], 'citation key must stay extractable'
    assert count_tokens(out) <= 256
    assert text.startswith(out), 'must be a prefix of the original, not a re-rendering'


def test_split_pieces_are_substrings_of_the_original():
    from ingestion.token_utils import split_by_tokens

    text = '[PR #99095 - MERGED]\nTitle: Prevent DiskPressure. ' + 'body text. ' * 300
    for piece in split_by_tokens(text, 250, overlap=32):
        assert piece in text, 'pieces must be verbatim slices, never decoded tokens'
