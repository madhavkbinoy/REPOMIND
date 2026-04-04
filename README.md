# RepoMind

A RAG chatbot that surfaces design decisions, architectural rationale, and undocumented reasoning from GitHub repositories — grounded in issues, PRs, commits, and code.

**Tech Stack:** Groq (LLaMA 3.3-70B), sentence-transformers, CrossEncoder, Qdrant, FastAPI, React (Vite)

**Current Scope:** kubernetes/kubernetes - pkg/kubelet subsystem (issues and PRs with `area/kubelet` label)

---

## Quick Start

### Prerequisites
- Docker Desktop (for Qdrant + Redis)
- Python 3.11+
- Node.js 18+

### Setup

```bash
# 1. Clone the repo
git clone <repo-url>
cd repomind

# 2. Copy environment template
cp .env.example .env

# 3. Edit .env - add your keys:
#    - GROQ_API_KEY: Get from https://console.groq.com
#    - GITHUB_TOKEN: Get from GitHub Settings > Developer settings > Fine-grained tokens
#      (Permissions: Repository contents - Read-only)

# 4. Run setup (15-30 minutes for MAX_PRS=50)
chmod +x setup.sh
./setup.sh

# 5. Start API (in terminal 1)
uvicorn api.main:app --reload --port 8000

# 6. Start Frontend (in terminal 2)
cd repomind-ui
npm install
npm run dev
```

### Verify

```bash
# Test API
curl http://localhost:8000/health
# Expected: {"status":"ok"}

# Test retrieval
python -c "from retrieval.pipeline import retrieve; c,s,r = retrieve('How does kubelet handle pod eviction?', 'kubernetes_kubernetes'); print(f'Score: {s:.2f}, Chunks: {len(c)}')"
```

### Use

Open http://localhost:5173 in your browser and ask questions about kubelet design decisions!

Example questions:
- "How does kubelet handle pod eviction?"
- "Why does kubelet reject pods under disk pressure?"
- "What happens to guaranteed pods under memory pressure?"

---

## Project Structure

```
repomind/
├── ingestion/           # Data scraping and processing
│   ├── github_scraper.py    # GitHub GraphQL API client
│   ├── chunker.py           # Issue/PR chunking logic
│   ├── embedder.py          # Embed and upsert to Qdrant
│   ├── linker.py            # Build code-issue links
│   └── token_utils.py       # Token counting helpers
├── retrieval/           # Query processing
│   ├── vector_search.py     # Qdrant similarity search
│   ├── bm25_search.py       # BM25 keyword search
│   ├── reranker.py          # CrossEncoder reranking
│   └── pipeline.py          # Full retrieval pipeline
├── generation/           # LLM response generation
│   ├── prompts.py          # System prompts
│   └── generator.py        # Generation + citation verification
├── api/                  # FastAPI server
│   ├── main.py            # App entry point
│   ├── models.py          # Pydantic models
│   └── routes/            # API endpoints
│       ├── chat.py        # Chat with streaming
│       ├── index.py       # Trigger repo indexing
│       └── webhook.py     # GitHub webhooks
├── workers/              # Celery async tasks
│   └── tasks.py          # Background job definitions
├── db/                   # Database setup
│   ├── schema.sql        # SQLite schema
│   └── setup.py          # Qdrant collection setup
├── repomind-ui/          # React frontend
├── docker-compose.yml    # Docker services
├── requirements.txt      # Python dependencies
├── setup.sh              # One-command setup script
└── .env.example          # Environment template
```

---

## Scaling Up

By default, the setup uses MAX_PRS=50 for quick testing. To get more comprehensive data:

```bash
# Edit .env - change MAX_PRS=50 to MAX_PRS=200

# Re-run scraping (takes 1-3 hours!)
python ingestion/github_scraper.py
python index_small.py
python ingestion/linker.py
```

---

## Troubleshooting

### Docker not running
```bash
# Start Docker Desktop, then:
docker-compose up -d
```

### Qdrant not accessible
```bash
# Check Qdrant is running
curl http://localhost:6333/dashboard
# Or check containers
docker ps
```

### API returns errors
```bash
# Check API logs
tail -f api.log
```

### No data indexed
```bash
# Re-run setup
./setup.sh
```

---

## Current Status

- **Indexed:** 500 kubelet issues + 50 kubelet PRs = 1426 chunks
- **Anti-hallucination:** 3-layer system (strict prompts, citation verification, UI warnings)
- **Confidence threshold:** 0.40 (tuneable in .env)

---

## License

MIT