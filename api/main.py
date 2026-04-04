from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from .routes import chat, index, webhook

app = FastAPI(title='RepoMind')

app.add_middleware(CORSMiddleware,
    allow_origins=['http://localhost:5173'],
    allow_methods=['*'],
    allow_headers=['*'],
)

app.include_router(chat.router,    prefix='/api')
app.include_router(index.router,   prefix='/api')
app.include_router(webhook.router, prefix='/api')

@app.get('/health')
def health():
    return {'status': 'ok'}