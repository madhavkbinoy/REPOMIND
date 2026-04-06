import json, asyncio, os
import sqlite3
from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from groq import Groq
from ..models import ChatRequest
from retrieval.pipeline import retrieve
from generation.generator import generate, format_context, dedupe_sources, verify_citations, FALLBACK_MESSAGE
from generation.prompts import SYSTEM_PROMPT
from dotenv import load_dotenv
from .admin import track_out_of_scope_query

load_dotenv()

router    = APIRouter()
client    = Groq(api_key=os.getenv('GROQ_API_KEY'))
THRESHOLD = float(os.getenv('CONFIDENCE_THRESHOLD', 0.40))
DB_PATH   = os.getenv('DATABASE_PATH', './db/repomind.db')


def get_db():
    conn = sqlite3.connect(DB_PATH)
    return conn


def save_message(user_id: int, role: str, content: str):
    conn = get_db()
    try:
        conn.execute(
            'INSERT INTO chat_history (user_id, role, content) VALUES (?, ?, ?)',
            (user_id, role, content)
        )
        conn.commit()
    finally:
        conn.close()


def get_user_history(user_id: int) -> list:
    conn = get_db()
    try:
        messages = conn.execute(
            '''SELECT role, content FROM chat_history 
               WHERE user_id = ? ORDER BY created_at ASC''',
            (user_id,)
        ).fetchall()
        return [{'role': m[0], 'content': m[1]} for m in messages]
    finally:
        conn.close()


@router.post('/chat')
async def chat(req: ChatRequest):
    collection = req.repo.replace('/', '_')
    
    # Load history from DB if user_id provided, otherwise use request history
    if req.user_id:
        db_history = get_user_history(req.user_id)
        history = db_history if db_history else [m.dict() for m in req.history]
    else:
        history = [m.dict() for m in req.history]
    
    loop       = asyncio.get_event_loop()

    chunks, best_score, _ = await loop.run_in_executor(
        None, retrieve, req.question, collection, history
    )

    if best_score < THRESHOLD or not chunks:
        # Track out-of-scope query
        track_out_of_scope_query(req.question)
        return {'answer': FALLBACK_MESSAGE, 'sources': [], 'is_fallback': True, 'best_score': best_score}

    context  = format_context(chunks)
    system   = SYSTEM_PROMPT.format(repo=req.repo, context=context)
    messages = [{'role': m['role'], 'content': m['content']} for m in history[-6:]]
    messages.append({'role': 'user', 'content': req.question})

    full_answer = ''

    async def stream():
        nonlocal full_answer
        with client.chat.completions.create(
            model='llama-3.3-70b-versatile',
            max_tokens=1500,
            messages=[{'role': 'system', 'content': system}] + messages,
            stream=True
        ) as s:
            for chunk in s:
                text = chunk.choices[0].delta.content or ''
                if text:
                    full_answer += text
                    yield f'data: {json.dumps({"token": text})}\n\n'
        
        verification = await loop.run_in_executor(
            None, verify_citations, full_answer, chunks
        )
        
        final_answer = verification.get('verified_answer', full_answer)
        citations_valid = verification.get('valid', True)
        
        # Save chat to history if user is logged in
        if req.user_id:
            save_message(req.user_id, 'user', req.question)
            save_message(req.user_id, 'assistant', final_answer)
        
        meta = {
            'done': True,
            'sources': dedupe_sources(chunks),
            'is_fallback': False,
            'best_score': best_score,
            'citations_valid': citations_valid,
            'invalid_citations': verification.get('invalid_citations', []),
        }
        yield f'data: {json.dumps(meta)}\n\n'

    return StreamingResponse(stream(), media_type='text/event-stream')