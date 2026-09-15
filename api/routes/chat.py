import json, asyncio, os
import sqlite3
from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from groq import Groq, APIStatusError
from ..models import ChatRequest
from ..deps import optional_user
from ..ratelimit import rate_limit
from retrieval.pipeline import retrieve
from generation.generator import (
    should_answer, fallback_response, build_messages, finalize, dedupe_sources,
    MAX_ANSWER_TOKENS,
)
from dotenv import load_dotenv
from .admin import track_out_of_scope_query

load_dotenv()

router    = APIRouter()
client    = Groq(api_key=os.getenv('GROQ_API_KEY'))
MODEL     = os.getenv('GROQ_MODEL', 'openai/gpt-oss-120b')
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
async def chat(req: ChatRequest, request: Request,
               user: dict | None = Depends(optional_user),
               _rl: None = Depends(rate_limit)):
    collection = req.repo.replace('/', '_')
    uid        = user['user_id'] if user else None

    # Authenticated callers resume their stored history; anonymous ones send theirs.
    if uid:
        db_history = get_user_history(uid)
        history    = db_history if db_history else [m.model_dump() for m in req.history]
    else:
        history = [m.model_dump() for m in req.history]

    loop = asyncio.get_event_loop()
    chunks, best_score, _ = await loop.run_in_executor(
        None, retrieve, req.question, collection, history
    )

    if not should_answer(best_score, chunks):
        track_out_of_scope_query(req.question)
        return fallback_response(best_score)

    messages = build_messages(req.question, chunks, req.repo, history)

    async def stream():
        full = ''
        try:
            upstream = client.chat.completions.create(
                model=MODEL,
                max_tokens=MAX_ANSWER_TOKENS,
                messages=messages,
                stream=True,
            )
        except APIStatusError as e:
            # Rate limits and upstream outages must reach the client as a readable
            # message. Previously the exception escaped the generator and the browser
            # simply received an empty stream, which looks like the app is broken.
            detail = 'The language model is rate limited right now. Try again shortly.' \
                if e.status_code == 429 else 'The language model is unavailable right now.'
            print(f'[chat] upstream {e.status_code}: {str(e)[:160]}')
            yield f'data: {json.dumps({"done": True, "error": detail, "answer": detail, "is_fallback": True, "sources": [], "best_score": best_score})}\n\n'
            return

        with upstream as stream_resp:
            for chunk in stream_resp:
                text = chunk.choices[0].delta.content or ''
                if text:
                    full += text
                    yield f'data: {json.dumps({"token": text})}\n\n'

        # Same finalize() the blocking path uses: insufficient-context check,
        # citation verification, source dedupe. The verified answer ships in the
        # done frame -- the streamed tokens are unverified by construction.
        try:
            result = await loop.run_in_executor(None, finalize, full, chunks, best_score)
        except APIStatusError as e:
            # Verification is best-effort: if the verifier is rate limited, ship the
            # answer flagged as unverified rather than losing it entirely.
            print(f'[chat] verifier unavailable ({e.status_code}); returning unverified')
            result = {'answer': full, 'sources': dedupe_sources(chunks), 'is_fallback': False,
                      'best_score': best_score, 'citations_valid': True,
                      'invalid_citations': [], 'verification_ran': False}

        # The confidence gate above catches low-scoring questions, but the model
        # can also refuse mid-answer with INSUFFICIENT_CONTEXT on a question that
        # scored well. Both are out-of-scope for the dashboard's purposes;
        # tracking only the gate undercounts what the index is missing.
        if result.get('is_fallback'):
            track_out_of_scope_query(req.question)

        if uid:
            save_message(uid, 'user', req.question)
            save_message(uid, 'assistant', result['answer'])

        yield f'data: {json.dumps({"done": True, **result})}\n\n'

    return StreamingResponse(stream(), media_type='text/event-stream')
