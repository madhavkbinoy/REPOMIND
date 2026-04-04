from fastapi import APIRouter, Request
from workers.tasks import update_issue_task

router = APIRouter()

@router.post('/webhook/github')
async def github_webhook(req: Request):
    payload = await req.json()
    event   = req.headers.get('X-GitHub-Event')
    if event in ('issues', 'issue_comment'):
        repo   = payload['repository']['full_name']
        number = payload['issue']['number']
        update_issue_task.delay(repo, number)
    return {'ok': True}