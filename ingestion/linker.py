import json, re, sqlite3, os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

db          = sqlite3.connect(os.getenv('DATABASE_PATH', './db/repomind.db'))
FILE_PREFIX = os.getenv('KUBELET_FILE_PREFIX', 'pkg/kubelet')

CLOSES_RE = re.compile(
    r'(?:closes|fixes|resolves|fix|close|resolve)\s+#(\d+)',
    re.IGNORECASE
)


def build_links_from_prs(repo: str):
    pr_dir = Path(f'./data/raw/{repo.replace("/","_")}/prs')
    if not pr_dir.exists():
        print(f'No PR data found for {repo}')
        return
    count = 0
    for pr_file in pr_dir.glob('*.json'):
        pr        = json.loads(pr_file.read_text())
        pr_number = pr['number']
        files     = [f['path'] for f in (pr.get('files', {}).get('nodes') or [])]
        body      = pr.get('body') or ''
        issue_refs = [int(n) for n in CLOSES_RE.findall(body)]
        for file_path in files:
            if not file_path.startswith(FILE_PREFIX):
                continue
            db.execute(
                'INSERT OR IGNORE INTO code_issue_links (repo,file_path,pr_number,link_type) VALUES (?,?,?,?)',
                (repo, file_path, pr_number, 'modified_by')
            )
            count += 1
            for issue_num in issue_refs:
                db.execute(
                    'INSERT OR IGNORE INTO code_issue_links (repo,file_path,pr_number,issue_number,link_type) VALUES (?,?,?,?,?)',
                    (repo, file_path, pr_number, issue_num, 'fixed_by')
                )
                count += 1
    db.commit()
    print(f'Built {count} link table entries for {repo}')


if __name__ == '__main__':
    build_links_from_prs('kubernetes/kubernetes')