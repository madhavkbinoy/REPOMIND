import json
from pathlib import Path
from ingestion.chunker  import chunk_issue, chunk_pr
from ingestion.embedder import upsert_chunks
from db.setup           import create_collection, init_sqlite

REPO       = 'kubernetes/kubernetes'
COLLECTION = REPO.replace('/', '_')
RAW_DIR    = Path('./data/raw/kubernetes_kubernetes')

init_sqlite()
create_collection(REPO)

all_chunks = []

issue_files = list((RAW_DIR / 'issues').glob('*.json'))[:200]
for path in issue_files:
    issue = json.loads(path.read_text())
    all_chunks.extend(chunk_issue(issue, REPO))

pr_files = list((RAW_DIR / 'prs').glob('*.json'))[:100]
for path in pr_files:
    pr = json.loads(path.read_text())
    all_chunks.extend(chunk_pr(pr, REPO))

print(f'Total chunks from {len(issue_files)} issues + {len(pr_files)} PRs: {len(all_chunks)}')
upsert_chunks(all_chunks, COLLECTION)
print('Done. Test retrieval before anything else.')