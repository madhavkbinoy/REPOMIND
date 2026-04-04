import os, sqlite3
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PayloadSchemaType
from dotenv import load_dotenv

load_dotenv()

qdrant = QdrantClient(
    host=os.getenv('QDRANT_HOST', 'localhost'),
    port=int(os.getenv('QDRANT_PORT', 6333))
)


def create_collection(repo: str):
    name     = repo.replace('/', '_')
    existing = [c.name for c in qdrant.get_collections().collections]
    if name in existing:
        print(f'Collection {name} already exists')
        return
    qdrant.create_collection(
        collection_name=name,
        vectors_config=VectorParams(size=384, distance=Distance.COSINE)
    )
    for field, ftype in [
        ('source_type', PayloadSchemaType.KEYWORD),
        ('state',       PayloadSchemaType.KEYWORD),
        ('number',      PayloadSchemaType.INTEGER),
        ('labels',      PayloadSchemaType.KEYWORD),
    ]:
        qdrant.create_payload_index(name, field, ftype)
    print(f'Created collection: {name}')


def init_sqlite():
    os.makedirs('./db', exist_ok=True)
    db = sqlite3.connect(os.getenv('DATABASE_PATH', './db/repomind.db'))
    db.executescript(open('./db/schema.sql').read())
    db.close()
    print('SQLite schema initialised')


if __name__ == '__main__':
    init_sqlite()
    create_collection('kubernetes/kubernetes')