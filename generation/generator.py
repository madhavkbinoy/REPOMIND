import os
import re
import json
from groq import Groq
from .prompts import SYSTEM_PROMPT, FALLBACK_MESSAGE
from dotenv import load_dotenv

load_dotenv()

client    = Groq(api_key=os.getenv('GROQ_API_KEY'))
MODEL     = os.getenv('GROQ_MODEL', 'openai/gpt-oss-120b')

# Verification is a constrained classification task -- given a claim and the chunk it
# cites, decide whether the chunk supports it and return JSON. That is a different job
# from open-ended generation, and it does not need the larger model. (It also draws on a
# separate rate-limit pool, which makes evaluation runs practical on a free tier.)
MODEL_VERIFY = os.getenv('GROQ_MODEL_VERIFY', 'openai/gpt-oss-20b')

# Total characters of source evidence handed to the verifier, split across the documents
# an answer cites. Must comfortably exceed what one document's retrieved chunks occupy:
# at TOP_N=18 a single issue can contribute 4 chunks of ~850 chars each.
VERIFY_CHARS = int(os.getenv('VERIFY_CHARS', 12000))
THRESHOLD = float(os.getenv('CONFIDENCE_THRESHOLD', 0.40))
MAX_ANSWER_TOKENS = int(os.getenv('MAX_ANSWER_TOKENS', 3072))


def _slugify(s: str, limit: int = 40) -> str:
    s = re.sub(r'[^A-Za-z0-9]+', '-', s or '').strip('-').lower()
    return s[:limit].strip('-') or 'unknown'


def citation_key(chunk: dict) -> str:
    """The exact string the model must emit to cite this chunk.

    One label, one meaning. This is simultaneously (a) the label `format_context`
    puts on the chunk, (b) the format SYSTEM_PROMPT rule 2 demands, (c) what
    App.jsx highlights, and (d) for numbered sources, what `verify_citations`
    extracts with r'#(\\d+)'. The context previously carried a second, positional
    `[i+1]` label that nothing downstream parsed; it competed with this one and
    the model frequently emitted neither.

    Chunks with no `number` -- commits and community docs -- get a key that is
    *deliberately non-numeric*. A positional integer there would be
    indistinguishable from an issue number to the verifier's r'#(\\d+)', i.e. a
    fabricated citation by construction. The `#name` form keeps the citation
    shape uniform for the model and for the UI highlighter, while the leading
    letter guarantees the numeric extractor skips it and the answer degrades to
    'unverified but honest' rather than 'verified against the wrong document'.
    """
    num = chunk.get('number')
    if num is not None:
        return f'(#{num})'

    if (chunk.get('source_type') or '') == 'commit':
        oid = (chunk.get('url') or '').rstrip('/').rsplit('/', 1)[-1][:8]
        return f'(#commit-{oid or "unknown"})'

    return f'(#doc-{_slugify(chunk.get("title") or chunk.get("file_path") or chunk.get("url"))})'


def format_context(chunks: list[dict]) -> str:
    parts = []
    for c in chunks:
        src = c.get('url') or c.get('file_path') or 'unknown'
        parts.append(f'Cite as {citation_key(c)} | {src}\n{c["text"]}\n')
    return '\n'.join(parts)


def dedupe_sources(chunks: list[dict]) -> list[dict]:
    seen, out = set(), []
    for c in chunks:
        key = (c.get('source_type'), c.get('number'), c.get('file_path'))
        if key not in seen:
            seen.add(key)
            out.append({
                'source_type': c.get('source_type'),
                'number':      c.get('number'),
                'title':       c.get('title'),
                'url':         c.get('url'),
            })
    return out[:6]


VERIFY_PROMPT = '''
You are a citation verifier. You will be given:
1. An answer that cites GitHub issues by number
2. The actual text chunks from those issues

For each citation in the answer, check whether the cited chunk actually
supports the specific claim made about it.

Answer to verify:
{answer}

Available chunks:
{chunks}

Respond with a JSON object in exactly this format, nothing else:
{{
  "valid": true or false,
  "invalid_citations": ["#N: reason why this citation does not support the claim"],
  "verified_answer": "the answer with invalid citations removed or replaced with [UNVERIFIED]"
}}
'''


def verify_citations(answer: str, chunks: list[dict]) -> dict:
    cited_numbers = set(int(n) for n in re.findall(r'#(\d+)', answer))
    if not cited_numbers:
        return {'valid': True, 'verified_answer': answer,
                'invalid_citations': [], 'verification_ran': True}

    # Gather EVERY retrieved chunk for each cited document, not just the last one.
    # This previously read `chunk_map[num] = text[:800]` inside the loop, so a document
    # with four retrieved chunks was judged on one of them, truncated -- roughly a
    # quarter of the evidence the model actually saw. The verifier then reported the
    # citation as unsupported and stripped it: a false rejection by construction, and
    # one that got worse as TOP_N grew from 7 to 18.
    grouped: dict[int, list[str]] = {}
    for c in chunks:
        num = c.get('number')
        if num in cited_numbers:
            grouped.setdefault(num, []).append(c.get('text', ''))

    if not grouped:

        # The answer cited something that was never retrieved -- an invented source.
        # Caught without an LLM call, so verification did run, and conclusively.
        return {'valid': False, 'verified_answer': answer, 'verification_ran': True,
                'invalid_citations': [f'#{n} was not in the retrieved context'
                                      for n in sorted(cited_numbers)]}

    # Budget shared across cited documents so a many-citation answer cannot blow up the
    # verifier prompt, while a single-citation answer gets the full evidence.
    per_doc = max(VERIFY_CHARS // max(len(grouped), 1), 1200)
    chunk_map = {num: '\n---\n'.join(texts)[:per_doc] for num, texts in grouped.items()}
    chunk_str = '\n\n'.join(f"#{num}:\n{text}" for num, text in chunk_map.items())

    try:
        resp = client.chat.completions.create(
            model=MODEL_VERIFY,
            max_tokens=2048,
            messages=[{
                'role': 'user',
                'content': VERIFY_PROMPT.format(answer=answer, chunks=chunk_str)
            }]
        )

        raw    = (resp.choices[0].message.content or '').strip()
        result = json.loads(raw)
        result.setdefault('verification_ran', True)
        return result
    except Exception as e:
        # Fail open so a verifier outage cannot block answers -- but say so, and
        # flag it, because an unverified answer must not look like a verified one.
        print(f'[verify] verification failed, returning unverified answer: {e}')
        return {'valid': True, 'verified_answer': answer,
                'invalid_citations': [], 'verification_ran': False}


def should_answer(best_score: float, chunks: list[dict]) -> bool:
    """The confidence gate. Single definition, used by both the streaming and
    blocking paths so they can never disagree about when to refuse."""
    return bool(chunks) and best_score >= THRESHOLD


def fallback_response(best_score: float) -> dict:
    return {'answer': FALLBACK_MESSAGE, 'sources': [], 'is_fallback': True,
            'best_score': best_score}


def build_messages(question: str, chunks: list[dict], repo: str,
                   history: list[dict]) -> list[dict]:
    system = SYSTEM_PROMPT.format(repo=repo, context=format_context(chunks))
    msgs   = [{'role': m['role'], 'content': m['content']} for m in history[-6:]]
    msgs.append({'role': 'user', 'content': question})
    return [{'role': 'system', 'content': system}] + msgs


def finalize(answer: str, chunks: list[dict], best_score: float) -> dict:
    """Post-process a completed answer: insufficient-context check, citation
    verification, source dedupe. Shared by the streaming and blocking paths."""
    answer = (answer or '').strip()

    if answer.startswith('INSUFFICIENT_CONTEXT'):
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


def generate(question: str, chunks: list[dict], best_score: float,
             repo: str, history: list[dict] = []) -> dict:
    """Blocking generation. The streaming route in api/routes/chat.py runs the
    same three stages, differing only in how the answer text is obtained."""
    if not should_answer(best_score, chunks):
        return fallback_response(best_score)

    resp = client.chat.completions.create(
        model=MODEL,
        max_tokens=MAX_ANSWER_TOKENS,
        messages=build_messages(question, chunks, repo, history),
    )
    return finalize(resp.choices[0].message.content, chunks, best_score)
