import os
import sqlite3
from contextlib import contextmanager

_PG_CONN = None

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
USE_POSTGRES = bool(DATABASE_URL)

if USE_POSTGRES:
    import psycopg
    from psycopg.rows import dict_row
else:
    DB_PATH = os.path.join(os.path.dirname(__file__), "data", "wiki.db")
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    username VARCHAR(24) UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role VARCHAR(16) NOT NULL DEFAULT 'user',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS wiki_pages (
    id INTEGER PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    title VARCHAR(120) UNIQUE NOT NULL,
    content TEXT NOT NULL,
    author_id INTEGER,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    views INTEGER NOT NULL DEFAULT 0,
    protected BOOLEAN NOT NULL DEFAULT FALSE,
    deleted BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE TABLE IF NOT EXISTS revisions (
    id INTEGER PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    page_id INTEGER NOT NULL,
    title VARCHAR(120) NOT NULL,
    content TEXT NOT NULL,
    author_id INTEGER,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS discussions (
    id INTEGER PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    page_id INTEGER NOT NULL,
    user_id INTEGER,
    body TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    page_id INTEGER,
    user_id INTEGER,
    reason TEXT NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'open',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS homepage_sections (
    section_key VARCHAR(32) PRIMARY KEY,
    content TEXT NOT NULL,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS page_views (
    id INTEGER PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    page_id INTEGER NOT NULL,
    viewed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

def _sqlite_schema():
    return SCHEMA.replace("GENERATED ALWAYS AS IDENTITY", "AUTOINCREMENT").replace("BOOLEAN NOT NULL DEFAULT FALSE", "INTEGER NOT NULL DEFAULT 0")

def get_db_type():
    return "postgres" if USE_POSTGRES else "sqlite"

@contextmanager
def connection():
    global _PG_CONN
    if USE_POSTGRES:
        # Reuse one PostgreSQL connection instead of opening a new TLS/database
        # connection for every small query. Reconnect automatically if needed.
        if _PG_CONN is None or _PG_CONN.closed:
            _PG_CONN = psycopg.connect(DATABASE_URL, row_factory=dict_row)
        conn = _PG_CONN
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            # A broken network connection should be recreated on the next query.
            if conn.closed:
                _PG_CONN = None
            raise
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

def init_db():
    with connection() as conn:
        if USE_POSTGRES:
            for statement in SCHEMA.split(";"):
                if statement.strip():
                    conn.execute(statement)
        else:
            conn.executescript(_sqlite_schema())

def query(sql, params=()):
    if not USE_POSTGRES:
        sql = sql.replace("%s", "?")
        sql = sql.replace("ILIKE", "LIKE")
    with connection() as conn:
        cur = conn.execute(sql, params)
        return [dict(row) for row in cur.fetchall()]

def execute(sql, params=()):
    if not USE_POSTGRES:
        sql = sql.replace("%s", "?")
        sql = sql.replace("NOT protected", "NOT protected")
    with connection() as conn:
        cur = conn.execute(sql, params)
        try:
            return cur.lastrowid
        except Exception:
            return None

def ensure_admin():
    username = os.environ.get("ADMIN_USERNAME", "admin").strip()
    password = os.environ.get("ADMIN_PASSWORD", "")
    if not password:
        return
    rows = query("SELECT id FROM users WHERE username=%s", (username,))
    if rows:
        return
    from werkzeug.security import generate_password_hash
    execute("INSERT INTO users(username,password_hash,role,created_at) VALUES (%s,%s,'admin',CURRENT_TIMESTAMP)", (username, generate_password_hash(password)))
