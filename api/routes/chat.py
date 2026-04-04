import json, asyncio, os
from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from groq import Groq
from ..models import ChatRequest
from retrieval.pipeline import retrieve
from generation.generator import generate, format_context, dedupe_sources, verify_citations, FALLBACK_MESSAGE
from generation.prompts import SYSTEM_PROMPT
from dotenv import load_dotenv

load_dotenv()

router    = APIRouter()
client    = Groq(api_key=os.getenv('GROQ_API_KEY'))
THRESHOLD = float(os.getenv('CONFIDENCE_THRESHOLD', 0.40))


@router.post('/chat')
async def chat(req: ChatRequest):
    collection = req.repo.replace('/', '_')
    history    = [m.dict() for m in req.history]
    loop       = asyncio.get_event_loop()

    chunks, best_score, _ = await loop.run_in_executor(
        None, retrieve, req.question, collection, history
    )

    if best_score < THRESHOLD or not chunks:
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