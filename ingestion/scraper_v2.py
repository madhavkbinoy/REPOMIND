"""
Complete, cheap scrape of everything that ever touched pkg/kubelet.

Why this exists
---------------
The original github_scraper.py walks the repository's entire merged/closed PR
history (100k+ PRs) and filters client-side for pkg/kubelet. That is both
incomplete in practice (it stops at MAX_PRS, whichever ones it happened to find
first) and enormously expensive: each page requests tens of thousands of GraphQL
nodes against a 5,000 point/hour budget.

This version moves the expensive enumeration step off the API entirely:

  1. A blobless git clone gives the COMPLETE commit set for the path via
     `git log -- pkg/kubelet` -- no API calls, no rate limit, no pagination.
  2. Merge-commit messages yield the PR numbers that touched the path.
     Commits whose PR cannot be read from the message are batch-resolved
     through associatedPullRequests.
  3. Those specific PRs are fetched by number, batched with GraphQL aliases.
  4. Issues come from two unioned sources: date-sliced label search across ALL
     configured labels, plus `fixes #N` back-references from the PRs above
     (which catches kubelet issues that were never labelled).

Output JSON shapes are identical to the original scrapers, so index_small.py
and ingestion/chunker.py work unchanged.

Usage
-----
    python ingestion/scraper_v2.py all        # clone -> commits -> prs -> issues
    python ingestion/scraper_v2.py commits    # stages can be run individually
    python ingestion/scraper_v2.py prs
    python ingestion/scraper_v2.py issues
"""
import os, re, json, sys, time, subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv()

TOKEN       = os.getenv('GITHUB_TOKEN')
OWNER, NAME = 'kubernetes', 'kubernetes'
REPO_SLUG   = f'{OWNER}/{NAME}'

RAW_DIR     = Path(f'./data/raw/{OWNER}_{NAME}')
CLONE_DIR   = Path(os.getenv('CLONE_DIR', './data/repo-clone'))
FILE_PREFIX = os.getenv('KUBELET_FILE_PREFIX', 'pkg/kubelet')

_RAW_LABELS = os.getenv('KUBELET_ISSUE_LABELS', 'area/kubelet')
LABELS      = [l.strip() for l in _RAW_LABELS.split(',') if l.strip()]

HEADERS  = {'Authorization': f'Bearer {TOKEN}', 'Content-Type': 'application/json'}
ENDPOINT = 'https://api.github.com/graphql'

PR_BATCH    = 12     # PRs per aliased request
OID_BATCH   = 40     # commit oids per associatedPullRequests request
SEARCH_CAP  = 1000   # GitHub's hard cap on any single search query


# --------------------------------------------------------------------------
# GraphQL plumbing
# --------------------------------------------------------------------------

# GitHub prices GraphQL on REQUESTED capacity, not on what comes back, and nested
# connections multiply: the original scraper's reviews(50){comments(30)} asks for
# 1,500 node slots per PR to hold what is typically ~10 real review comments.
# Across 6,268 PRs that is roughly 105,000 points -- about 21 hours of budget.
#
# These limits are generous enough for the overwhelming majority of PRs at roughly
# a third of that. totalCount is free (it does not count toward node cost) and is
# recorded on every connection so truncation is measured rather than silent --
# see the truncation report at the end of the PR stage.
PR_FIELDS = '''
fragment prFields on PullRequest {
  number title body state mergedAt
  author { login }
  labels(first: 10) { nodes { name } }
  files(first: 20) { totalCount nodes { path } }
  reviews(first: 30) {
    totalCount
    nodes {
      author { login } body state submittedAt
      comments(first: 15) { totalCount nodes { body path position } }
    }
  }
  comments(first: 60) { totalCount nodes { author { login } body createdAt } }
}
'''

ISSUE_FIELDS = '''
fragment issueFields on Issue {
  number title body state closedAt
  author { login }
  labels(first: 10) { nodes { name } }
  comments(first: 50) { nodes { author { login } body createdAt } }
}
'''

RATE = 'rateLimit { cost remaining resetAt }'


def graphql(query: str, variables: dict = None, attempt: int = 0) -> dict:
    """POST a query, honouring the GraphQL point budget and retrying transients."""
    resp = httpx.post(
        ENDPOINT, headers=HEADERS,
        json={'query': query, 'variables': variables or {}},
        timeout=60,
    )

    if resp.status_code in (502, 503, 504) and attempt < 4:
        time.sleep(2 ** attempt)
        return graphql(query, variables, attempt + 1)

    resp.raise_for_status()
    data = resp.json()

    if 'errors' in data:
        msg = json.dumps(data['errors'])
        # Secondary rate limits and timeouts are retryable; schema errors are not.
        if attempt < 4 and any(k in msg.upper() for k in ('RATE_LIMIT', 'TIMEOUT', 'SERVICE_UNAVAILABLE')):
            time.sleep(30 * (attempt + 1))
            return graphql(query, variables, attempt + 1)
        if not data.get('data'):
            raise RuntimeError(msg)
        print(f'  ! partial errors: {msg[:200]}')

    _respect_budget(data.get('data') or {})
    return data['data']


def _respect_budget(data: dict):
    """Sleep until reset when the hourly point budget is nearly spent."""
    rl = data.get('rateLimit')
    if not rl or rl.get('remaining') is None:
        return
    if rl['remaining'] > 150:
        return
    reset = datetime.fromisoformat(rl['resetAt'].replace('Z', '+00:00'))
    wait  = (reset - datetime.now(timezone.utc)).total_seconds() + 5
    if wait > 0:
        print(f'  rate limit low ({rl["remaining"]} pts), sleeping {wait/60:.1f} min')
        time.sleep(wait)


def _write(path: Path, payload: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2))


# --------------------------------------------------------------------------
# Stage 1 -- commits, straight out of git
# --------------------------------------------------------------------------

def ensure_clone():
    """Blobless clone: all commits and trees, no file contents. A few GB, once."""
    if (CLONE_DIR / '.git').exists():
        print(f'Clone exists at {CLONE_DIR}, fetching updates...')
        subprocess.run(['git', '-C', str(CLONE_DIR), 'fetch', '--all', '--quiet'], check=True)
        return

    CLONE_DIR.parent.mkdir(parents=True, exist_ok=True)
    print(f'Cloning {REPO_SLUG} (blobless) into {CLONE_DIR} -- this takes a few minutes...')
    subprocess.run([
        'git', 'clone', '--filter=blob:none', '--no-checkout',
        f'https://github.com/{REPO_SLUG}.git', str(CLONE_DIR),
    ], check=True)
    print('Clone complete.')


# oid \0 author \0 iso-date \0 full message  <RS>
_LOG_FORMAT = '%H%x00%an%x00%aI%x00%B%x1e'


def read_commits(first_parent: bool = None) -> list[dict]:
    """Commits that touched FILE_PREFIX. Complete, and free.

    Measured on kubernetes/kubernetes: 13,562 commits touch pkg/kubelet, but only
    6,272 are on the mainline (6,237 merges + 35 direct commits). The other ~7,290
    are intra-PR branch commits -- 'address review feedback', 'rebase', 'fix lint'.
    They belong to PRs already identified by their merge commit, so including them
    costs ~200 wasted API requests during PR resolution and fills the corpus with
    work-in-progress noise.

    First-parent is therefore the default. Set COMMITS_FIRST_PARENT=false to index
    the full set -- worth doing once as a Phase 1 ablation row.
    """
    if first_parent is None:
        first_parent = os.getenv('COMMITS_FIRST_PARENT', 'true').lower() != 'false'

    cmd = ['git', '-C', str(CLONE_DIR), 'log', f'--format={_LOG_FORMAT}']
    if first_parent:
        cmd.append('--first-parent')
    cmd += ['--', FILE_PREFIX]

    out = subprocess.run(
        cmd, check=True, capture_output=True, text=True, errors='replace',
    ).stdout

    commits = []
    for record in out.split('\x1e'):
        record = record.strip('\n')
        if not record.strip():
            continue
        parts = record.split('\x00')
        if len(parts) < 4:
            continue
        oid, author, date, message = parts[0], parts[1], parts[2], parts[3]
        commits.append({
            'oid':           oid,
            'message':       message.strip(),
            'author':        {'name': author, 'email': '', 'date': date},
            'committedDate': date,
        })
    return commits


def stage_commits() -> list[dict]:
    ensure_clone()
    commits = read_commits()
    out_dir = RAW_DIR / 'commits'
    written = 0
    for c in commits:
        path = out_dir / f"{c['oid']}.json"
        if not path.exists():
            _write(path, c)
            written += 1
    print(f'Commits: {len(commits)} touching {FILE_PREFIX} ({written} newly written)')
    return commits


# --------------------------------------------------------------------------
# Stage 2 -- PRs, derived from those commits
# --------------------------------------------------------------------------

MERGE_RE  = re.compile(r'Merge pull request #(\d+)', re.IGNORECASE)
SQUASH_RE = re.compile(r'\(#(\d+)\)\s*$')            # GitHub squash convention


def pr_numbers_from_commits(commits: list[dict]) -> tuple[set[int], list[str]]:
    """Read PR numbers out of commit messages; return those plus unresolved oids."""
    found, unresolved = set(), []
    for c in commits:
        msg     = c['message']
        subject = msg.split('\n', 1)[0]
        m = MERGE_RE.search(msg) or SQUASH_RE.search(subject)
        if m:
            found.add(int(m.group(1)))
        else:
            unresolved.append(c['oid'])
    return found, unresolved


def resolve_via_api(oids: list[str]) -> set[int]:
    """Batch-resolve commits whose PR number isn't in the message."""
    found = set()
    for i in range(0, len(oids), OID_BATCH):
        batch   = oids[i:i + OID_BATCH]
        aliases = '\n'.join(
            f'c{j}: object(oid: "{oid}") {{ ... on Commit {{ '
            f'associatedPullRequests(first: 3) {{ nodes {{ number }} }} }} }}'
            for j, oid in enumerate(batch)
        )
        query = f'query {{ {RATE} repository(owner: "{OWNER}", name: "{NAME}") {{ {aliases} }} }}'
        data  = graphql(query)
        repo  = data.get('repository') or {}
        for node in repo.values():
            for pr in ((node or {}).get('associatedPullRequests', {}) or {}).get('nodes', []) or []:
                found.add(pr['number'])
        print(f'  resolved {min(i + OID_BATCH, len(oids))}/{len(oids)} unmatched commits '
              f'-> {len(found)} PRs so far')
    return found


def _truncations(pr: dict) -> list[str]:
    """Which connections on this PR hit their requested limit."""
    hit = []
    if (pr.get('files') or {}).get('totalCount', 0) > 20:
        hit.append('files')
    reviews = pr.get('reviews') or {}
    if reviews.get('totalCount', 0) > 30:
        hit.append('reviews')
    if any((r.get('comments') or {}).get('totalCount', 0) > 15
           for r in (reviews.get('nodes') or [])):
        hit.append('review_comments')
    if (pr.get('comments') or {}).get('totalCount', 0) > 60:
        hit.append('comments')
    return hit


def fetch_prs(numbers: list[int]):
    """Fetch specific PRs by number, batched with aliases."""
    out_dir = RAW_DIR / 'prs'
    pending = [n for n in sorted(numbers) if not (out_dir / f'{n}.json').exists()]
    print(f'PRs: {len(numbers)} referenced, {len(pending)} to fetch')

    clipped: dict[str, list[int]] = {}
    for i in range(0, len(pending), PR_BATCH):
        batch   = pending[i:i + PR_BATCH]
        aliases = '\n'.join(f'p{j}: pullRequest(number: {n}) {{ ...prFields }}'
                            for j, n in enumerate(batch))
        query = (f'{PR_FIELDS} query {{ {RATE} '
                 f'repository(owner: "{OWNER}", name: "{NAME}") {{ {aliases} }} }}')
        data  = graphql(query)
        repo  = data.get('repository') or {}
        for node in repo.values():
            if not (node and node.get('number')):
                continue
            _write(out_dir / f"{node['number']}.json", node)
            for field in _truncations(node):
                clipped.setdefault(field, []).append(node['number'])
        print(f'  {min(i + PR_BATCH, len(pending))}/{len(pending)} PRs fetched')

    if clipped:
        print('\n--- truncation report ---')
        for field, prs in sorted(clipped.items()):
            print(f'  {field:16} clipped on {len(prs)} PRs, e.g. {prs[:5]}')
        _write(RAW_DIR / 'truncated_prs.json', clipped)
        print(f'  full list written to {RAW_DIR / "truncated_prs.json"}')
    else:
        print('\nNo PR exceeded its connection limits -- corpus is complete.')


def stage_prs(commits: list[dict]) -> set[int]:
    from_msgs, unresolved = pr_numbers_from_commits(commits)
    print(f'PR numbers from commit messages: {len(from_msgs)} '
          f'({len(unresolved)} commits unmatched)')

    numbers = set(from_msgs)
    if unresolved:
        numbers |= resolve_via_api(unresolved)

    fetch_prs(sorted(numbers))
    return numbers


# --------------------------------------------------------------------------
# Stage 3 -- issues, from label search plus PR back-references
# --------------------------------------------------------------------------

CLOSES_RE = re.compile(r'(?:closes|fixes|resolves|close|fix|resolve)\s+#(\d+)', re.IGNORECASE)

SEARCH_QUERY = ISSUE_FIELDS + '''
query($q: String!, $cursor: String) {
  ''' + RATE + '''
  search(type: ISSUE, query: $q, first: 50, after: $cursor) {
    issueCount
    pageInfo { hasNextPage endCursor }
    nodes { ... on Issue { ...issueFields } }
  }
}
'''


def _search_window(label: str, start: datetime, end: datetime, out_dir: Path) -> int:
    """Fetch one date window, splitting it if it exceeds GitHub's 1,000 result cap."""
    q = (f'repo:{REPO_SLUG} is:issue label:"{label}" '
         f'created:{start:%Y-%m-%d}..{end:%Y-%m-%d}')

    first = graphql(SEARCH_QUERY, {'q': q, 'cursor': None})
    total = first['search']['issueCount']

    if total > SEARCH_CAP and (end - start).days > 1:
        mid = start + (end - start) / 2
        return (_search_window(label, start, mid, out_dir)
                + _search_window(label, mid + timedelta(days=1), end, out_dir))

    if total == 0:
        return 0

    saved, page = 0, first['search']
    while True:
        for node in page['nodes']:
            if node and node.get('number'):
                _write(out_dir / f"{node['number']}.json", node)
                saved += 1
        if not page['pageInfo']['hasNextPage']:
            break
        page = graphql(SEARCH_QUERY,
                       {'q': q, 'cursor': page['pageInfo']['endCursor']})['search']

    print(f'  {label} {start:%Y-%m}..{end:%Y-%m}: {saved}/{total}')
    return saved


def fetch_issues_by_number(numbers: list[int], out_dir: Path):
    """Fetch issues referenced by PR bodies but never labelled."""
    pending = [n for n in sorted(numbers) if not (out_dir / f'{n}.json').exists()]
    if not pending:
        return
    print(f'Issues from PR back-references: {len(pending)} to fetch')

    for i in range(0, len(pending), PR_BATCH):
        batch   = pending[i:i + PR_BATCH]
        aliases = '\n'.join(f'i{j}: issue(number: {n}) {{ ...issueFields }}'
                            for j, n in enumerate(batch))
        query = (f'{ISSUE_FIELDS} query {{ {RATE} '
                 f'repository(owner: "{OWNER}", name: "{NAME}") {{ {aliases} }} }}')
        data = graphql(query)
        repo = data.get('repository') or {}
        for node in repo.values():
            if node and node.get('number'):
                _write(out_dir / f"{node['number']}.json", node)
        print(f'  {min(i + PR_BATCH, len(pending))}/{len(pending)} issues fetched')


def stage_issues(pr_numbers: set[int]):
    out_dir = RAW_DIR / 'issues'
    start   = datetime(2014, 1, 1)
    end     = datetime.now()

    for label in LABELS:
        print(f'Searching issues labelled {label}...')
        n = _search_window(label, start, end, out_dir)
        print(f'  {label}: {n} issues')

    # Issues referenced by the PRs we already have, which label search misses.
    referenced = set()
    pr_dir     = RAW_DIR / 'prs'
    if pr_dir.exists():
        for path in pr_dir.glob('*.json'):
            body = (json.loads(path.read_text()).get('body') or '')
            referenced.update(int(n) for n in CLOSES_RE.findall(body))
    fetch_issues_by_number(sorted(referenced), out_dir)


# --------------------------------------------------------------------------

def main():
    stage = sys.argv[1] if len(sys.argv) > 1 else 'all'
    t0    = time.time()

    # The commits stage is pure git — only the API stages need a token.
    if stage != 'commits' and (not TOKEN or TOKEN == 'your_github_token_here'):
        sys.exit('GITHUB_TOKEN is not set. Add it to .env before running.')

    if stage in ('all', 'commits'):
        commits = stage_commits()
    else:
        ensure_clone()
        commits = read_commits()

    pr_numbers = set()
    if stage in ('all', 'prs'):
        pr_numbers = stage_prs(commits)
    elif stage == 'issues':
        pr_numbers = {int(p.stem) for p in (RAW_DIR / 'prs').glob('*.json')}

    if stage in ('all', 'issues'):
        stage_issues(pr_numbers)

    if stage in ('all', 'deep'):
        stage_deep()

    for kind in ('commits', 'prs', 'issues'):
        d = RAW_DIR / kind
        print(f'{kind:8}: {len(list(d.glob("*.json"))) if d.exists() else 0}')
    print(f'Elapsed: {(time.time() - t0) / 60:.1f} min')


# --------------------------------------------------------------------------
# Stage 4 -- deep re-fetch of PRs whose discussion was clipped
# --------------------------------------------------------------------------

# Only used for the handful of PRs the truncation report flagged. Running these
# limits across all 6,245 PRs would be wasteful; running them across the ~400
# that actually need it costs almost nothing.
PR_FIELDS_DEEP = '''
fragment prFields on PullRequest {
  number title body state mergedAt
  author { login }
  labels(first: 20) { nodes { name } }
  files(first: 100) { totalCount nodes { path } }
  reviews(first: 100) {
    totalCount
    nodes {
      author { login } body state submittedAt
      comments(first: 60) { totalCount nodes { body path position } }
    }
  }
  comments(first: 200) { totalCount nodes { author { login } body createdAt } }
}
'''

DEEP_BATCH = 4


def stage_deep():
    """Re-fetch PRs flagged by the truncation report, at much higher limits.

    'files' truncation is ignored: only the first 5 paths reach the chunk prefix
    and nothing downstream reads the rest, so a clipped file list costs nothing.
    Clipped reviews and comments are real lost discussion and are re-fetched.
    """
    report = RAW_DIR / 'truncated_prs.json'
    if not report.exists():
        print('No truncation report -- run the prs stage first.')
        return

    clipped = json.loads(report.read_text())
    targets = set()
    for field in ('reviews', 'review_comments', 'comments'):
        targets.update(clipped.get(field, []))

    print(f'Deep re-fetch: {len(targets)} PRs with clipped discussion '
          f'(ignoring {len(set(clipped.get("files", [])) - targets)} files-only)')

    out_dir = RAW_DIR / 'prs'
    todo    = sorted(targets)
    still   = {}
    for i in range(0, len(todo), DEEP_BATCH):
        batch   = todo[i:i + DEEP_BATCH]
        aliases = '\n'.join(f'p{j}: pullRequest(number: {n}) {{ ...prFields }}'
                            for j, n in enumerate(batch))
        query = (f'{PR_FIELDS_DEEP} query {{ {RATE} '
                 f'repository(owner: "{OWNER}", name: "{NAME}") {{ {aliases} }} }}')
        data = graphql(query)
        for node in (data.get('repository') or {}).values():
            if not (node and node.get('number')):
                continue
            _write(out_dir / f"{node['number']}.json", node)
            rv = (node.get('reviews') or {})
            if rv.get('totalCount', 0) > 100 or (node.get('comments') or {}).get('totalCount', 0) > 200:
                still[node['number']] = {'reviews': rv.get('totalCount'),
                                         'comments': (node.get('comments') or {}).get('totalCount')}
        if (i // DEEP_BATCH) % 20 == 0:
            print(f'  {min(i + DEEP_BATCH, len(todo))}/{len(todo)}')

    print(f'Deep re-fetch complete. Still clipped at the higher limits: {len(still)}')
    if still:
        print(' ', dict(list(still.items())[:5]))


if __name__ == '__main__':
    main()
