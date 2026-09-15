import os
import json, asyncio
import sqlite3
import hashlib
import secrets
import bcrypt
from fastapi import APIRouter, HTTPException, Response, Depends
from pydantic import BaseModel
from dotenv import load_dotenv
from ..deps import current_user

load_dotenv()

router = APIRouter()

DB_PATH = os.getenv('DATABASE_PATH', './db/repomind.db')


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, stored: str) -> bool:
    """Verify against bcrypt, falling back to the legacy unsalted sha256 scheme.

    Legacy hashes are upgraded in place on the next successful login -- see login().
    """
    if stored.startswith('$2'):
        return bcrypt.checkpw(password.encode(), stored.encode())
    return hashlib.sha256(password.encode()).hexdigest() == stored


def is_legacy_hash(stored: str) -> bool:
    return not stored.startswith('$2')


def create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    conn = get_db()
    conn.execute('INSERT INTO sessions (token, user_id, expires_at) VALUES (?, ?, datetime("now", "+7 days"))',
                 (token, user_id))
    conn.commit()
    conn.close()
    return token


class RegisterRequest(BaseModel):
    username: str
    password: str


class LoginRequest(BaseModel):
    username: str
    password: str


@router.post('/register')
def register(req: RegisterRequest):
    conn = get_db()
    try:
        password_hash = hash_password(req.password)
        cursor = conn.execute(
            'INSERT INTO users (username, password_hash) VALUES (?, ?)',
            (req.username, password_hash)
        )
        conn.commit()
        user_id = cursor.lastrowid
        token = create_session(user_id)
        return {'token': token, 'user_id': user_id, 'username': req.username, 'is_admin': False}
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=400, detail='Username already exists')
    finally:
        conn.close()


@router.post('/login')
def login(req: LoginRequest):
    conn = get_db()
    try:
        user = conn.execute(
            'SELECT id, username, password_hash, is_admin FROM users WHERE username = ?',
            (req.username,)
        ).fetchone()
        
        if not user or not verify_password(req.password, user['password_hash']):
            raise HTTPException(status_code=401, detail='Invalid credentials')

        # Transparent migration: re-hash legacy sha256 rows now that we know the
        # plaintext is correct. No password reset, no user-visible change.
        if is_legacy_hash(user['password_hash']):
            conn.execute('UPDATE users SET password_hash = ? WHERE id = ?',
                         (hash_password(req.password), user['id']))
            conn.commit()

        token = create_session(user['id'])
        return {'token': token, 'user_id': user['id'], 'username': user['username'], 'is_admin': bool(user['is_admin'])}
    finally:
        conn.close()


@router.post('/logout')
def logout(response: Response):
    response.set_cookie(key='token', value='', expires=0)
    return {'ok': True}


@router.get('/me')
def get_me(user: dict = Depends(current_user)):
    return {'authenticated': True, **user}


# Chat history endpoints
@router.get('/history')
def get_history(user: dict = Depends(current_user)):
    uid  = user['user_id']
    conn = get_db()
    try:
        messages = conn.execute(
            '''SELECT role, content FROM chat_history 
               WHERE user_id = ? ORDER BY created_at ASC''',
            (uid,)
        ).fetchall()
        return [{'role': m['role'], 'content': m['content']} for m in messages]
    finally:
        conn.close()


@router.post('/history')
def add_message(role: str, content: str, user: dict = Depends(current_user)):
    uid  = user['user_id']
    conn = get_db()
    try:
        conn.execute(
            'INSERT INTO chat_history (user_id, role, content) VALUES (?, ?, ?)',
            (uid, role, content)
        )
        conn.commit()
        return {'ok': True}
    finally:
        conn.close()


@router.delete('/history')
def clear_history(user: dict = Depends(current_user)):
    uid  = user['user_id']
    conn = get_db()
    try:
        conn.execute('DELETE FROM chat_history WHERE user_id = ?', (uid,))
        conn.commit()
        return {'ok': True}
    finally:
        conn.close()
