import re

from .token_utils import (count_tokens, truncate_to_tokens, split_by_tokens,
                          EMBED_MAX_TOKENS)

# Sized to the embedding model, not to taste. The previous 500 was nearly double
# all-MiniLM-L6-v2's 256-token limit, so 88% of chunks were silently truncated at
# encode time and vector search only ever saw their opening ~36%.
# tests/test_core.py asserts MAX_TOKENS <= EMBED_MAX_TOKENS so this cannot drift again.
MAX_TOKENS = EMBED_MAX_TOKENS
OVERLAP    = 32

# Kubernetes PRs are heavily automated. Measured over 12,016 sampled PR comments:
# 19.9% are bot-authored, 27.9% are Prow slash commands, and 17.2% are sub-60-character
# replies ("+1", "ping", "done"). Only 35% carry actual discussion -- but they hold 49%
# of the text. Indexing the rest buries real rationale under thousands of near-identical
# "/lgtm" chunks that match everything and mean nothing.
BOT_AUTHORS = re.compile(
    r'^(k8s-ci-robot|k8s-github-robot|k8s-triage-robot|k8s-reviewable|fejta-bot|'
    r'kubernetes-bot|openshift-ci(-robot)?|codecov(-io|-commenter)?|dependabot.*|'
    r'stale\[bot\]|github-actions)$', re.I)

PROW_COMMAND = re.compile(
    r'^\s*/(lgtm|approve|assign|unassign|retest|test|ok-to-test|hold|unhold|close|reopen|'
    r'remove-\S+|area|sig|kind|priority|triage|cc|uncc|milestone|override|skip|retitle|'
    r'release-note\S*|meow|woof|shrug|joke|dog|cat)\b', re.I)

MIN_COMMENT_CHARS = 40


def is_substantive(body: str, author: str = '') -> bool:
    """Keep only comments that could plausibly contain design rationale."""
    b = (body or '').strip()
    if len(b) < MIN_COMMENT_CHARS:
        return False
    if author and BOT_AUTHORS.match(author):
        return False
    if PROW_COMMAND.match(b):
        return False
    return True


# The prefix is metadata, not content -- but it is inside the embedded text, so every
# token it takes is a token of real discussion the model never sees. Kubernetes PRs
# carry ~10 process labels and very long file paths; unbudgeted, the header reached 221
# of the 256-token limit, leaving 25 tokens for the actual comment. Keep it compact.
PREFIX_MAX_TOKENS  = 64
TITLE_MAX_WORDS    = 14
MEANINGFUL_LABEL   = re.compile(r'^(area|kind|sig|component|triage)/', re.I)


def _short_title(title: str) -> str:
    words = (title or '').split()
    return ' '.join(words[:TITLE_MAX_WORDS]) + ('…' if len(words) > TITLE_MAX_WORDS else '')


def _useful_labels(node: dict, limit: int = 3) -> str:
    """Drop process labels (lgtm, approved, size/L, release-note) -- they describe the
    workflow, not the subject, and they are near-identical across thousands of PRs."""
    names = [l['name'] for l in (node.get('labels', {}).get('nodes') or [])]
    keep  = [n for n in names if MEANINGFUL_LABEL.match(n)][:limit]
    return ', '.join(keep)


def _fit_prefix(prefix: str) -> str:
    """Hard guarantee: the header can never starve the content budget."""
    return truncate_to_tokens(prefix, PREFIX_MAX_TOKENS)


def build_issue_prefix(issue: dict) -> str:
    labels = _useful_labels(issue)
    return _fit_prefix(
        f"[ISSUE #{issue['number']} - {issue.get('state', '?')}]\n"
        f"Title: {_short_title(issue['title'])}\n"
        f"Labels: {labels or 'none'}\n"
        f"---\n"
    )


def chunk_issue(issue: dict, repo: str) -> list[dict]:
    prefix = build_issue_prefix(issue)
    budget = max(MAX_TOKENS - count_tokens(prefix) - 10, 96)
    units  = []

    if issue.get('body'):
        author = (issue.get('author') or {}).get('login', '?')
        units.append(f"[{author}]: {issue['body']}")

    for c in (issue.get('comments', {}).get('nodes') or []):
        author = (c.get('author') or {}).get('login', '?')
        if is_substantive(c.get('body'), author):
            units.append(f"[{author}]: {c['body']}")

    chunks, current, idx = [], '', 0

    def flush(text):
        nonlocal idx
        if not text.strip():
            return
        chunks.append({
            'text':        truncate_to_tokens(prefix + text, MAX_TOKENS),
            'source_type': 'issue',
            'repo':        repo,
            'number':      issue['number'],
            'title':       issue['title'],
            'url':         f"https://github.com/{repo}/issues/{issue['number']}",
            'labels':      [l['name'] for l in (issue.get('labels', {}).get('nodes') or [])],
            'state':       issue.get('state'),
            'chunk_index': idx,
            'file_path':   None,
        })
        idx += 1

    # A single comment can exceed the whole budget on its own. Splitting each unit
    # first is what stops one long review turning into one oversized chunk.
    for unit in units:
        for piece in split_by_tokens(unit, budget - 1, overlap=OVERLAP):
            piece_text = piece + '\n'
            if count_tokens(current) + count_tokens(piece_text) > budget:
                flush(current)
                current = truncate_to_tokens(current, OVERLAP) + piece_text
            else:
                current += piece_text
    flush(current)
    return chunks


def chunk_commit(commit: dict, repo: str) -> list[dict]:
    """Chunk a commit into a single chunk."""
    message = commit.get('message', '')
    author = commit.get('author', {}) or {}
    author_name = author.get('name', 'unknown')
    committed_date = commit.get('committedDate', '')
    oid = commit.get('oid', '')
    
    prefix = (
        f"[COMMIT {oid[:8]}]\n"
        f"Author: {author_name}\n"
        f"Date: {committed_date}\n"
        f"---\n"
    )
    
    # Release/merge commits can carry enormous messages; one reached 56,086 tokens.
    body = truncate_to_tokens(message, MAX_TOKENS - count_tokens(prefix) - 5)

    return [{
        'text':        prefix + body,
        'source_type': 'commit',
        'repo':        repo,
        'number':      None,
        'title':       message.split('\n')[0][:100] if message else 'No message',
        'url':         f"https://github.com/{repo}/commit/{oid}",
        'labels':      [],
        'state':       None,
        'chunk_index': 0,
        'file_path':   None,
    }]



def build_pr_prefix(pr: dict) -> str:
    labels = _useful_labels(pr)
    # Basenames only: 'staging/src/k8s.io/apiserver/pkg/endpoints/filters/x.go' costs ~18
    # tokens and 'x.go' carries the part a question would actually mention.
    files  = [f['path'].rsplit('/', 1)[-1] for f in (pr.get('files', {}).get('nodes') or [])][:3]
    return _fit_prefix(
        f"[PR #{pr['number']} - {pr.get('state', '?')}]\n"
        f"Title: {_short_title(pr['title'])}\n"
        f"Labels: {labels or 'none'}\n"
        f"Files: {', '.join(files) or 'none'}\n"
        f"---\n"
    )


def chunk_pr(pr: dict, repo: str) -> list[dict]:
    prefix = build_pr_prefix(pr)
    budget = max(MAX_TOKENS - count_tokens(prefix) - 10, 96)
    units  = []

    if pr.get('body'):
        author = (pr.get('author') or {}).get('login', '?')
        units.append(f"[{author} - description]: {pr['body']}")

    for review in (pr.get('reviews', {}).get('nodes') or []):
        author = (review.get('author') or {}).get('login', '?')
        if is_substantive(review.get('body'), author):
            units.append(f"[{author} - review {review.get('state', '')}]: {review['body']}")
        for rc in (review.get('comments', {}).get('nodes') or []):
            if is_substantive(rc.get('body')):
                path = rc.get('path', '')
                units.append(f"[inline comment on {path}]: {rc['body']}")

    for c in (pr.get('comments', {}).get('nodes') or []):
        author = (c.get('author') or {}).get('login', '?')
        if is_substantive(c.get('body'), author):
            units.append(f"[{author}]: {c['body']}")

    chunks, current, idx = [], '', 0

    def flush(text):
        nonlocal idx
        if not text.strip():
            return
        chunks.append({
            'text':        truncate_to_tokens(prefix + text, MAX_TOKENS),
            'source_type': 'pr',
            'repo':        repo,
            'number':      pr['number'],
            'title':       pr['title'],
            'url':         f"https://github.com/{repo}/pull/{pr['number']}",
            'labels':      [l['name'] for l in (pr.get('labels', {}).get('nodes') or [])],
            'state':       pr.get('state'),
            'chunk_index': idx,
            'file_path':   None,
        })
        idx += 1

    # A single comment can exceed the whole budget on its own. Splitting each unit
    # first is what stops one long review turning into one oversized chunk.
    for unit in units:
        for piece in split_by_tokens(unit, budget - 1, overlap=OVERLAP):
            piece_text = piece + '\n'
            if count_tokens(current) + count_tokens(piece_text) > budget:
                flush(current)
                current = truncate_to_tokens(current, OVERLAP) + piece_text
            else:
                current += piece_text
    flush(current)
    return chunks