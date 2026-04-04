from celery import Celery
import os, json
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

celery = Celery('repomind', broker=os.getenv('REDIS_URL', 'redis://localhost:6379/0'))


@celery.task
def full_index_repo(repo: str):
    from ingestion.github_scraper import scrape_issues, scrape_prs
    from ingestion.chunker        import chunk_issue, chunk_pr
    from ingestion.embedder       import upsert_chunks
    from ingestion.linker         import build_links_from_prs
    from db.setup                 import create_collection

    owner, name = repo.split('/')
    collection  = repo.replace('/', '_')
    raw         = Path(f'./data/raw/{owner}_{name}')

    scrape_issues(owner, name)
    scrape_prs(owner, name)
    create_collection(repo)

    chunks = []
    for p in (raw / 'issues').glob('*.json'):
        chunks.extend(chunk_issue(json.loads(p.read_text()), repo))
    for p in (raw / 'prs').glob('*.json'):
        chunks.extend(chunk_pr(json.loads(p.read_text()), repo))

    print(f'Indexing {len(chunks)} chunks for {repo}')
    upsert_chunks(chunks, collection)
    build_links_from_prs(repo)
    print('Full index complete')


@celery.task
def update_issue_task(repo: str, number: int):
    import httpx
    from ingestion.chunker  import chunk_issue
    from ingestion.embedder import upsert_chunks
    token = os.getenv('GITHUB_TOKEN')
    hdrs  = {'Authorization': f'Bearer {token}'}
    issue = httpx.get(f'https://api.github.com/repos/{repo}/issues/{number}', headers=hdrs).json()
    cmts  = httpx.get(issue['comments_url'], headers=hdrs).json()
    issue['comments'] = {'nodes': [{'body': c['body'], 'author': {'login': c['user']['login']}} for c in cmts]}
    chunks = chunk_issue(issue, repo)
    upsert_chunks(chunks, repo.replace('/', '_'))