import os, json, time, httpx
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

TOKEN      = os.getenv('GITHUB_TOKEN')
RAW_DIR    = Path('./data/raw')
MAX_ISSUES = int(os.getenv('MAX_ISSUES', 500))
MAX_PRS    = int(os.getenv('MAX_PRS', 200))

_RAW_LABELS    = os.getenv('KUBELET_ISSUE_LABELS', 'area/kubelet')
KUBELET_LABELS = [l.strip() for l in _RAW_LABELS.split(',') if l.strip()]
QUERY_LABEL    = KUBELET_LABELS[0]

FILE_PREFIX = os.getenv('KUBELET_FILE_PREFIX', 'pkg/kubelet')

HEADERS = {
    'Authorization': f'Bearer {TOKEN}',
    'Content-Type':  'application/json',
}

ISSUES_QUERY = '''
query($owner: String!, $repo: String!, $cursor: String, $label: String!) {
  repository(owner: $owner, name: $repo) {
    issues(first: 50, after: $cursor,
           labels: [$label],
           orderBy: {field: UPDATED_AT, direction: DESC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number title body state closedAt
        author { login }
        labels(first: 10) { nodes { name } }
        comments(first: 100) {
          nodes { author { login } body createdAt }
        }
      }
    }
  }
}
'''

PRS_QUERY = '''
query($owner: String!, $repo: String!, $cursor: String) {
  repository(owner: $owner, name: $repo) {
    pullRequests(first: 30, after: $cursor, states: [MERGED, CLOSED]) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number title body state mergedAt
        author { login }
        labels(first: 10) { nodes { name } }
        files(first: 50) { nodes { path } }
        reviews(first: 50) {
          nodes {
            author { login } body state submittedAt
            comments(first: 30) { nodes { body path position } }
          }
        }
        comments(first: 100) { nodes { author { login } body createdAt } }
      }
    }
  }
}
'''


def graphql(query: str, variables: dict) -> dict:
    resp = httpx.post(
        'https://api.github.com/graphql',
        headers=HEADERS,
        json={'query': query, 'variables': variables},
        timeout=30
    )
    resp.raise_for_status()
    data = resp.json()
    if 'errors' in data:
        raise RuntimeError(data['errors'])
    return data['data']


def _issue_matches_labels(issue: dict) -> bool:
    """Return True if the issue has at least one of our target kubelet labels."""
    issue_labels = {l['name'] for l in (issue.get('labels', {}).get('nodes') or [])}
    return bool(issue_labels & set(KUBELET_LABELS))


def scrape_issues(owner: str, repo: str):
    out = RAW_DIR / f'{owner}_{repo}' / 'issues'
    out.mkdir(parents=True, exist_ok=True)
    cursor, page, total, skipped = None, 0, 0, 0
    while True:
        data  = graphql(ISSUES_QUERY, {
            'owner':  owner,
            'repo':   repo,
            'cursor': cursor,
            'label':  QUERY_LABEL,
        })
        info  = data['repository']['issues']['pageInfo']
        nodes = data['repository']['issues']['nodes']
        for issue in nodes:
            if not _issue_matches_labels(issue):
                skipped += 1
                continue
            path = out / f"{issue['number']}.json"
            if not path.exists():
                path.write_text(json.dumps(issue, indent=2))
            total += 1
            if total >= MAX_ISSUES:
                print(f'Reached MAX_ISSUES={MAX_ISSUES} cap. Done.')
                return
        page += 1
        print(f'Issues page {page}: {len(nodes)} fetched, {total} kept, {skipped} skipped')
        if not info['hasNextPage']:
            break
        cursor = info['endCursor']
        time.sleep(0.3)
    print(f'Issues scrape complete: {total} kubelet issues saved.')


def _pr_touches_kubelet(pr: dict) -> bool:
    """Return True if at least one file in the PR is under pkg/kubelet/."""
    files = [f['path'] for f in (pr.get('files', {}).get('nodes') or [])]
    return any(f.startswith(FILE_PREFIX) for f in files)


def scrape_prs(owner: str, repo: str):
    out = RAW_DIR / f'{owner}_{repo}' / 'prs'
    out.mkdir(parents=True, exist_ok=True)
    cursor, page, total, skipped = None, 0, 0, 0
    while True:
        data  = graphql(PRS_QUERY, {'owner': owner, 'repo': repo, 'cursor': cursor})
        info  = data['repository']['pullRequests']['pageInfo']
        nodes = data['repository']['pullRequests']['nodes']
        for pr in nodes:
            if not _pr_touches_kubelet(pr):
                skipped += 1
                continue
            path = out / f"{pr['number']}.json"
            if not path.exists():
                path.write_text(json.dumps(pr, indent=2))
            total += 1
            if total >= MAX_PRS:
                print(f'Reached MAX_PRS={MAX_PRS} cap. Done.')
                return
        page += 1
        print(f'PRs page {page}: {len(nodes)} fetched, {total} kept, {skipped} skipped')
        if not info['hasNextPage']:
            break
        cursor = info['endCursor']
        time.sleep(0.5)
    print(f'PRs scrape complete: {total} kubelet PRs saved.')


if __name__ == '__main__':
    owner, repo = 'kubernetes', 'kubernetes'
    scrape_issues(owner, repo)
    scrape_prs(owner, repo)