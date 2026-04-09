import json
from pathlib import Path
from ingestion.chunker  import chunk_issue, chunk_pr, chunk_commit
from ingestion.embedder import upsert_chunks
from db.setup           import create_collection, init_sqlite

REPO       = 'kubernetes/kubernetes'
COLLECTION = REPO.replace('/', '_')
RAW_DIR    = Path('./data/raw/kubernetes_kubernetes')

init_sqlite()
create_collection(REPO)

all_chunks = []

# Index issues
issue_files = list((RAW_DIR / 'issues').glob('*.json'))
for path in issue_files:
    issue = json.loads(path.read_text())
    all_chunks.extend(chunk_issue(issue, REPO))
print(f'Indexed {len(issue_files)} issues')

# Index PRs
pr_files = list((RAW_DIR / 'prs').glob('*.json'))
for path in pr_files:
    pr = json.loads(path.read_text())
    all_chunks.extend(chunk_pr(pr, REPO))
print(f'Indexed {len(pr_files)} PRs')

# Index commits
commits_dir = RAW_DIR / 'commits'
if commits_dir.exists():
    commit_files = list(commits_dir.glob('*.json'))
    for path in commit_files:
        commit = json.loads(path.read_text())
        all_chunks.extend(chunk_commit(commit, REPO))
    print(f'Indexed {len(commit_files)} commits')

print(f'Total chunks: {len(all_chunks)}')
upsert_chunks(all_chunks, COLLECTION)
print('Done. Test retrieval before anything else.')