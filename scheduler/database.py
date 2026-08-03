"""
数据库 — engine + session + 建表
"""
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from config import settings

Path("data").mkdir(exist_ok=True)

engine = create_engine(settings.database_url, connect_args={"check_same_thread": False})


def init_db():
    """创建所有表（幂等）"""
    with engine.connect() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS reminders (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL DEFAULT 'default',
                task TEXT NOT NULL,
                run_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                sent_at TEXT,
                seen_at TEXT
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                token TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                last_login_at TEXT
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS user_im_bindings (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                platform TEXT NOT NULL,
                im_user_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(user_id, platform),
                UNIQUE(platform, im_user_id)
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS feishu_credentials (
                user_id TEXT PRIMARY KEY,
                app_id TEXT NOT NULL,
                app_secret TEXT NOT NULL,
                open_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """))
        conn.commit()
