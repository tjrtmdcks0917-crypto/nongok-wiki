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
    real_name VARCHAR(30),
    student_no VARCHAR(6),
    school_name VARCHAR(80),
    profile_name VARCHAR(30),
    profile_bio VARCHAR(300),
    profile_status VARCHAR(80),
    profile_color VARCHAR(7) NOT NULL DEFAULT '#87aa43',
    profile_emoji VARCHAR(8),
    role VARCHAR(16) NOT NULL DEFAULT 'user',
    account_status VARCHAR(16) NOT NULL DEFAULT 'approved',
    is_graduate BOOLEAN NOT NULL DEFAULT FALSE,
    graduation_year INTEGER,
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
CREATE TABLE IF NOT EXISTS pending_document_edits (
    id INTEGER PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    page_id INTEGER NOT NULL,
    proposed_content TEXT NOT NULL,
    submitter_id INTEGER NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'pending',
    reviewer_id INTEGER,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    reviewed_at TIMESTAMP
);
CREATE TABLE IF NOT EXISTS discussions (
    id INTEGER PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    page_id INTEGER NOT NULL,
    user_id INTEGER,
    body TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    user_id INTEGER NOT NULL,
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
CREATE TABLE IF NOT EXISTS polls (
    id INTEGER PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    question VARCHAR(200) NOT NULL,
    created_by INTEGER,
    is_open BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS poll_options (
    id INTEGER PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    poll_id INTEGER NOT NULL,
    option_text VARCHAR(120) NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS poll_votes (
    id INTEGER PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    poll_id INTEGER NOT NULL,
    option_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (poll_id, user_id)
);
CREATE TABLE IF NOT EXISTS gallery_posts (
    id INTEGER PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    user_id INTEGER NOT NULL,
    title VARCHAR(100) NOT NULL,
    body TEXT NOT NULL,
    views INTEGER NOT NULL DEFAULT 0,
    deleted BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS gallery_images (
    id INTEGER PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    post_id INTEGER NOT NULL,
    mime_type VARCHAR(32) NOT NULL,
    image_data BYTEA NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS gallery_comments (
    id INTEGER PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    post_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    body TEXT NOT NULL,
    deleted BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS gallery_reads (
    user_id INTEGER PRIMARY KEY,
    last_seen_post_id INTEGER NOT NULL DEFAULT 0,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS follows (
    follower_id INTEGER NOT NULL,
    following_id INTEGER NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (follower_id, following_id)
);
CREATE TABLE IF NOT EXISTS school_space_posts (
    id INTEGER PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    scope_type VARCHAR(8) NOT NULL,
    grade INTEGER NOT NULL,
    class_no INTEGER,
    user_id INTEGER NOT NULL,
    title VARCHAR(100) NOT NULL,
    body TEXT NOT NULL,
    is_pinned BOOLEAN NOT NULL DEFAULT FALSE,
    deleted BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    user_id INTEGER NOT NULL,
    notification_type VARCHAR(32) NOT NULL,
    title VARCHAR(160) NOT NULL,
    body VARCHAR(300),
    target_url VARCHAR(300),
    is_read BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_notifications_user_read
    ON notifications(user_id, is_read, id);
CREATE TABLE IF NOT EXISTS admin_activity_logs (
    id INTEGER PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    actor_id INTEGER,
    action VARCHAR(80) NOT NULL,
    target_type VARCHAR(40),
    target_id VARCHAR(64),
    detail VARCHAR(500),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS site_visits (
    id INTEGER PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    visit_date VARCHAR(10) NOT NULL,
    visitor_key VARCHAR(64) NOT NULL,
    first_seen_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (visit_date, visitor_key)
);
CREATE INDEX IF NOT EXISTS idx_wiki_pages_deleted_updated
    ON wiki_pages(deleted, updated_at);
CREATE INDEX IF NOT EXISTS idx_page_views_page_time
    ON page_views(page_id, viewed_at);
CREATE INDEX IF NOT EXISTS idx_gallery_posts_deleted_id
    ON gallery_posts(deleted, id);
CREATE INDEX IF NOT EXISTS idx_gallery_images_post_id
    ON gallery_images(post_id, id);
CREATE INDEX IF NOT EXISTS idx_gallery_comments_post_deleted
    ON gallery_comments(post_id, deleted, id);
CREATE INDEX IF NOT EXISTS idx_site_visits_date_seen
    ON site_visits(visit_date, first_seen_at);
"""

BACKUP_TABLE_COLUMNS = {
    "users": ["id", "username", "password_hash", "real_name", "student_no", "school_name", "profile_name", "profile_bio", "profile_status", "profile_color", "profile_emoji", "role", "account_status", "is_graduate", "graduation_year", "created_at"],
    "wiki_pages": ["id", "title", "content", "author_id", "created_at", "updated_at", "views", "protected", "deleted"],
    "revisions": ["id", "page_id", "title", "content", "author_id", "created_at"],
    "pending_document_edits": ["id", "page_id", "proposed_content", "submitter_id", "status", "reviewer_id", "created_at", "reviewed_at"],
    "discussions": ["id", "page_id", "user_id", "body", "created_at"],
    "chat_messages": ["id", "user_id", "body", "created_at"],
    "reports": ["id", "page_id", "user_id", "reason", "status", "created_at"],
    "homepage_sections": ["section_key", "content", "updated_at"],
    "page_views": ["id", "page_id", "viewed_at"],
    "polls": ["id", "question", "created_by", "is_open", "created_at"],
    "poll_options": ["id", "poll_id", "option_text", "sort_order"],
    "poll_votes": ["id", "poll_id", "option_id", "user_id", "created_at"],
    "gallery_posts": ["id", "user_id", "title", "body", "views", "deleted", "created_at"],
    "gallery_images": ["id", "post_id", "mime_type", "image_data", "sort_order", "created_at"],
    "gallery_comments": ["id", "post_id", "user_id", "body", "deleted", "created_at"],
    "gallery_reads": ["user_id", "last_seen_post_id", "updated_at"],
    "follows": ["follower_id", "following_id", "created_at"],
    "school_space_posts": ["id", "scope_type", "grade", "class_no", "user_id", "title", "body", "is_pinned", "deleted", "created_at"],
    "notifications": ["id", "user_id", "notification_type", "title", "body", "target_url", "is_read", "created_at"],
    "admin_activity_logs": ["id", "actor_id", "action", "target_type", "target_id", "detail", "created_at"],
    "site_visits": ["id", "visit_date", "visitor_key", "first_seen_at"],
}

BACKUP_IDENTITY_TABLES = {
    table for table, columns in BACKUP_TABLE_COLUMNS.items() if "id" in columns
}


def backup_rows():
    """Read a consistent full application backup using only known tables/columns."""
    data = {}
    with connection() as conn:
        for table, columns in BACKUP_TABLE_COLUMNS.items():
            column_sql = ", ".join(columns)
            cur = conn.execute(f"SELECT {column_sql} FROM {table}")
            data[table] = [dict(row) for row in cur.fetchall()]
    return data


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
            conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS real_name VARCHAR(30)")
            conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS student_no VARCHAR(6)")
            conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS school_name VARCHAR(80)")
            conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS profile_name VARCHAR(30)")
            conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS profile_bio VARCHAR(300)")
            conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS profile_status VARCHAR(80)")
            conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS profile_color VARCHAR(7) NOT NULL DEFAULT '#87aa43'")
            conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS profile_emoji VARCHAR(8)")
            conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS account_status VARCHAR(16) NOT NULL DEFAULT 'approved'")
            conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_graduate BOOLEAN NOT NULL DEFAULT FALSE")
            conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS graduation_year INTEGER")
            conn.execute("ALTER TABLE gallery_posts ADD COLUMN IF NOT EXISTS views INTEGER NOT NULL DEFAULT 0")
            conn.execute("UPDATE users SET role='graduate' WHERE is_graduate=TRUE AND role='user'")
            conn.execute("DROP INDEX IF EXISTS idx_users_student_no_unique")
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_current_student_no_unique "
                "ON users(student_no) WHERE student_no IS NOT NULL AND is_graduate=FALSE"
            )
            conn.execute("DROP INDEX IF EXISTS idx_users_graduate_identity_unique")
        else:
            conn.executescript(_sqlite_schema())
            columns = {row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
            if "real_name" not in columns:
                conn.execute("ALTER TABLE users ADD COLUMN real_name VARCHAR(30)")
            if "student_no" not in columns:
                conn.execute("ALTER TABLE users ADD COLUMN student_no VARCHAR(6)")
            if "school_name" not in columns:
                conn.execute("ALTER TABLE users ADD COLUMN school_name VARCHAR(80)")
            if "profile_name" not in columns:
                conn.execute("ALTER TABLE users ADD COLUMN profile_name VARCHAR(30)")
            if "profile_bio" not in columns:
                conn.execute("ALTER TABLE users ADD COLUMN profile_bio VARCHAR(300)")
            if "profile_status" not in columns:
                conn.execute("ALTER TABLE users ADD COLUMN profile_status VARCHAR(80)")
            if "profile_color" not in columns:
                conn.execute("ALTER TABLE users ADD COLUMN profile_color VARCHAR(7) NOT NULL DEFAULT '#87aa43'")
            if "profile_emoji" not in columns:
                conn.execute("ALTER TABLE users ADD COLUMN profile_emoji VARCHAR(8)")
            if "account_status" not in columns:
                conn.execute("ALTER TABLE users ADD COLUMN account_status VARCHAR(16) NOT NULL DEFAULT 'approved'")
            if "is_graduate" not in columns:
                conn.execute("ALTER TABLE users ADD COLUMN is_graduate BOOLEAN NOT NULL DEFAULT 0")
            if "graduation_year" not in columns:
                conn.execute("ALTER TABLE users ADD COLUMN graduation_year INTEGER")
            gallery_columns = {row[1] for row in conn.execute("PRAGMA table_info(gallery_posts)").fetchall()}
            if "views" not in gallery_columns:
                conn.execute("ALTER TABLE gallery_posts ADD COLUMN views INTEGER NOT NULL DEFAULT 0")
            conn.execute("UPDATE users SET role='graduate' WHERE is_graduate=1 AND role='user'")
            conn.execute("DROP INDEX IF EXISTS idx_users_student_no_unique")
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_current_student_no_unique "
                "ON users(student_no) WHERE student_no IS NOT NULL AND is_graduate=0"
            )
            conn.execute("DROP INDEX IF EXISTS idx_users_graduate_identity_unique")

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
    rows = query("SELECT id, account_status FROM users WHERE username=%s", (username,))
    if rows:
        if rows[0].get("account_status") != "approved":
            execute("UPDATE users SET account_status='approved' WHERE id=%s", (rows[0]["id"],))
        return
    from werkzeug.security import generate_password_hash
    execute(
        "INSERT INTO users(username,password_hash,role,account_status,created_at) VALUES (%s,%s,'admin','approved',CURRENT_TIMESTAMP)",
        (username, generate_password_hash(password)),
    )
