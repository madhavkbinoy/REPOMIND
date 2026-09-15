"""
Scrape Kubernetes Enhancement Proposals (KEPs) for sig-node, plus the sig-node
governance docs from kubernetes/community.

Why KEPs matter more than anything else in the corpus
-----------------------------------------------------
Issues and PRs capture argument; KEPs capture the *conclusion* of that argument in
a fixed template. Measured across the 126 sig-node KEPs in this repo:

    ## Motivation      124 KEPs
    ## Design Details  116 KEPs
    ## Alternatives    109 KEPs      <- rejected designs, written down, on purpose
    ## Drawbacks       103 KEPs

"Alternatives" is a section where maintainers record what they considered and did
not do. For a system whose entire purpose is surfacing design rationale, that is
the highest-density source available, and it is pre-structured.

Both source repos are plain markdown in git -- no API, no token, no rate limit.

Note on placement: chunk_kep() lives here rather than in ingestion/chunker.py only
because chunker.py was being edited concurrently when this was written. It belongs
next to chunk_issue/chunk_pr/chunk_commit and should be moved there.

Usage
-----
    python ingestion/kep_scraper.py          # clone/refresh, then write JSON
"""
import os, re, json, subprocess, sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

load_dotenv()

RAW_DIR   = Path('./data/raw/kubernetes_kubernetes')
ENH_DIR   = Path(os.getenv('ENHANCEMENTS_DIR', './data/enhancements'))
COMM_DIR  = Path(os.getenv('COMMUNITY_DIR', './data/community'))
SIG       = os.getenv('KEP_SIG', 'sig-node')

ENH_REPO  = 'https://github.com/kubernetes/enhancements.git'
COMM_REPO = 'https://github.com/kubernetes/community.git'

# Templated checklists, not rationale. Indexing them buries the real content
# under identical boilerplate repeated across 120 documents.
SKIP_SECTIONS = {
    'table of contents',
    'release signoff checklist',
    'production readiness review questionnaire',
}


def ensure_clone(path: Path, url: str):
    if (path / '.git').exists():
        subprocess.run(['git', '-C', str(path), 'pull', '--quiet', '--ff-only'],
                       check=False, capture_output=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    print(f'Cloning {url} -> {path}')
    subprocess.run(['git', 'clone', '--depth', '1', '--quiet', url, str(path)], check=True)


# --------------------------------------------------------------------------

_H2 = re.compile(r'^##\s+(.+?)\s*$', re.MULTILINE)


def split_sections(markdown: str) -> list[dict]:
    """Split a KEP README into its `## ` sections, preserving order."""
    matches = list(_H2.finditer(markdown))
    out = []
    for i, m in enumerate(matches):
        heading = m.group(1).strip()
        start   = m.end()
        stop    = matches[i + 1].start() if i + 1 < len(matches) else len(markdown)
        body    = markdown[start:stop].strip()
        if not body or heading.lower() in SKIP_SECTIONS:
            continue
        out.append({'heading': heading, 'body': body})
    return out


def read_kep(kep_dir: Path) -> dict | None:
    readme = kep_dir / 'README.md'
    if not readme.exists():
        return None

    meta = {}
    yml  = kep_dir / 'kep.yaml'
    if yml.exists():
        try:
            meta = yaml.safe_load(yml.read_text()) or {}
        except yaml.YAMLError:
            meta = {}

    # Directory names are `<number>-<slug>`; kep.yaml is authoritative when present.
    slug   = kep_dir.name
    number = meta.get('kep-number')
    if number is None:
        m = re.match(r'^(\d+)-', slug)
        number = int(m.group(1)) if m else None

    sections = split_sections(readme.read_text(errors='replace'))
    if not sections:
        return None

    return {
        'kep_number':    number,
        'slug':          slug,
        'title':         meta.get('title') or slug.split('-', 1)[-1].replace('-', ' '),
        'status':        meta.get('status', 'unknown'),
        'authors':       meta.get('authors', []) or [],
        'creation_date': str(meta.get('creation-date', '')),
        'last_updated':  str(meta.get('last-updated', '')),
        'sections':      sections,
        'url': f'https://github.com/kubernetes/enhancements/tree/master/keps/{SIG}/{slug}',
    }


def read_community_doc(path: Path) -> dict | None:
    text = path.read_text(errors='replace')
    sections = split_sections(text)
    if not sections:
        return None
    rel = path.relative_to(COMM_DIR)
    return {
        'kep_number':    None,
        'slug':          str(rel),
        'title':         path.stem.replace('-', ' '),
        'status':        'community-doc',
        'authors':       [],
        'creation_date': '',
        'last_updated':  '',
        'sections':      sections,
        'url': f'https://github.com/kubernetes/community/blob/master/{rel}',
    }


# --------------------------------------------------------------------------

def chunk_kep(kep: dict, repo: str) -> list[dict]:
    """One chunk per section. Sections are already the natural semantic unit --
    'Alternatives' is a self-contained argument -- so there is no need to split
    mid-thought the way issue threads require."""
    from .token_utils import count_tokens, truncate_to_tokens, EMBED_MAX_TOKENS

    is_kep = kep.get('kep_number') is not None
    label  = f"KEP-{kep['kep_number']}" if is_kep else kep['slug']

    chunks = []
    for idx, sec in enumerate(kep['sections']):
        prefix = (
            f"[{label} - {kep['status']}]\n"
            f"Title: {kep['title']}\n"
            f"Section: {sec['heading']}\n"
            f"---\n"
        )
        # Same limit as chunker.py -- sized to the embedding model, not to taste.
        budget = EMBED_MAX_TOKENS - count_tokens(prefix) - 5
        body   = sec['body']
        if count_tokens(body) > budget:
            body = truncate_to_tokens(body, budget)

        chunks.append({
            'text':        prefix + body,
            'source_type': 'kep' if is_kep else 'community',
            'repo':        repo,
            'number':      kep['kep_number'],
            'title':       f"{label}: {kep['title']} — {sec['heading']}",
            'url':         kep['url'],
            'labels':      [SIG, sec['heading'].lower()],
            'state':       kep['status'],
            'chunk_index': idx,
            'file_path':   None,
        })
    return chunks


# --------------------------------------------------------------------------

def main():
    ensure_clone(ENH_DIR, ENH_REPO)
    ensure_clone(COMM_DIR, COMM_REPO)

    kep_root = ENH_DIR / 'keps' / SIG
    if not kep_root.exists():
        sys.exit(f'No KEP directory at {kep_root}')

    out_dir = RAW_DIR / 'keps'
    out_dir.mkdir(parents=True, exist_ok=True)

    written, sections, with_alts = 0, 0, 0
    for kep_dir in sorted(p for p in kep_root.iterdir() if p.is_dir()):
        kep = read_kep(kep_dir)
        if not kep:
            continue
        (out_dir / f"{kep['slug']}.json").write_text(json.dumps(kep, indent=2))
        written  += 1
        sections += len(kep['sections'])
        if any(s['heading'].lower().startswith('alternativ') for s in kep['sections']):
            with_alts += 1

    print(f'KEPs:      {written} written, {sections} sections, '
          f'{with_alts} with an Alternatives section')

    comm_root = COMM_DIR / SIG
    comm_out  = RAW_DIR / 'community'
    comm_out.mkdir(parents=True, exist_ok=True)
    cwritten, csections = 0, 0
    if comm_root.exists():
        for md in sorted(comm_root.rglob('*.md')):
            doc = read_community_doc(md)
            if not doc:
                continue
            safe = str(doc['slug']).replace('/', '__').replace('.md', '')
            (comm_out / f'{safe}.json').write_text(json.dumps(doc, indent=2))
            cwritten  += 1
            csections += len(doc['sections'])

    print(f'Community: {cwritten} docs written, {csections} sections')
    print(f'\nNote: sig-node meeting notes are kept in Google Docs, not in the '
          f'community repo, so they are not indexable from git.')


if __name__ == '__main__':
    main()
