import os, uuid, sqlite3
from sentence_transformers import SentenceTransformer
from qdrant_client import QdrantClient
from qdrant_client.models import PointStruct
from dotenv import load_dotenv

load_dotenv()

_model = SentenceTransformer('all-MiniLM-L6-v2')
qdrant = QdrantClient(host=os.getenv('QDRANT_HOST', 'localhost'), port=int(os.getenv('QDRANT_PORT', 6333)))
db     = sqlite3.connect(os.getenv('DATABASE_PATH', './db/repomind.db'))
BATCH  = 100


def embed_texts(texts: list[str]) -> list[list[float]]:
    return _model.encode(texts, show_progress_bar=False).tolist()


def upsert_chunks(chunks: list[dict], collection: str):
    total = len(chunks)
    for i in range(0, total, BATCH):
        batch = chunks[i:i+BATCH]
        vecs  = embed_texts([c['text'] for c in batch])
        points = []
        for chunk, vec in zip(batch, vecs):
            pid = str(uuid.uuid4())
            points.append(PointStruct(
                id=pid, vector=vec,
                payload={
                    'text':        chunk['text'],
                    'source_type': chunk['source_type'],
                    'repo':        chunk.get('repo'),
                    'number':      chunk.get('number'),
                    'title':       chunk.get('title'),
                    'url':         chunk.get('url'),
                    'labels':      chunk.get('labels', []),
                    'state':       chunk.get('state'),
                    'chunk_index': chunk.get('chunk_index', 0),
                    'file_path':   chunk.get('file_path'),
                }
            ))
            db.execute('INSERT OR REPLACE INTO chunks VALUES (?,?,?,?,?,?,?,?)',
                (pid, chunk.get('repo'), chunk['source_type'], chunk.get('number'),
                 chunk.get('file_path'), chunk.get('chunk_index', 0),
                 chunk.get('url'), chunk.get('title')))
            db.execute('INSERT OR REPLACE INTO bm25_index VALUES (?,?)',
                (pid, chunk['text']))
        qdrant.upsert(collection_name=collection, points=points)
        db.commit()
        print(f'Batch {i//BATCH+1}/{(total+BATCH-1)//BATCH} done ({len(batch)} chunks)')