# Deploying RepoMind

Two paths. **The free one is recommended** — it is also simpler to operate, because it
removes the only component that needs hosting.

| | Free | Paid |
|---|---|---|
| Vector DB | Embedded Qdrant, in-process | Qdrant Cloud |
| Host | Hugging Face Spaces, free CPU tier | Fly.io 1 GB |
| LLM | Groq free tier | Groq free tier |
| **Cost** | **$0** | **~$5–7/month** |

---

# The free path

## Why it works

`QdrantClient(path=...)` runs the Qdrant engine **in-process against a local directory** — no
server, no network, nothing to host. A single container serves the whole system.

Qdrant warns that local mode is not recommended above 20,000 points and this corpus has
113,047. **Measured: 34 ms per query.** The cross-encoder rerank and the LLM call take
*seconds*, so vector search is nowhere near the bottleneck. The warning is about large-scale
throughput, not this.

| | measured |
|---|---|
| Embedded store size | **566 MB** (113,047 points) |
| Export time | 54 s |
| Query latency | **34 ms** |
| Models baked into image | ~180 MB |
| SQLite (BM25 corpus, users, history) | 144 MB |
| **Total to ship** | **~890 MB** |

## 1. Export the collection

The server's storage format is not the embedded format, so points are copied rather than moved:

```bash
python db/export_embedded.py            # -> ./data/qdrant_embedded
```

It recreates the payload indexes (a point copy does not carry them — without them, filtered
queries degrade to a full scan) and verifies the point count matches before reporting success.

## 2. Configure

```bash
QDRANT_PATH=./data/qdrant_embedded      # embedded mode; leave QDRANT_HOST/PORT unset
DATABASE_PATH=./db/repomind.db
GROQ_API_KEY=...
GROQ_MODEL=openai/gpt-oss-120b
GROQ_MODEL_FAST=qwen/qwen3.8-27b
GROQ_MODEL_VERIFY=openai/gpt-oss-20b
CONFIDENCE_THRESHOLD=0.60
RETRIEVAL_TOP_N=18
ALLOWED_ORIGINS=https://<your-space>.hf.space
RATE_MAX_CALLS=5                        # see the quota section -- 30 is too generous
```

`db/qdrant_client_factory.py` picks the mode from the environment: `QDRANT_PATH` → embedded,
`QDRANT_URL` → cloud, otherwise host/port. No code change between local dev and deployment.

## 3. Ship it

Hugging Face Spaces (Docker SDK) is the natural home — free CPU tier, no card required, and for
an ML project it signals the right thing. Push the repo plus `data/qdrant_embedded/` and
`db/repomind.db`; both exceed GitHub's file limits, so use **Git LFS** or a companion HF Dataset
downloaded at startup.

Check the current free-tier RAM before committing: torch plus MiniLM plus the cross-encoder plus
BM25's resident corpus wants roughly 1.5 GB.

## ⚠️ The real constraint is the LLM quota, not hosting

```
Groq free tier      200,000 tokens/day
Per question        ~5,400 tokens (4,600 context + ~800 output)
                 => ~37 questions/day, across all visitors
```

**Set `RATE_MAX_CALLS=5` per hour per IP.** At the development default of 30, a single visitor
can consume 80% of a day's quota in an hour.

Two things that make a small quota survivable, both already implemented:

- `/api/chat` catches upstream `429`s and returns *"The language model is rate limited right now"*
  rather than an empty stream. Without it a rate-limited demo looks broken.
- Seed the empty state with three clickable example questions, so the quota is spent on good
  questions rather than on visitors working out what the thing is for.

If the demo gets real traffic, **Groq's paid tier is the first thing to buy** — not hosting.

## Before sharing the link

- [ ] Rotate `GROQ_API_KEY` if it has ever been pasted anywhere
- [ ] `RATE_MAX_CALLS` lowered
- [ ] `ADMIN_PASSWORD` is not `change-me`
- [ ] `ALLOWED_ORIGINS` matches the deployed URL, or every request fails CORS
- [ ] Models baked into the image — otherwise first request downloads them, and cold start is
      already 17 s because `pipeline`, `reranker` and `embedder` all load at import
- [ ] Ask one in-scope and one out-of-scope question against the live URL

---

# The paid path


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

The collection is 113,047 points × 384 dimensions ≈ 174 MB of vectors, plus payloads (the chunk text
is the bulk — 566 MB total on disk). Check usage against the free tier after upload.

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

Setting `QDRANT_URL` is all that is required — `db/qdrant_client_factory.py` selects cloud mode from
the environment, so no code change is needed. (Three modules previously built their own client with
host/port hardcoded, which is why supporting a second deployment shape once meant editing all three
and keeping them in agreement.)

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

`bm25_search._load()` reads the entire `bm25_index` table into memory — all 113,047 chunks with full
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
