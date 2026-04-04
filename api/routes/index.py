from fastapi import APIRouter
from ..models import IndexRequest
from workers.tasks import full_index_repo

router = APIRouter()

@router.post('/index')
def trigger_index(req: IndexRequest):
    task = full_index_repo.delay(req.repo)
    return {'task_id': task.id, 'status': 'queued'}

@router.get('/repos')
def list_repos():
    from db.setup import qdrant
    return {'repos': [c.name.replace('_', '/') for c in qdrant.get_collections().collections]}