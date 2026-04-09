from .token_utils import count_tokens, truncate_to_tokens

MAX_TOKENS = 500
OVERLAP    = 50


def build_issue_prefix(issue: dict) -> str:
    labels = ', '.join(l['name'] for l in (issue.get('labels', {}).get('nodes') or []))
    return (
        f"[ISSUE #{issue['number']} - {issue.get('state', '?')}]\n"
        f"Title: {issue['title']}\n"
        f"Labels: {labels or 'none'}\n"
        f"---\n"
    )


def chunk_issue(issue: dict, repo: str) -> list[dict]:
    prefix = build_issue_prefix(issue)
    budget = MAX_TOKENS - count_tokens(prefix) - 10
    units  = []

    if issue.get('body'):
        author = (issue.get('author') or {}).get('login', '?')
        units.append(f"[{author}]: {issue['body']}")

    for c in (issue.get('comments', {}).get('nodes') or []):
        if c.get('body'):
            author = (c.get('author') or {}).get('login', '?')
            units.append(f"[{author}]: {c['body']}")

    chunks, current, idx = [], '', 0

    def flush(text):
        nonlocal idx
        if not text.strip():
            return
        chunks.append({
            'text':        prefix + text,
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

    for unit in units:
        unit_text = unit + '\n'
        if count_tokens(current) + count_tokens(unit_text) > budget:
            flush(current)
            current = truncate_to_tokens(current, OVERLAP) + unit_text
        else:
            current += unit_text
    flush(current)
    return chunks


def chunk_code_file(file_path: str, content: str, repo: str) -> list[dict]:
    """Chunk a Go source file into manageable pieces."""
    prefix = (
        f"[CODE FILE: {file_path}]\n"
        f"File path: {file_path}\n"
        f"---\n"
    )
    
    budget = MAX_TOKENS - count_tokens(prefix) - 10
    chunks = []
    
    lines = content.split('\n')
    current = ''
    idx = 0
    
    for line in lines:
        if count_tokens(current) + count_tokens(line) > budget:
            if current.strip():
                chunks.append({
                    'text':        prefix + current,
                    'source_type': 'code',
                    'repo':        repo,
                    'number':      None,
                    'title':       file_path.split('/')[-1],
                    'url':         f"https://github.com/{repo}/blob/master/{file_path}",
                    'labels':      [],
                    'state':       None,
                    'chunk_index': idx,
                    'file_path':   file_path,
                })
                idx += 1
            current = truncate_to_tokens(current, OVERLAP) + line + '\n'
        else:
            current += line + '\n'
    
    if current.strip():
        chunks.append({
            'text':        prefix + current,
            'source_type': 'code',
            'repo':        repo,
            'number':      None,
            'title':       file_path.split('/')[-1],
            'url':         f"https://github.com/{repo}/blob/master/{file_path}",
            'labels':      [],
            'state':       None,
            'chunk_index': idx,
            'file_path':   file_path,
        })
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
    
    return [{
        'text':        prefix + message,
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
    labels = ', '.join(l['name'] for l in (pr.get('labels', {}).get('nodes') or []))
    files  = [f['path'] for f in (pr.get('files', {}).get('nodes') or [])][:5]
    files_str = ', '.join(files) + ('...' if len(files) == 5 else '')
    return (
        f"[PR #{pr['number']} - {pr.get('state', '?')}]\n"
        f"Title: {pr['title']}\n"
        f"Labels: {labels or 'none'}\n"
        f"Files: {files_str or 'none'}\n"
        f"---\n"
    )


def chunk_pr(pr: dict, repo: str) -> list[dict]:
    prefix = build_pr_prefix(pr)
    budget = MAX_TOKENS - count_tokens(prefix) - 10
    units  = []

    if pr.get('body'):
        author = (pr.get('author') or {}).get('login', '?')
        units.append(f"[{author} - description]: {pr['body']}")

    for review in (pr.get('reviews', {}).get('nodes') or []):
        if review.get('body'):
            author = (review.get('author') or {}).get('login', '?')
            units.append(f"[{author} - review {review.get('state', '')}]: {review['body']}")
        for rc in (review.get('comments', {}).get('nodes') or []):
            if rc.get('body'):
                path = rc.get('path', '')
                units.append(f"[inline comment on {path}]: {rc['body']}")

    for c in (pr.get('comments', {}).get('nodes') or []):
        if c.get('body'):
            author = (c.get('author') or {}).get('login', '?')
            units.append(f"[{author}]: {c['body']}")

    chunks, current, idx = [], '', 0

    def flush(text):
        nonlocal idx
        if not text.strip():
            return
        chunks.append({
            'text':        prefix + text,
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

    for unit in units:
        unit_text = unit + '\n'
        if count_tokens(current) + count_tokens(unit_text) > budget:
            flush(current)
            current = truncate_to_tokens(current, OVERLAP) + unit_text
        else:
            current += unit_text
    flush(current)
    return chunks