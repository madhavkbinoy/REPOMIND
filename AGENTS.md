# RepoMind Agent

## Project Overview
- **Name:** RepoMind
- **Description:** RAG chatbot for GitHub repo design decisions (focused on kubernetes/kubernetes kubelet subsystem)
- **Tech Stack:** Groq LLaMA 3.3-70B, sentence-transformers (all-MiniLM-L6-v2), CrossEncoder, Qdrant, SQLite (BM25), FastAPI, Celery, React (Vite)

## Architecture

### Data Flow
1. **Ingestion:** `ingestion/github_scraper.py` → `chunker.py` → `embedder.py` → Qdrant
2. **Retrieval:** `retrieval/pipeline.py` (RRF fusion of vector + BM25) → reranker → link expansion
3. **Generation:** `generation/generator.py` → citation verification (3-layer anti-hallucination)

### Key Files
- `api/main.py` - FastAPI app with /health, /api/chat, /api/index, /api/webhook routes
- `retrieval/pipeline.py` - Main retrieval with query rewrite, RRF fusion, reranking, link expansion
- `generation/generator.py` - LLM generation with citation verification layer
- `repomind-ui/src/App.jsx` - React frontend with streaming support

### Environment Variables
See `.env.example` for required keys: GROQ_API_KEY, QDRANT_HOST, DATABASE_PATH, etc.

## Commands
```bash
# Start services
docker-compose up -d  # Qdrant, Redis
uvicorn api.main:app --reload --port 8000
cd repomind-ui && npm run dev

# Reindex data
python ingestion/github_scraper.py
python index_small.py
python ingestion/linker.py

# Test retrieval
python -c "from retrieval.pipeline import retrieve; c,s,r = retrieve('How does kubelet handle pod eviction?', 'kubernetes_kubernetes'); print(f'Score: {s}, Chunks: {len(c)}')"
```

## Conventions
- Use Groq `llama-3.3-70b-versatile` for generation and query rewriting
- Vector search uses sentence-transformers `all-MiniLM-L6-v2`
- Confidence threshold: 0.40 (set via CONFIDENCE_THRESHOLD env var)
- Always cite sources with [#N] inline citations from chunks
- Citation verification is mandatory - removes invalid citations
