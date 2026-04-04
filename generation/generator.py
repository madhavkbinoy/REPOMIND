import os
import re
import json
from groq import Groq
from .prompts import SYSTEM_PROMPT, FALLBACK_MESSAGE
from dotenv import load_dotenv

load_dotenv()

client    = Groq(api_key=os.getenv('GROQ_API_KEY'))
THRESHOLD = float(os.getenv('CONFIDENCE_THRESHOLD', 0.40))


def format_context(chunks: list[dict]) -> str:
    parts = []
    for i, c in enumerate(chunks):
        src = c.get('url') or c.get('file_path') or 'unknown'
        parts.append(f'[{i+1}] {src}\n{c["text"]}\n')
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
        return {'valid': True, 'verified_answer': answer, 'invalid_citations': []}

    chunk_map = {}
    for c in chunks:
        num = c.get('number')
        if num in cited_numbers:
            chunk_map[num] = c.get('text', '')[:800]

    if not chunk_map:
        return {'valid': False, 'verified_answer': answer, 'invalid_citations': ['No chunks found for cited issues']}

    chunk_str = '\n\n'.join(f"#{num}:\n{text}" for num, text in chunk_map.items())

    try:
        resp = client.chat.completions.create(
            model='llama-3.3-70b-versatile',
            max_tokens=1000,
            messages=[{
                'role': 'user',
                'content': VERIFY_PROMPT.format(answer=answer, chunks=chunk_str)
            }]
        )

        raw = resp.choices[0].message.content.strip()
        result = json.loads(raw)
        return result
    except (json.JSONDecodeError, Exception):
        return {'valid': True, 'verified_answer': answer, 'invalid_citations': []}


def generate(question: str, chunks: list[dict], best_score: float,
             repo: str, history: list[dict] = []) -> dict:
    if best_score < THRESHOLD or not chunks:
        return {'answer': FALLBACK_MESSAGE, 'sources': [], 'is_fallback': True, 'best_score': best_score}

    context  = format_context(chunks)
    system   = SYSTEM_PROMPT.format(repo=repo, context=context)
    messages = [{'role': m['role'], 'content': m['content']} for m in history[-6:]]
    messages.append({'role': 'user', 'content': question})

    resp   = client.chat.completions.create(
        model='llama-3.3-70b-versatile',
        max_tokens=1500,
        messages=[{'role': 'system', 'content': system}] + messages
    )
    answer = resp.choices[0].message.content

    if answer.strip().startswith('INSUFFICIENT_CONTEXT'):
        return {
            'answer':      answer.replace('INSUFFICIENT_CONTEXT:', '').strip(),
            'sources':     [],
            'is_fallback': True,
            'best_score':  best_score,
        }

    verification = verify_citations(answer, chunks)
    final_answer = verification.get('verified_answer', answer)
    citations_valid = verification.get('valid', True)

    return {
        'answer':           final_answer,
        'sources':          dedupe_sources(chunks),
        'is_fallback':      False,
        'best_score':       best_score,
        'citations_valid':  citations_valid,
        'invalid_citations': verification.get('invalid_citations', []),
    }