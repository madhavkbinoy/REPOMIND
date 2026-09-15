#!/bin/bash
# setup.sh - One-command setup for RepoMind
# Run this after cloning the repo and setting up .env

set -e

echo "=== RepoMind Setup ==="
echo ""

# Check if venv exists
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
fi

# Activate venv
echo "Activating virtual environment..."
source venv/bin/activate

# Install dependencies
echo "Installing Python dependencies..."
pip install -r requirements.txt > /dev/null 2>&1

# Check if .env exists
if [ ! -f ".env" ]; then
    echo "Creating .env from template..."
    cp .env.example .env
    echo "⚠️  Please edit .env and add your GROQ_API_KEY and GITHUB_TOKEN"
    exit 1
fi

# Initialize database
echo "Initializing database..."
python db/setup.py
echo "✓ Database initialized"

# Scrape kubelet data
echo ""
echo "=== Scraping kubelet data ==="
echo "Blobless clone + targeted fetch. ~20-30 minutes, resumable."
python ingestion/scraper_v2.py all
echo "✓ Commits, PRs and issues scraped"

echo ""
echo "=== Scraping sig-node KEPs ==="
python ingestion/kep_scraper.py
echo "✓ KEPs and community docs scraped"

# Index data
echo ""
echo "=== Indexing data to Qdrant ==="
python index_small.py
echo "✓ Indexing complete"

echo ""
echo "=== Setup Complete! ==="
echo ""
echo "To start the API server:"
echo "  uvicorn api.main:app --reload --port 8000"
echo ""
echo "To start the frontend (in another terminal):"
echo "  cd repomind-ui && npm install && npm run dev"
echo ""
echo "Then open http://localhost:5173 in your browser"