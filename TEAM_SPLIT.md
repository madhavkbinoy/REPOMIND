# RepoMind - Team Contribution Split

This document outlines a logical 3-way split of the RepoMind project for a team of 3 contributors. Each part is independent and can be developed in parallel.

---

## Part 1: Data Ingestion Pipeline

### Description
This part handles scraping GitHub data (issues, PRs, commits), chunking text into manageable pieces, generating embeddings, and creating links between code files and issues/PRs. This is the foundation of the RAG system - without ingested data, there's nothing to retrieve.

### What It Does
1. **Scrapes GitHub data** from kubernetes/kubernetes repository (kubelet subsystem)
2. **Token-aware chunking** of issues, PRs, and commits
3. **Embedding generation** using sentence-transformers
4. **Vector storage** in Qdrant
5. **BM25 indexing** in SQLite for full-text search
6. **Link creation** between file paths and related issues/PRs

### Code Files

| File | Purpose |
|------|---------|
| `ingestion/github_scraper.py` | Scrape issues and PRs from GitHub API |
| `ingestion/commit_scraper.py` | Scrape kubelet-related commits via GraphQL |
| `ingestion/chunker.py` | Token-aware text chunking (500 tokens) |
| `ingestion/embedder.py` | Generate embeddings (all-MiniLM-L6-v2) |
| `ingestion/linker.py` | Map file paths to related PRs/issues |
| `ingestion/token_utils.py` | Token counting utilities |
| `ingestion/__init__.py` | Package initialization |
| `index_small.py` | Main indexing script (combines chunking + embedding) |

### Key Dependencies
- `sentence-transformers` - For embeddings
- `qdrant-client` - Vector database
- `httpx` - HTTP client for GitHub API

### To Test
```bash
python ingestion/github_scraper.py
python index_small.py
python ingestion/linker.py
```

---

## Part 2: Retrieval & Generation Engine

### Description
This is the core RAG engine. It handles query processing, searches both vector and BM25 indexes, reranks results, and generates answers using the LLM with citation verification. This part answers user questions.

### What It Does
1. **Query rewriting** - Expand abbreviations (e.g., "kubelet" → detailed term)
2. **Hybrid search** - Combine vector (Qdrant) + BM25 (SQLite) via RRF fusion
3. **Reranking** - Use CrossEncoder for precise relevance
4. **LLM generation** - Generate answers using Groq LLaMA 3.3
5. **Citation verification** - 3-layer anti-hallucination (extract → verify → remove invalid)
6. **Fallback handling** - When confidence is too low

### Code Files

| File | Purpose |
|------|---------|
| `retrieval/pipeline.py` | Main retrieval orchestration (RRF, reranking, link expansion) |
| `retrieval/vector_search.py` | Qdrant vector similarity search |
| `retrieval/bm25_search.py` | SQLite BM25 full-text search |
| `retrieval/reranker.py` | CrossEncoder reranking |
| `retrieval/__init__.py` | Package initialization |
| `generation/generator.py` | LLM generation with citation verification |
| `generation/prompts.py` | System prompts for LLM |
| `generation/__init__.py` | Package initialization |

### Key Dependencies
- `groq` - LLaMA 3.3 API
- `sentence-transformers` - CrossEncoder
- `qdrant-client` - Vector search

### To Test
```bash
python -c "from retrieval.pipeline import retrieve; c,s,r = retrieve('How does kubelet handle pod eviction?', 'kubernetes_kubernetes'); print(f'Score: {s}, Chunks: {len(c)}')"
```

---

## Part 3: API, Authentication & Frontend

### Description
This part provides the user-facing interface. It includes the FastAPI backend with authentication, admin dashboard, chat persistence, and a React frontend with glassmorphism UI. This connects users to the RAG engine.

### What It Does
1. **REST API** - FastAPI endpoints for all operations
2. **User authentication** - Register, login, session management (SHA-256 hashing)
3. **Chat persistence** - Save/retrieve chat history per user
4. **Admin dashboard** - Track out-of-scope queries
5. **Streaming chat** - Real-time LLM response streaming
6. **Glassmorphism UI** - Modern React frontend with blur effects

### Code Files

#### Backend API
| File | Purpose |
|------|---------|
| `api/main.py` | FastAPI app, CORS, router setup |
| `api/models.py` | Pydantic models |
| `api/routes/chat.py` | `/api/chat` - Streaming chat endpoint |
| `api/routes/auth.py` | `/api/register`, `/api/login`, `/api/history` |
| `api/routes/admin.py` | `/api/admin/queries` - Admin endpoints |
| `api/routes/index.py` | `/api/index` - Trigger reindexing |
| `api/routes/webhook.py` | Webhook for external triggers |
| `api/routes/__init__.py` | Route imports |
| `api/__init__.py` | Package initialization |
| `db/schema.sql` | Database schema (users, sessions, chat_history) |

#### Frontend
| File | Purpose |
|------|---------|
| `repomind-ui/src/App.jsx` | Main React component |
| `repomind-ui/src/App.css` | Glassmorphism styles |
| `repomind-ui/src/AdminDashboard.jsx` | Admin panel component |
| `repomind-ui/src/main.jsx` | React entry point |

### Key Dependencies
- `fastapi` - Web framework
- `uvicorn` - ASGI server
- `sqlite3` - Chat/user database
- `react` + `vite` - Frontend

### To Test
```bash
# Start backend
uvicorn api.main:app --reload --port 8000

# Start frontend
cd repomind-ui && npm run dev

# Test endpoints
curl http://localhost:8000/health
curl -X POST http://localhost:8000/api/register -H "Content-Type: application/json" -d '{"username":"test","password":"test123"}'
```

---

## Contribution Summary

| Part | Name | Files | Focus Area |
|------|------|-------|------------|
| **Part 1** | Data Ingestion | 8 files | Scraping → Chunks → Embeddings |
| **Part 2** | Retrieval & Generation | 7 files | Search → Rerank → LLM → Verify |
| **Part 3** | API & Frontend | 15 files | Auth → Chat → Admin → UI |

---

## Integration Points

When combining parts:

1. **Part 1 → Part 2:** Part 1 populates Qdrant and SQLite. Part 2 queries this data.
2. **Part 2 → Part 3:** Part 3 calls Part 2's retrieval via `/api/chat` endpoint.
3. **All together:** Full flow: UI → API → Retrieval → Generation → Response with citations.

### End-to-End Test (All Parts Combined)
```bash
# Start services
docker compose up -d  # Qdrant, Redis

# Part 1: Ingest data (run once)
python ingestion/github_scraper.py
python index_small.py
python ingestion/linker.py

# Part 2 & 3: Start servers
uvicorn api.main:app --reload --port 8000  # Part 3 (includes Part 2)
cd repomind-ui && npm run dev  # Part 3 (frontend)

# Test via UI or API
curl -X POST http://localhost:8000/api/chat \
  -H "Content-Type: application/json" \
  -d '{"question": "How does kubelet handle pod eviction?", "repo": "kubernetes/kubernetes"}'
```
