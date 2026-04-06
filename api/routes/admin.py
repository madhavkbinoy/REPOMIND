import os
import sqlite3
import hashlib
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

router = APIRouter()

DB_PATH = os.getenv('DATABASE_PATH', './db/repomind.db')
ADMIN_USERNAME = os.getenv('ADMIN_USERNAME', 'admin')
ADMIN_PASSWORD_HASH = hashlib.sha256(os.getenv('ADMIN_PASSWORD', 'admin').encode()).hexdigest()


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def verify_admin_credentials(username: str, password: str) -> bool:
    return username == ADMIN_USERNAME and hashlib.sha256(password.encode()).hexdigest() == ADMIN_PASSWORD_HASH


@router.get('/admin/queries')
def get_out_of_scope_queries(admin_user: str = None, admin_pass: str = None):
    if not admin_user or not admin_pass:
        raise HTTPException(status_code=401, detail='Admin credentials required')
    
    if not verify_admin_credentials(admin_user, admin_pass):
        raise HTTPException(status_code=401, detail='Invalid admin credentials')
    
    conn = get_db()
    try:
        queries = conn.execute(
            '''SELECT query, count, first_seen, last_seen 
               FROM out_of_scope_queries 
               ORDER BY count DESC'''
        ).fetchall()
        
        return [{'query': q['query'], 'count': q['count'], 
                'first_seen': q['first_seen'], 'last_seen': q['last_seen']} for q in queries]
    finally:
        conn.close()


@router.delete('/admin/queries')
def clear_queries(admin_user: str = None, admin_pass: str = None):
    if not admin_user or not admin_pass:
        raise HTTPException(status_code=401, detail='Admin credentials required')
    
    if not verify_admin_credentials(admin_user, admin_pass):
        raise HTTPException(status_code=401, detail='Invalid admin credentials')
    
    conn = get_db()
    try:
        conn.execute('DELETE FROM out_of_scope_queries')
        conn.commit()
        return {'ok': True}
    finally:
        conn.close()


def track_out_of_scope_query(query: str):
    conn = get_db()
    try:
        existing = conn.execute(
            'SELECT id, count FROM out_of_scope_queries WHERE query = ?',
            (query,)
        ).fetchone()
        
        if existing:
            conn.execute(
                'UPDATE out_of_scope_queries SET count = count + 1, last_seen = datetime("now") WHERE id = ?',
                (existing['id'],)
            )
        else:
            conn.execute(
                'INSERT INTO out_of_scope_queries (query, count) VALUES (?, 1)',
                (query,)
            )
        conn.commit()
    finally:
        conn.close()
