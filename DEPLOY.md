# Deploying RepoMind

Target: ~$5–7/month. Backend on Fly.io, vectors on Qdrant Cloud's free tier, frontend on
Cloudflare Pages.

```
 visitor ──▶ Cloudflare Pages (static, free)
                  │ HTTPS
                  ▼
            Fly.io · 1 GB shared-cpu-1x  ──▶ Qdrant Cloud (1 GB free tier)
            FastAPI + MiniLM + reranker  ──▶ Groq API
                  │
                  └──▶ SQLite on a Fly volume
```

Redis and Celery are **not** deployed. Nothing consumes the queue and no worker is started, so
shipping a broker would be deploying a component that does nothing.

---

## 1. Qdrant Cloud

Create a free 1 GB cluster at https://cloud.qdrant.io and copy the cluster URL and API key.

The collection is 67,161 points × 384 dimensions ≈ 103 MB of vectors, plus payloads (the chunk text
is the bulk). It fits the free tier, but check usage after upload.

Upload the local collection rather than re-scraping:

```bash
./venv/bin/python - <<'EOF'
import os
from qdrant_client import QdrantClient

src = QdrantClient(host='localhost', port=6333)
dst = QdrantClient(url=os.environ['QDRANT_URL'], api_key=os.environ['QDRANT_API_KEY'])

name = 'kubernetes_kubernetes'
dst.create_collection(name, vectors_config=src.get_collection(name).config.params.vectors)

offset, n = None, 0
while True:
    points, offset = src.scroll(name, limit=256, offset=offset,
                                with_payload=True, with_vectors=True)
    if not points:
        break
    dst.upsert(name, points=points)
    n += len(points)
    print(f'{n} points', end='\r')
print(f'\nuploaded {n}')
EOF
```

`db/setup.py` also creates payload indexes on `source_type`, `state`, `number` and `labels` — create
those on the remote collection too, or filtering degrades to a full scan.

---

## 2. Backend on Fly.io

```bash
fly launch --no-deploy          # accept the existing fly.toml
fly volumes create repomind_data --size 1 --region iad
```

Set secrets (never bake these into the image):

```bash
fly secrets set \
  GROQ_API_KEY=... \
  QDRANT_URL=https://xyz.qdrant.io \
  QDRANT_API_KEY=... \
  ADMIN_USERNAME=... \
  ADMIN_PASSWORD=... \
  ALLOWED_ORIGINS=https://repomind.pages.dev
```

⚠️ `retrieval/vector_search.py` and `db/setup.py` construct `QdrantClient(host=..., port=...)`.
Qdrant Cloud needs `QdrantClient(url=..., api_key=...)`. Add that branch before deploying:

```python
qdrant = (QdrantClient(url=os.getenv('QDRANT_URL'), api_key=os.getenv('QDRANT_API_KEY'))
          if os.getenv('QDRANT_URL') else
          QdrantClient(host=os.getenv('QDRANT_HOST', 'localhost'),
                       port=int(os.getenv('QDRANT_PORT', 6333))))
```

Then:

```bash
fly deploy
fly logs
curl https://repomind.fly.dev/health
```

The SQLite file on the volume starts empty. Initialise the schema once:

```bash
fly ssh console -C "python db/setup.py"
```

### The BM25 problem

`bm25_search._load()` reads the entire `bm25_index` table into memory — all 67,161 chunks with full
text — on the first query. **That table lives in SQLite, and the deployed volume only has the schema.**

Two options:

1. **Ship the SQLite file.** `fly ssh sftp shell` and upload `db/repomind.db` (it carries users,
   sessions and chat history too). Simplest, but the file is large and BM25 still costs ~2.2 s and
   significant RAM on first query.
2. **Switch to SQLite FTS5** (recommended). Queries from disk, no resident corpus, and it fixes the
   separate bug where chunks indexed after startup stay invisible to BM25 until restart. This changes
   retrieval behaviour, so measure it as an ablation row first — do not swap silently.

Without one of these, keyword search returns nothing and retrieval silently degrades to vector-only.

---

## 3. Frontend on Cloudflare Pages

```bash
cd repomind-ui
npm run build
npx wrangler pages deploy dist --project-name repomind
```

Set `VITE_API_URL=https://repomind.fly.dev` as a build-time variable, and make sure the API's
`ALLOWED_ORIGINS` contains the Pages URL. Both halves must agree or every request fails CORS.

---

## 4. Before sharing the link

- [ ] `min_machines_running = 1` — confirmed in `fly.toml`. Cold start is **17 seconds**; with
      scale-to-zero the first visitor sees a page that looks broken.
- [ ] Rate limiting is on (`RATE_MAX_CALLS`, default 30/hour/IP). Each question costs 2–3 Groq calls
      against a shared key.
- [ ] Seed the empty state with 3 clickable example questions. Visitors will not invent good kubelet
      questions, and a bad first query makes a working system look broken.
- [ ] Rotate `GROQ_API_KEY` and `GITHUB_TOKEN` if they were ever pasted anywhere.
- [ ] `ADMIN_PASSWORD` is not `change-me`.
- [ ] Ask one in-scope and one out-of-scope question against the live URL.

---

## Cost

| | |
|---|---|
| Fly.io 1 GB shared-cpu-1x, always on | ~$5–7/mo |
| Qdrant Cloud 1 GB | free |
| Cloudflare Pages | free |
| Groq | free tier |

Memory is the binding constraint: MiniLM + cross-encoder + torch, plus BM25's resident corpus. 512 MB
OOMs on the first query — hence 1 GB and a single uvicorn worker, since each worker loads its own
copy of both models.
