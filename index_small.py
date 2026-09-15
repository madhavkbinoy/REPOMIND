import json
from pathlib import Path
from ingestion.chunker     import chunk_issue, chunk_pr, chunk_commit
from ingestion.kep_scraper import chunk_kep
from ingestion.embedder    import upsert_chunks
from db.setup              import create_collection, init_sqlite

REPO       = 'kubernetes/kubernetes'
COLLECTION = REPO.replace('/', '_')
RAW_DIR    = Path('./data/raw/kubernetes_kubernetes')

init_sqlite()
create_collection(REPO)

all_chunks = []


def index_dir(name: str, chunk_fn) -> int:
    """Chunk every JSON file in RAW_DIR/name. Returns the document count."""
    d = RAW_DIR / name
    if not d.exists():
        print(f'{name:10} skipped (no data)')
        return 0
    files = sorted(d.glob('*.json'))
    before = len(all_chunks)
    for path in files:
        all_chunks.extend(chunk_fn(json.loads(path.read_text()), REPO))
    print(f'{name:10} {len(files):>6} docs -> {len(all_chunks) - before:>6} chunks')
    return len(files)


index_dir('issues',    chunk_issue)
index_dir('prs',       chunk_pr)
index_dir('commits',   chunk_commit)
index_dir('keps',      chunk_kep)
index_dir('community', chunk_kep)

print(f'\nTotal chunks: {len(all_chunks)}')
upsert_chunks(all_chunks, COLLECTION)
print('Done. Test retrieval before anything else.')
