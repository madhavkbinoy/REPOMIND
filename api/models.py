from pydantic import BaseModel

class Message(BaseModel):
    role:    str
    content: str

class ChatRequest(BaseModel):
    question: str
    repo:     str           = 'kubernetes/kubernetes'
    history:  list[Message] = []
    user_id:  int | None    = None

class IndexRequest(BaseModel):
    repo: str