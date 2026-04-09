# RepoMind Agent

## Project Overview

- **Name:** RepoMind
- **Description:** RAG chatbot for GitHub repository design decisions, focused on the kubernetes/kubernetes kubelet subsystem. Provides intelligent Q&A about design decisions, PRs, issues, and commits from the Kubernetes kubelet area.
- **Tech Stack:** 
  - **LLM:** Groq LLaMA 3.3-70B (versatile)
  - **Embeddings:** sentence-transformers (all-MiniLM-L6-v2)
  - **Reranker:** CrossEncoder
  - **Vector DB:** Qdrant
  - **BM25:** SQLite full-text search
  - **Backend:** FastAPI + Celery
  - **Frontend:** React (Vite) with glassmorphism UI
  - **Database:** SQLite (chat history, users, sessions)

---

## Architecture

### Data Flow

```
┌─────────────────────────────────────────────────────────────────────┐
│                           INGESTION                                  │
├─────────────────────────────────────────────────────────────────────┤
│  github_scraper.py ──► chunker.py ──► embedder.py ──► Qdrant       │
│  commit_scraper.py    (token-aware)   (all-MiniLM-L6-v2)           │
│         │                                                                    │
│         └──────────────► SQLite (BM25 index)                            │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│                          RETRIEVAL                                   │
├─────────────────────────────────────────────────────────────────────┤
│  Query ──► Query Rewrite (LLM) ──► RRF Fusion                      │
│              │                    ├──► Vector Search (Qdrant)       │
│              │                    └──► BM25 Search (SQLite)         │
│              │                                                               │
│              └──────────► Reranker (CrossEncoder) ──► Link Expansion  │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│                         GENERATION                                    │
├─────────────────────────────────────────────────────────────────────┤
│  Context + Question ──► LLM (Groq LLaMA 3.3) ──► Response           │
│                                │                                      │
│                                └─► Citation Verification (3-layer)   │
│                                     1. Extract citations from response│
│                                     2. Verify against source chunks   │
│                                     3. Remove invalid citations      │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Implemented Features

### 1. Authentication System
- **User Registration:** `/api/register` - Create new user accounts
- **User Login:** `/api/login` - Authenticate and get session token
- **Session Management:** Token-based sessions (7-day expiry)
- **Password Hashing:** SHA-256
- **Database Schema:** `users` table with `is_admin` flag

### 2. Chat Persistence
- **Save Messages:** `/api/history` (POST) - Store chat messages per user
- **Retrieve History:** `/api/history` (GET) - Load user's chat history
- **Clear History:** `/api/history` (DELETE) - Delete user's chat history
- **Per-User Storage:** Chat history linked to user_id

### 3. Admin Dashboard
- **Out-of-Scope Query Tracking:** Tracks queries that couldn't be answered
- **Query Statistics:** View frequency of out-of-scope queries
- **Admin Endpoints:** `/api/admin/queries` (GET, DELETE)
- **Admin Credentials:** Configured via `ADMIN_USERNAME` and `ADMIN_PASSWORD` in `.env`

### 4. Retrieval Pipeline
- **Query Rewriting:** Expand abbreviated terms (e.g., "kubelet" → "kubelet pod lifecycle management")
- **RRF Fusion:** Reciprocal Rank Fusion combining vector + BM25 scores
- **Reranking:** CrossEncoder for precise relevance ordering
- **Link Expansion:** Map file paths to related PRs/issues

### 5. Generation with Anti-Hallucination
- **Citation Extraction:** Parse [#N] citations from LLM response
- **3-Layer Verification:**
  1. Extract all citations from response
  2. Verify each citation matches source content
  3. Remove invalid citations from final response
- **Fallback Handling:** When confidence < threshold, show fallback message
- **Source Attribution:** Links to original GitHub issues/PRs

### 6. Frontend (Glassmorphism UI)
- **Framework:** React + Vite
- **Styling:** Glassmorphism with blur effects, purple gradient background
- **Features:**
  - Login/Register modal
  - Real-time streaming responses
  - Auto-scroll to latest message
  - Source links with citation highlighting
  - Admin dashboard modal
  - Clear chat / Logout functionality

---

## Key Files

### API Routes (`api/routes/`)
| File | Description |
|------|-------------|
| `main.py` | FastAPI app, CORS middleware, router inclusion |
| `chat.py` | `/api/chat` - Streaming chat endpoint |
| `auth.py` | `/api/register`, `/api/login`, `/api/logout`, `/api/me`, `/api/history` |
| `admin.py` | `/api/admin/queries` - Admin dashboard endpoints |
| `index.py` | `/api/index` - Trigger reindexing |
| `webhook.py` | Webhook for external triggers |

### Ingestion (`ingestion/`)
| File | Description |
|------|-------------|
| `github_scraper.py` | Scrape GitHub issues and PRs from kubernetes/kubelet |
| `commit_scraper.py` | Scrape kubelet-related commits (GraphQL) |
| `chunker.py` | Token-aware text chunking (500 tokens default) |
| `embedder.py` | Generate embeddings using sentence-transformers |
| `linker.py` | Map file paths to related PRs/issues |
| `token_utils.py` | Token counting utilities |

### Retrieval (`retrieval/`)
| File | Description |
|------|-------------|
| `pipeline.py` | Main retrieval orchestration, RRF fusion, reranking |
| `vector_search.py` | Qdrant vector similarity search |
| `bm25_search.py` | SQLite BM25 full-text search |
| `reranker.py` | CrossEncoder reranking |

### Generation (`generation/`)
| File | Description |
|------|-------------|
| `generator.py` | LLM generation with citation verification |
| `prompts.py` | System prompts for LLM |

### Frontend (`repomind-ui/src/`)
| File | Description |
|------|-------------|
| `App.jsx` | Main React component with auth, chat, admin |
| `App.css` | Glassmorphism UI styles |
| `AdminDashboard.jsx` | Admin panel for out-of-scope queries |

---

## Database Schema

```sql
-- Chunks for RAG
CREATE TABLE chunks (
  id TEXT PRIMARY KEY, repo TEXT NOT NULL, source_type TEXT NOT NULL,
  number INTEGER, file_path TEXT, chunk_index INTEGER DEFAULT 0,
  url TEXT, title TEXT
);

-- BM25 full-text index
CREATE TABLE bm25_index (
  chunk_id TEXT PRIMARY KEY, text TEXT NOT NULL
);

-- File to Issue/PR links
CREATE TABLE code_issue_links (
  id INTEGER PRIMARY KEY AUTOINCREMENT, repo TEXT NOT NULL,
  file_path TEXT NOT NULL, pr_number INTEGER, issue_number INTEGER,
  link_type TEXT
);

-- User authentication
CREATE TABLE users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL,
  is_admin INTEGER DEFAULT 0,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Chat history persistence
CREATE TABLE chat_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  role TEXT NOT NULL,
  content TEXT NOT NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (user_id) REFERENCES users(id)
);

-- User sessions
CREATE TABLE sessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  token TEXT UNIQUE NOT NULL,
  user_id INTEGER NOT NULL,
  expires_at TIMESTAMP NOT NULL,
  FOREIGN KEY (user_id) REFERENCES users(id)
);

-- Out of scope queries tracking
CREATE TABLE out_of_scope_queries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  query TEXT NOT NULL,
  count INTEGER DEFAULT 1,
  first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

---

## Environment Variables

See `.env.example` for required keys:

| Variable | Description |
|----------|-------------|
| `GROQ_API_KEY` | Groq API key for LLaMA 3.3 |
| `GITHUB_TOKEN` | GitHub PAT for API access |
| `ADMIN_USERNAME` | Admin username for dashboard |
| `ADMIN_PASSWORD` | Admin password for dashboard |
| `QDRANT_HOST` | Qdrant host (default: localhost) |
| `QDRANT_PORT` | Qdrant port (default: 6333) |
| `REDIS_URL` | Redis URL for Celery (default: redis://localhost:6379/0) |
| `DATABASE_PATH` | SQLite database path (default: ./db/repomind.db) |
| `CONFIDENCE_THRESHOLD` | Min confidence for generation (default: 0.40) |
| `MAX_CHUNK_TOKENS` | Max tokens per chunk (default: 500) |
| `MAX_ISSUES` | Max issues to scrape (default: 900) |
| `MAX_PRS` | Max PRs to scrape (default: 1000) |
| `KUBELET_ISSUE_LABELS` | GitHub labels to filter (default: area/kubelet,component/kubelet) |
| `KUBELET_FILE_PREFIX` | File path filter (default: pkg/kubelet) |

---

## Commands

### Start Services
```bash
# Start Qdrant and Redis
docker compose up -d

# Start backend
uvicorn api.main:app --reload --port 8000

# Start frontend
cd repomind-ui && npm run dev
```

### Reindex Data
```bash
# Scrape GitHub data
python ingestion/github_scraper.py

# Index into Qdrant + SQLite
python index_small.py

# Create file-to-issue links
python ingestion/linker.py
```

### Test Retrieval
```bash
python -c "from retrieval.pipeline import retrieve; c,s,r = retrieve('How does kubelet handle pod eviction?', 'kubernetes_kubernetes'); print(f'Score: {s}, Chunks: {len(c)}')"
```

---

## Conventions

- **LLM:** Use Groq `llama-3.3-70b-versatile` for generation and query rewriting
- **Embeddings:** sentence-transformers `all-MiniLM-L6-v2`
- **Confidence Threshold:** 0.40 (set via `CONFIDENCE_THRESHOLD` env var)
- **Citations:** Always cite sources with [#N] inline citations from chunks
- **Verification:** Citation verification is mandatory - removes invalid citations
- **Session Expiry:** 7 days for user sessions

---

## API Endpoints Summary

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Health check |
| `/api/chat` | POST | Streaming chat with RAG |
| `/api/register` | POST | User registration |
| `/api/login` | POST | User login |
| `/api/logout` | POST | User logout |
| `/api/me` | GET | Get current user |
| `/api/history` | GET/POST/DELETE | Chat history management |
| `/api/admin/queries` | GET/DELETE | Admin query stats |
| `/api/index` | POST | Trigger reindexing |
