"""
Per-IP rate limiting for the public demo.

Every /api/chat call costs 2-3 Groq requests (rewrite, answer, verify) against a
shared key. Without a cap, one script turns a portfolio demo into someone else's
free LLM endpoint.

In-memory and per-process, which is fine for a single-machine deployment. Multiple
machines would need Redis -- but the deployment runs one machine on purpose.
"""
import os, time
from collections import defaultdict, deque

from fastapi import HTTPException, Request

WINDOW    = int(os.getenv('RATE_WINDOW_SECONDS', 3600))
MAX_CALLS = int(os.getenv('RATE_MAX_CALLS', 30))

_hits: dict[str, deque] = defaultdict(deque)


def _client_ip(request: Request) -> str:
    # Fly (and most proxies) put the real client first in X-Forwarded-For.
    fwd = request.headers.get('x-forwarded-for')
    if fwd:
        return fwd.split(',')[0].strip()
    return request.client.host if request.client else 'unknown'


def rate_limit(request: Request):
    """FastAPI dependency. Raises 429 with a Retry-After once the window is full."""
    ip  = _client_ip(request)
    now = time.time()
    q   = _hits[ip]

    while q and now - q[0] > WINDOW:
        q.popleft()

    if len(q) >= MAX_CALLS:
        retry = int(WINDOW - (now - q[0])) + 1
        raise HTTPException(
            status_code=429,
            detail=f'Rate limit reached ({MAX_CALLS} questions per '
                   f'{WINDOW // 60} minutes). Try again in {retry // 60 + 1} minutes.',
            headers={'Retry-After': str(retry)},
        )

    q.append(now)

    # Bound memory: drop IPs whose windows have fully expired.
    if len(_hits) > 10_000:
        for k in [k for k, v in _hits.items() if not v or now - v[-1] > WINDOW]:
            del _hits[k]
