# agent.md — RepoMind

Operating brief for any AI agent working in this repository. Read this before touching code.

---

## 1. What this project is

**RepoMind** is a Retrieval-Augmented Generation (RAG) chatbot that answers questions about *design
decisions, architectural rationale, and undocumented reasoning* inside a GitHub repository — grounded
in that repo's issues, pull requests, and commit messages rather than in the model's own knowledge.

**Current scope is hard-coded to one subsystem:** `kubernetes/kubernetes`, the **kubelet** area
(issues labelled `area/kubelet` / `component/kubelet`, PRs touching `pkg/kubelet/`, and commits on
`pkg/kubelet`). The repo is a single-repo demo, not a multi-tenant product, even though a few code
paths (`/api/index`, `/api/repos`, `repo` request fields) are parameterised by repo name.

The defining product goal is **anti-hallucination**: the system would rather say "I don't know" than
produce an unsourced claim. That intent shows up in three places — a very strict system prompt, an
LLM-based citation verifier, and a confidence threshold with a fallback message.

Reported index size (from README): ~871 kubelet issues, ~943 kubelet PRs, ~13,073 commit messages.

---

## 2. Tech stack

| Layer | Choice |
|---|---|
| Answering + citation verification | Groq — `openai/gpt-oss-120b` (env: `GROQ_MODEL`) |
| Query rewriting | Groq — `qwen/qwen3.8-27b` (env: `GROQ_MODEL_FAST`) |
| Embeddings | `sentence-transformers` `all-MiniLM-L6-v2` (384-dim, cosine) |
| Reranker | `cross-encoder/ms-marco-MiniLM-L-6-v2` (CrossEncoder) |
| Vector store | Qdrant (Docker, `localhost:6333`) |
| Keyword search | `rank-bm25` (`BM25Okapi`) over text stored in SQLite |
| Relational store | SQLite (`./db/repomind.db`) |
| API | FastAPI + Uvicorn (`localhost:8000`) |
| Background jobs | Celery + Redis (`localhost:6379`) |
| Frontend | React 19 + Vite (`localhost:5173`), plain CSS glassmorphism |

Python **3.11+ is required** (the code uses `list[dict]` and `X | None` annotations evaluated at
runtime, e.g. `retrieval/bm25_search.py:7`). The system Python on this machine is 3.9 — always use
the `venv/` that `setup.sh` creates.

---

## 3. Repository map

```
REPOMIND/
├── agent.md                  # this file
├── README.md                 # user-facing setup + feature list
├── setup.sh                  # venv → deps → db init → scrape → index → link
├── requirements.txt          # unpinned deps
├── docker-compose.yml        # qdrant + redis only
├── .env.example              # env template (see §5)
├── index_small.py            # MAIN indexing entrypoint: raw JSON → chunks → Qdrant + SQLite
├── DECISIONS.md              # why the system is built this way (incl. what was wrong)
├── Dockerfile / fly.toml     # deployment: CPU-only torch, models baked in, 1 warm machine
├── eval/                     # golden set builder + ablation / refusal / threshold harness
│
├── ingestion/
│   ├── scraper_v2.py         # clone-driven: commits → PRs → issues → deep re-fetch
│   ├── kep_scraper.py        # sig-node KEPs + community docs, pure git
│   ├── chunker.py            # chunk_issue / chunk_pr / chunk_commit
│   ├── token_utils.py        # MiniLM tokenizer: count_tokens, truncate_to_tokens
│   ├── embedder.py           # embed_texts + upsert_chunks (Qdrant points + SQLite rows)
│   ├── scraper_v2.py         # clone-driven scrape: commits → PRs → issues (preferred)
│   └── kep_scraper.py        # sig-node KEPs + community docs (git only, no API)
│
├── retrieval/
│   ├── vector_search.py      # Qdrant query_points, score_threshold 0.25, k=20
│   ├── bm25_search.py        # lazily-built in-memory BM25 over bm25_index table
│   ├── reranker.py           # CrossEncoder rerank, drops score <= 0
│   └── pipeline.py           # rewrite → vector+bm25 → RRF → rerank
│
├── generation/
│   ├── prompts.py            # SYSTEM_PROMPT (7 strict rules) + FALLBACK_MESSAGE
│   └── generator.py          # format_context, dedupe_sources, verify_citations, generate
│
├── api/
│   ├── main.py               # FastAPI app, env-driven CORS, /health
│   ├── deps.py               # current_user / optional_user bearer-token dependencies
│   ├── ratelimit.py          # per-IP cap on /api/chat
│   ├── models.py             # Message, ChatRequest, IndexRequest
│   └── routes/
│       ├── chat.py           # POST /api/chat — SSE streaming answer + verification
│       ├── auth.py           # register/login/logout/me + chat-history CRUD
│       ├── admin.py          # out-of-scope query analytics + track_out_of_scope_query()
│       ├── index.py          # POST /api/index (Celery), GET /api/repos
│       └── webhook.py        # POST /api/webhook/github — re-index a changed issue
│
├── workers/tasks.py          # Celery: full_index_repo, update_issue_task
├── db/
│   ├── schema.sql            # 6 tables (see §6)
│   └── setup.py              # init_sqlite() + create_collection()
└── repomind-ui/
    └── src/
        ├── App.jsx           # chat UI, auth modal, SSE reader, citation highlighting
        ├── AdminDashboard.jsx# admin login + out-of-scope query table
        ├── App.css           # glassmorphism design system (purple gradient, blur)
        └── main.jsx / index.css
```

---

## 4. End-to-end pipeline

### 4.1 Ingestion (offline)

1. `ingestion/scraper_v2.py` — the scraper. Enumeration happens in git, not the API:
   - **Commits**: blobless clone + `git log --first-parent -- pkg/kubelet` → 6,272 mainline commits.
     Complete, ~48 s, zero API calls. (13,562 commits touch the path; 7,290 are intra-PR churn.)
   - **PRs**: numbers parsed from merge-commit messages (6,268 of them; only 4 needed
     `associatedPullRequests`), then fetched by number batched 12 per aliased GraphQL query.
   - **Issues**: date-sliced `search(type: ISSUE)` across *all* configured labels, auto-subdividing
     on GitHub's hard 1,000-result cap, unioned with `fixes #N` back-references from PR bodies.
     Label search found 889; back-references found ~1,098 more.
   - **Deep pass**: every connection requests free `totalCount`, so the run ends with a truncation
     report; `stage_deep` re-fetches only the clipped PRs at much higher limits. Currently 0 clipped.
2. `ingestion/kep_scraper.py` — clones `kubernetes/enhancements` and `kubernetes/community`; one JSON
   per sig-node KEP (126, 930 `##` sections, 106 with Alternatives) plus 25 community docs. Pure git.
   Templated sections (ToC, Release Signoff, PRR Questionnaire) are skipped as boilerplate.
3. `index_small.py` — the real indexing entrypoint. Calls `init_sqlite()` + `create_collection()`,
   chunks every issue/PR/commit JSON, and `upsert_chunks()` into Qdrant + SQLite.
4. `ingestion/kep_scraper.py` — clones `kubernetes/enhancements` and `kubernetes/community` and
   emits one JSON per sig-node KEP (126 of them, 930 `##` sections) plus 25 community docs. Pure git,
   no API or token. Templated sections (Table of Contents, Release Signoff Checklist, Production
   Readiness Review Questionnaire) are skipped as boilerplate.

**Chunking** (`ingestion/chunker.py`) is token-aware at 500 tokens with 50-token overlap, measured
with the MiniLM tokenizer. Every chunk is prefixed with a metadata header (`[ISSUE #N - STATE]`,
`[PR #N - STATE]`, `[COMMIT abcdef12]`) so the header travels into the embedding and into the LLM
context. Conversation units (body, each review, each inline comment, each comment) are accumulated
until the budget is hit, then flushed with an overlap tail carried into the next chunk. Commits are
always a single chunk.

**Chunk payload schema** (identical in Qdrant payload and used throughout retrieval):
`text, source_type ('issue'|'pr'|'commit'|'code'), repo, number, title, url, labels, state,
chunk_index, file_path`. Point IDs are random UUID4s, mirrored into `chunks.id` and
`bm25_index.chunk_id`.

### 4.2 Retrieval (`retrieval/pipeline.py::retrieve`)

```
question
  → rewrite_query()      LLM rewrite using last 4 history turns
  → vector_search(k=20)  Qdrant cosine, score_threshold=0.25
  → bm25_search(k=20)    BM25Okapi over lowercased whitespace tokens
  → _rrf(k=60)           Reciprocal Rank Fusion of the two ranked id lists
  → rerank(top_n=7)      CrossEncoder, drops any chunk with score <= 0
  → (chunks, best_score, rewritten)
```

`best_score` is the **raw cosine score of the top vector hit**, not a fused or reranked score. It is
the only thing compared against `CONFIDENCE_THRESHOLD`.

### 4.3 Generation

`generation/prompts.py::SYSTEM_PROMPT` embeds the repo name and the numbered context, then imposes
seven strict rules: every sentence traceable to a chunk; inline `(#N)` citation per claim; no
citing loosely-related sources; state what the context does *not* cover; no inference; emit
`INSUFFICIENT_CONTEXT: <what's missing>` when it cannot answer; never use training knowledge.

The **three-layer anti-hallucination system**:
1. **Threshold gate** — `best_score < CONFIDENCE_THRESHOLD` (0.40) or zero chunks ⇒ return
   `FALLBACK_MESSAGE`, no LLM call, and the question is recorded as out-of-scope.
2. **Citation verification** — `verify_citations()` regexes `#\d+` out of the answer, gathers the
   first 800 chars of each cited chunk, and asks the LLM to return JSON
   `{valid, invalid_citations[], verified_answer}`. The verified answer replaces the raw one.
3. **UI warnings** — the frontend renders a fallback banner when `is_fallback`, and a
   "some citations were removed" warning when `citations_valid === false`.

`dedupe_sources()` collapses chunks to at most 6 unique `(source_type, number, file_path)` source
links for the UI.

### 4.4 Serving

`POST /api/chat` (`api/routes/chat.py`) deliberately **re-implements** the generation path inline so
it can stream: it runs `retrieve` in a thread executor, applies the threshold gate, builds the same
system prompt, streams Groq tokens as SSE `data: {"token": "..."}` frames, then runs
`verify_citations` and emits one final `data: {"done": true, sources, best_score, citations_valid,
invalid_citations}` frame. If the user is logged in, both turns are persisted afterwards.

The frontend reads the stream with `res.body.getReader()`, splits on `\n\n`, appends tokens to the
last assistant message, and replaces that message wholesale on the `done` frame. It also handles the
non-SSE JSON response used for the fallback case.

---

## 5. Environment & configuration

`.env` at repo root (copy from `.env.example`):

```
GROQ_API_KEY           # required — console.groq.com
GITHUB_TOKEN           # required for scraping — read-only repo contents
QDRANT_HOST=localhost
QDRANT_PORT=6333
REDIS_URL=redis://localhost:6379/0
DATABASE_PATH=./db/repomind.db
CONFIDENCE_THRESHOLD=0.40
MAX_CHUNK_TOKENS=500           # declared but NOT read by code; chunker hard-codes MAX_TOKENS=500
MAX_ISSUES=500
MAX_PRS=50                     # 50 ≈ 15–30 min; 200 ≈ 1–3 h
KUBELET_ISSUE_LABELS=area/kubelet,component/kubelet
KUBELET_FILE_PREFIX=pkg/kubelet
```

`ADMIN_USERNAME` / `ADMIN_PASSWORD` are read by `api/routes/admin.py` (defaults `admin`/`admin`) and
documented in the README but are **missing from `.env.example`** — add them when touching that file.

Frontend: `VITE_API_URL` (defaults to `http://localhost:8000`). There is no `repomind-ui/.env`.

---

## 6. Data stores

**Qdrant** — one collection per repo, named `owner_repo` (`kubernetes_kubernetes`). 384-dim cosine
vectors. Payload indexes on `source_type`, `state`, `number` (int), `labels`.

**SQLite** (`db/schema.sql`, 6 tables):
- `chunks` — chunk metadata mirror (`id, repo, source_type, number, file_path, chunk_index, url, title`).
- `bm25_index` — `chunk_id → text`; the entire BM25 corpus is loaded into memory on first search.
- `users` — `username` unique, `password_hash`, `is_admin`.
- `chat_history` — `user_id, role, content, created_at`, indexed on `user_id`.
- `sessions` — `token` unique, `user_id`, `expires_at` (7 days).
- `out_of_scope_queries` — `query, count, first_seen, last_seen` (upsert-by-text counter).

`data/raw/` (git-ignored) holds the scraped JSON. `db/` is git-ignored *except* the two tracked
files `db/schema.sql` and `db/setup.py` — **new files added under `db/` need `git add -f`**.

---

## 7. API surface

| Method | Path | Notes |
|---|---|---|
| GET | `/health` | `{"status":"ok"}` |
| POST | `/api/chat` | body `{question, repo, history[], user_id?}`; SSE stream, or plain JSON on fallback |
| POST | `/api/register` | body `{username, password}` → `{token, user_id, username, is_admin}` |
| POST | `/api/login` | same shape; 401 on bad credentials |
| POST | `/api/logout` | clears a `token` cookie the app never sets |
| GET | `/api/me?token=` | `{authenticated, user_id, username, is_admin}` |
| GET | `/api/history?user_id=` | ordered `[{role, content}]` |
| POST | `/api/history?user_id=&role=&content=` | **query params, not a JSON body** |
| DELETE | `/api/history?user_id=` | wipes that user's history |
| GET | `/api/admin/queries?admin_user=&admin_pass=` | out-of-scope queries by count desc |
| DELETE | `/api/admin/queries?admin_user=&admin_pass=` | clears the table |
| POST | `/api/index` | body `{repo}` → enqueues `full_index_repo` on Celery |
| GET | `/api/repos` | collection names from Qdrant, `_` → `/` |
| POST | `/api/webhook/github` | on `issues`/`issue_comment` enqueues `update_issue_task` |

CORS allows only `http://localhost:5173`.

---

## 8. Running it

```bash
docker-compose up -d                     # Qdrant + Redis
cp .env.example .env                     # then fill GROQ_API_KEY + GITHUB_TOKEN
./setup.sh                               # venv, deps, db init, scrape, index, link
uvicorn api.main:app --reload --port 8000
cd repomind-ui && npm install && npm run dev
```

`setup.sh` runs `scraper_v2.py all` then `kep_scraper.py` then `index_small.py`. Every stage is
resumable — it skips files already on disk — so an interrupted scrape is safe to re-run.

Celery is also not started by `setup.sh`; `/api/index` and the webhook silently queue work that
nobody consumes unless you run `celery -A workers.tasks worker`.

Smoke tests:

```bash
curl http://localhost:8000/health
python -c "from retrieval.pipeline import retrieve; c,s,r = retrieve('How does kubelet handle pod eviction?','kubernetes_kubernetes'); print(s, len(c))"
```

**Always run Python from the repo root** — `db/setup.py` opens `./db/schema.sql` and every module
resolves `./data/raw` and `./db/repomind.db` relative to CWD.

---

## 9. Known quirks, gaps and traps

These are real observations from reading the code, not speculation. Respect them or fix them
deliberately; do not "clean them up" by accident.

**Dead / unreachable paths**
- ~~`chunk_code_file()` / `expand_with_links()` / `code_issue_links` were dead code.~~ **Removed in
  Phase 0 Task 0.2**, along with the orphaned module-level `QdrantClient` in `pipeline.py` that only
  `_fetch_by_number()` had used.
- `generation/generator.py::generate()` is not used by the API — `api/routes/chat.py` duplicates its
  logic inline to support streaming. **Any prompt or verification change must be made in both
  places**, or made in one and re-wired.
- `MAX_CHUNK_TOKENS` in `.env` is ignored; `chunker.MAX_TOKENS = 500` is a module constant.

**Retrieval behaviour**
- In `retrieve()`, candidates are filtered by `if i in vec_map` — so BM25 can only *reorder* vector
  hits, never introduce a document the vector search missed. RRF is therefore weaker than it looks.
- `vector_search` has a hard `score_threshold=0.25` and swallows all exceptions, returning `[]`.
  A Qdrant outage looks identical to "no results" and produces the fallback message.
- `rewrite_query()` costs an LLM round-trip on *every* question, so retrieval requires a valid
  `GROQ_API_KEY` even for pure retrieval tests.
- `bm25_search` loads the entire `bm25_index` table into memory once per process and never refreshes,
  so chunks indexed after API start are invisible to BM25 until restart.

**Concurrency**
- `ingestion/embedder.py` opens a module-level `sqlite3.connect()` on the importing thread. `chat.py`
  runs `retrieve` in a thread-pool executor, so cross-thread use raises "SQLite objects created in a
  thread can only be used in that same thread". `pipeline.py`'s connection was removed with
  `expand_with_links` in Task 0.2; `embedder.py` still needs `check_same_thread=False` (Task 0.6a).

**Security** (this is a demo; treat the auth as illustrative, not production-grade)
- Passwords are **unsalted SHA-256** (`auth.py::hash_password`) — no bcrypt/argon2, no work factor.
- `/api/history` (GET/POST/DELETE) and `/api/chat` accept a raw `user_id` with **no token check**,
  so any caller can read or delete any user's chat history.
- Session tokens live in `localStorage` and are passed in the query string (`/api/me?token=`).
- Admin credentials are passed as **query parameters** and cached in `localStorage` in plaintext by
  `AdminDashboard.jsx`. Admin auth is env-var based and completely separate from the `users.is_admin`
  column, which nothing ever sets to 1.
- ~~`commit_scraper.py` needed `GITHUB_TOKEN` exported manually.~~ Both original scrapers were
  **deleted** in Phase 0; `scraper_v2.py` supersedes them and reads `.env` normally.

**Robustness**
- `verify_citations` catches `(json.JSONDecodeError, Exception)` and returns the *unverified* answer
  marked `valid: True` — verification failures fail open and are invisible.
- `chat.py` uses Pydantic v1's deprecated `m.dict()`; `requirements.txt` is fully unpinned.
- `chat.py` prefers DB history over request history and passes the whole thing to `retrieve` while
  only the last 6 turns reach the LLM — long histories grow the rewrite prompt unboundedly.

**Testing / tooling**
- There are **no tests**, no CI, no Python linter or formatter config. The frontend has ESLint
  (`npm run lint`) and that is the only automated check in the repo.
- Frontend `index.html` still has the Vite default `<title>ui</title>`.

---

## 10. Conventions to follow

- **Python style in this repo**: aligned-assignment blocks (`client    = ...`), single quotes,
  compact multi-import lines (`import os, json, time, httpx`), `load_dotenv()` at module top,
  `os.getenv('X', default)` inline. Match it rather than reformatting.
- **Never commit** `.env`, `data/`, `qdrant_storage/`, `*.db`, or `*.log` — all git-ignored.
- New DB tables go in `db/schema.sql` with `CREATE TABLE IF NOT EXISTS`; `init_sqlite()` replays the
  whole script, so it must stay idempotent.
- New chunk types: add a `chunk_*` function in `chunker.py` emitting the full payload schema from
  §4.1, then register it in `index_small.py` **and** `workers/tasks.py::full_index_repo`.
- Frontend styling is hand-written CSS in `App.css` using the `--glass-*` / `--accent` custom
  properties and BEM-ish class names (`message__bubble`, `btn--primary`). No CSS framework — do not
  introduce one casually.
- Re-indexing is safe: scrapers skip existing JSON files and `upsert_chunks` uses
  `INSERT OR REPLACE` — but Qdrant point IDs are fresh UUIDs each run, so **re-running
  `index_small.py` duplicates vectors in Qdrant**. Recreate the collection for a clean rebuild.
- Scraping is slow and rate-limited. Prefer working against already-scraped `data/raw/` JSON; only
  re-scrape when you genuinely need newer data.

---

## 11. Where to make common changes

| Goal | File(s) |
|---|---|
| Change answer tone / strictness | `generation/prompts.py::SYSTEM_PROMPT` |
| Change the fallback wording | `generation/prompts.py::FALLBACK_MESSAGE` |
| Tune when the bot refuses | `CONFIDENCE_THRESHOLD` in `.env`; gate in `api/routes/chat.py` |
| Tune retrieval depth/quality | `retrieval/pipeline.py` (`k`, `top_n`, RRF `k=60`), `vector_search` `score_threshold`, `reranker.MIN_RERANK_SCORE` |
| Swap the LLM | `GROQ_MODEL` / `GROQ_MODEL_FAST` in `.env` — no hardcoded model names remain |
| Swap the embedding model | `ingestion/embedder.py` + `ingestion/token_utils.py` + `VectorParams(size=384)` in `db/setup.py` — all three must agree, and the index must be rebuilt |
| Widen the scope beyond kubelet | `KUBELET_ISSUE_LABELS` / `KUBELET_FILE_PREFIX` in `.env`, the hard-coded `'kubernetes','kubernetes'` in the scrapers, `REPO` in `index_small.py`, `REPO` in `repomind-ui/src/App.jsx` |
| Add an endpoint | new module in `api/routes/`, then `include_router(..., prefix='/api')` in `api/main.py` |
| Change chat UI behaviour | `repomind-ui/src/App.jsx` (SSE reader in `send()`, citation regex in `renderContent()`) |
