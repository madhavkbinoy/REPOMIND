import os
import json
import time
import httpx
from pathlib import Path
from datetime import datetime

TOKEN = os.getenv('GITHUB_TOKEN') or 'your_github_token_here'
HEADERS = {'Authorization': f'Bearer {TOKEN}', 'Content-Type': 'application/json'}
RAW_DIR = Path('./data/raw/kubernetes_kubernetes')
MAX_COMMITS = 15000

RAW_DIR.mkdir(parents=True, exist_ok=True)
commits_dir = RAW_DIR / 'commits'
commits_dir.mkdir(exist_ok=True)

COMMIT_QUERY = '''
query($owner: String!, $repo: String!, $cursor: String) {
  repository(owner: $owner, name: $repo) {
    defaultBranchRef {
      target {
        ... on Commit {
          history(first: 30, after: $cursor, path: "pkg/kubelet") {
            pageInfo { hasNextPage endCursor }
            nodes {
              oid
              message
              author { name email date }
              committedDate
            }
          }
        }
      }
    }
  }
}
'''

def check_rate_limit():
    resp = httpx.get('https://api.github.com/rate_limit', headers=HEADERS, timeout=10)
    data = resp.json()
    return data['resources']['graphql']['remaining'], data['resources']['graphql']['reset']

def graphql(query: str, variables: dict):
    while True:
        remaining, reset_ts = check_rate_limit()
        if remaining < 10:
            wait = reset_ts - datetime.now().timestamp()
            if wait > 0:
                print(f'Rate limit low ({remaining}), waiting {wait/60:.0f} min...')
                time.sleep(wait + 5)
            continue
        
        resp = httpx.post(
            'https://api.github.com/graphql',
            headers=HEADERS,
            json={'query': query, 'variables': variables},
            timeout=30
        )
        data = resp.json()
        if 'errors' in data:
            print(f'GraphQL errors: {data["errors"]}')
            time.sleep(5)
            continue
        return data['data']

def scrape_commits():
    cursor, page, total, skipped = None, 0, 0, 0
    start_time = datetime.now()
    
    while True:
        print(f'Fetching commits page {page + 1}...')
        data = graphql(COMMIT_QUERY, {
            'owner': 'kubernetes',
            'repo': 'kubernetes',
            'cursor': cursor
        })
        
        history = data['repository']['defaultBranchRef']['target']['history']
        info = history['pageInfo']
        nodes = history['nodes']
        
        for commit in nodes:
            path = commits_dir / f"{commit['oid']}.json"
            if path.exists():
                skipped += 1
                continue
            path.write_text(json.dumps(commit, indent=2))
            total += 1
            if total >= MAX_COMMITS:
                elapsed = (datetime.now() - start_time).total_seconds()
                print(f'Reached MAX_COMMITS={MAX_COMMITS}. Done. Total time: {elapsed/60:.1f} min')
                return
        
        page += 1
        print(f'Page {page}: {len(nodes)} fetched, {total} saved, {skipped} skipped')
        
        if not info['hasNextPage']:
            elapsed = (datetime.now() - start_time).total_seconds()
            print(f'Commit scrape complete: {total} kubelet commits saved. Total time: {elapsed/60:.1f} min')
            return
        
        cursor = info['endCursor']
        time.sleep(0.5)

if __name__ == '__main__':
    print(f'Starting commit scraping at {datetime.now()}')
    print(f'Output directory: {commits_dir}')
    scrape_commits()
