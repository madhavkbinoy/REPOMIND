import os, sqlite3
from fastapi import Header, HTTPException

DB_PATH = os.getenv('DATABASE_PATH', './db/repomind.db')


def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def current_user(authorization: str = Header(None)) -> dict:
    """Resolve a bearer token to a user. 401 if missing, invalid or expired."""
    if not authorization or not authorization.lower().startswith('bearer '):
        raise HTTPException(status_code=401, detail='Not authenticated')
    token = authorization.split(' ', 1)[1].strip()
    conn = _db()
    try:
        row = conn.execute(
            '''SELECT u.id, u.username, u.is_admin
               FROM sessions s JOIN users u ON u.id = s.user_id
               WHERE s.token = ? AND s.expires_at > datetime("now")''',
            (token,)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(status_code=401, detail='Invalid or expired session')
    return {'user_id': row['id'], 'username': row['username'], 'is_admin': bool(row['is_admin'])}


def optional_user(authorization: str = Header(None)) -> dict | None:
    """Same, but returns None instead of raising — for endpoints that allow anonymous use."""
    try:
        return current_user(authorization)
    except HTTPException:
        return None
