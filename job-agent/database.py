"""
数据库 — engine + 建表
共享 POKEE 提醒服务的 reminders.db
"""
from pathlib import Path

from sqlalchemy import create_engine, text

from config import settings

Path("data").mkdir(exist_ok=True)

engine = create_engine(settings.database_url, connect_args={"check_same_thread": False})


def init_db():
    """创建求职助手所有表"""
    with engine.connect() as conn:
        # users 表（独立部署，不再依赖 POKEE）
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                token TEXT,
                created_at TEXT NOT NULL,
                persona TEXT NOT NULL DEFAULT 'default',
                points INTEGER NOT NULL DEFAULT 0,
                lang TEXT NOT NULL DEFAULT 'zh',
                profile TEXT NOT NULL DEFAULT '{}',
                last_login_at TEXT
            )
        """))
        conn.commit()

        # ── 求职助手专属表 ──
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
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS growth_tasks (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                title TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'skill',
                status TEXT NOT NULL DEFAULT 'pending',
                sort_order INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """))
        # 补加成长任务扩展列（兼容已有表）
        for col, defn in [
            ("sort_order", "INTEGER DEFAULT 0"),
            ("description", "TEXT DEFAULT ''"),
            ("target_date", "TEXT DEFAULT ''"),
            ("completed_at", "TEXT"),
            ("last_checkin_at", "TEXT"),
            ("checkin_count", "INTEGER DEFAULT 0"),
            ("user_responded_at", "TEXT"),
            ("silence_days", "INTEGER DEFAULT 0"),
            ("last_tone", "TEXT DEFAULT ''"),
        ]:
            try:
                conn.execute(text(f"ALTER TABLE growth_tasks ADD COLUMN {col} {defn}"))
                conn.commit()
            except Exception:
                pass
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS growth_checkins (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                direction TEXT NOT NULL,
                message TEXT NOT NULL,
                tone TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                FOREIGN KEY (task_id) REFERENCES growth_tasks(id)
            )
        """))
        conn.commit()


def spend_points(user_id: str, amount: int, reason: str, ref_id: str = None) -> int:
    """扣减积分，amount 为正数（内部单位）。余额不足抛出 ValueError。返回扣后余额。"""
    from datetime import datetime

    now = datetime.utcnow().isoformat()

    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT points FROM users WHERE id = :uid"), {"uid": user_id}
        ).fetchone()
        current = row.points if row else 0

        if current < amount:
            raise ValueError(f"积分不足（需要 {round(amount / 10, 1)}，当前 {round(current / 10, 1)}）")

        conn.execute(
            text("UPDATE users SET points = points - :amt WHERE id = :uid"),
            {"amt": amount, "uid": user_id},
        )
        conn.execute(
            text("INSERT INTO points_ledger (user_id, amount, reason, ref_id, created_at) "
                 "VALUES (:uid, :amt, :reason, :ref, :now)"),
            {"uid": user_id, "amt": -amount, "reason": reason, "ref": ref_id, "now": now},
        )
        conn.commit()

        new_balance = current - amount
    return new_balance


def get_user_lang(user_id: str) -> str:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT lang FROM users WHERE id = :uid"), {"uid": user_id}
        ).fetchone()
    return row.lang if row and row.lang else "zh"
