"""
数据库 — engine + 建表
"""
from pathlib import Path

from sqlalchemy import create_engine, text

from config import settings

Path("data").mkdir(exist_ok=True)

engine = create_engine(settings.database_url, connect_args={"check_same_thread": False})


def init_db():
    with engine.connect() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                token TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS resumes (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                name TEXT NOT NULL,
                content TEXT NOT NULL,
                is_default INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS saved_jobs (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                resume_id TEXT,
                title TEXT NOT NULL,
                company TEXT NOT NULL,
                platform TEXT DEFAULT '',
                url TEXT DEFAULT '',
                jd_text TEXT DEFAULT '',
                score_total REAL,
                score_details TEXT DEFAULT '{}',
                verdict TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS star_stories (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                resume_id TEXT,
                title TEXT NOT NULL,
                situation TEXT NOT NULL,
                task TEXT NOT NULL,
                action TEXT NOT NULL,
                result TEXT NOT NULL,
                tags TEXT DEFAULT '[]',
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """))
        # Migrate: move data from old master_resumes to new resumes table
        try:
            old = conn.execute(text("SELECT user_id, content, created_at FROM master_resumes")).fetchall()
            for row in old:
                existing = conn.execute(
                    text("SELECT id FROM resumes WHERE user_id = :uid AND name = '默认简历'"),
                    {"uid": row.user_id},
                ).fetchone()
                if not existing:
                    import uuid
                    conn.execute(
                        text("INSERT INTO resumes (id, user_id, name, content, is_default, created_at, updated_at) "
                             "VALUES (:id, :uid, :name, :content, 1, :cat, :uat)"),
                        {"id": str(uuid.uuid4()), "uid": row.user_id, "name": "默认简历",
                         "content": row.content, "cat": row.created_at, "uat": row.created_at},
                    )
            conn.commit()
        except Exception:
            pass
        conn.commit()
