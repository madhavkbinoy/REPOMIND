# Two constraints drive this file, both measured rather than assumed:
#
#   1. Cold start. pipeline.py, reranker.py and embedder.py all load models at
#      import time -- 17 seconds locally. If sentence-transformers has to download
#      them on first request instead, that becomes minutes and depends on an
#      unauthenticated Hugging Face rate limit. So the models are baked in at build.
#
#   2. Image size. The default torch wheel bundles CUDA libraries that are dead
#      weight on a CPU-only host. The CPU index cuts hundreds of megabytes.

FROM python:3.11-slim AS builder

WORKDIR /build
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

# CPU-only torch first, so the resolver never pulls the CUDA build as a dependency.
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
 && pip install --no-cache-dir -r requirements.txt

# Bake the models into the image. Both are ~90 MB and never change.
ENV HF_HOME=/opt/hf
RUN python -c "\
from sentence_transformers import SentenceTransformer, CrossEncoder; \
SentenceTransformer('all-MiniLM-L6-v2'); \
CrossEncoder('cross-encoder/ms-marco-MiniLM-L-6-v2'); \
print('models cached')"


FROM python:3.11-slim

WORKDIR /app

ENV HF_HOME=/opt/hf \
    HF_HUB_OFFLINE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY --from=builder /opt/hf /opt/hf

COPY api/        ./api/
COPY retrieval/  ./retrieval/
COPY generation/ ./generation/
COPY ingestion/  ./ingestion/
COPY db/         ./db/

# HF_HUB_OFFLINE above makes a missing model fail loudly at build/boot rather than
# silently reaching for the network in production.

RUN useradd --create-home --uid 10001 repomind && chown -R repomind /app
USER repomind

EXPOSE 8000

# Single worker on purpose: each one holds its own copy of MiniLM + the
# cross-encoder in memory. Two workers double the RAM for no throughput gain on a
# shared-cpu instance.
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
