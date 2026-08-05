"""
数据库 — engine + session + 建表
"""
from pathlib import Path
from typing import Optional

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
                last_login_at TEXT,
                persona TEXT NOT NULL DEFAULT 'default'
            )
        """))
        # 补加 persona 列（兼容已有表）
        try:
            conn.execute(text("ALTER TABLE users ADD COLUMN persona TEXT NOT NULL DEFAULT 'default'"))
            conn.commit()
        except Exception:
            pass
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
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS persona_prefs (
                user_id TEXT PRIMARY KEY,
                persona TEXT NOT NULL DEFAULT 'default'
            )
        """))
        # 积分流水表
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS points_ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                amount INTEGER NOT NULL,
                reason TEXT NOT NULL,
                ref_id TEXT,
                created_at TEXT NOT NULL
            )
        """))
        conn.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_points_ledger_user
            ON points_ledger(user_id, created_at)
        """))
        # 补加 users.points 列（兼容已有表）
        try:
            conn.execute(text("ALTER TABLE users ADD COLUMN points INTEGER NOT NULL DEFAULT 0"))
            conn.commit()
        except Exception:
            pass




def get_user_persona(user_id: str) -> str:
    """查用户的 persona 偏好：优先 users 表，再查 persona_prefs，都不存在返回 default"""
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT persona FROM users WHERE id = :uid"), {"uid": user_id}
        ).fetchone()
        if row:
            return row.persona
        row = conn.execute(
            text("SELECT persona FROM persona_prefs WHERE user_id = :uid"), {"uid": user_id}
        ).fetchone()
    return row.persona if row else "default"


def add_points(user_id: str, amount: int, reason: str, ref_id: Optional[str] = None) -> int:
    """原子加/扣分，返回变动后的余额。amount 正数为收入，负数为支出。"""
    from datetime import datetime
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        from backports.zoneinfo import ZoneInfo
    TZ = ZoneInfo("Asia/Shanghai")
    now = datetime.now(TZ).isoformat()

    with engine.connect() as conn:
        # 先确保 users 表的 points 列和行存在（兼容 IM 用户）
        existing = conn.execute(
            text("SELECT points FROM users WHERE id = :uid"), {"uid": user_id}
        ).fetchone()
        if existing is None:
            import uuid
            placeholder_email = f"auto:{user_id}@placeholder.local"
            conn.execute(
                text("INSERT INTO users (id, email, password_hash, token, created_at, persona, points) "
                     "VALUES (:uid, :email, '', :token, :now, 'default', 0)"),
                {"uid": user_id, "email": placeholder_email, "token": str(uuid.uuid4()), "now": now},
            )
        conn.execute(
            text("UPDATE users SET points = points + :amt WHERE id = :uid"),
            {"amt": amount, "uid": user_id},
        )
        conn.execute(
            text("INSERT INTO points_ledger (user_id, amount, reason, ref_id, created_at) "
                 "VALUES (:uid, :amt, :reason, :ref, :now)"),
            {"uid": user_id, "amt": amount, "reason": reason, "ref": ref_id, "now": now},
        )
        conn.commit()
        row = conn.execute(
            text("SELECT points FROM users WHERE id = :uid"), {"uid": user_id}
        ).fetchone()
    return row.points if row else 0


def get_user_points(user_id: str) -> dict:
    """返回用户积分摘要（显示单位）"""
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT points FROM users WHERE id = :uid"), {"uid": user_id}
        ).fetchone()
        internal = row.points if row else 0
        # 今日收入
        from datetime import datetime
        try:
            from zoneinfo import ZoneInfo
        except ImportError:
            from backports.zoneinfo import ZoneInfo
        TZ = ZoneInfo("Asia/Shanghai")
        today = datetime.now(TZ).strftime("%Y-%m-%d")
        row2 = conn.execute(
            text("SELECT COALESCE(SUM(amount), 0) AS earned FROM points_ledger "
                 "WHERE user_id = :uid AND amount > 0 AND date(created_at) = :today"),
            {"uid": user_id, "today": today},
        ).fetchone()
        internal_earned = row2.earned if row2 else 0
    return {"balance": round(internal / 10, 1), "today_earned": round(internal_earned / 10, 1)}


def try_daily_bonus(user_id: str) -> dict:
    """每日首次活跃送积分。返回 {credited: bool, balance: float, today_earned: float}（显示单位）"""
    from datetime import datetime
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        from backports.zoneinfo import ZoneInfo
    TZ = ZoneInfo("Asia/Shanghai")
    today = datetime.now(TZ).strftime("%Y-%m-%d")

    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT id FROM points_ledger "
                 "WHERE user_id = :uid AND reason = 'daily_login' AND date(created_at) = :today "
                 "LIMIT 1"),
            {"uid": user_id, "today": today},
        ).fetchone()
        if row:
            pts = conn.execute(
                text("SELECT points FROM users WHERE id = :uid"), {"uid": user_id}
            ).fetchone()
            pts_earned = conn.execute(
                text("SELECT COALESCE(SUM(amount), 0) AS earned FROM points_ledger "
                     "WHERE user_id = :uid AND amount > 0 AND date(created_at) = :today"),
                {"uid": user_id, "today": today},
            ).fetchone()
            internal = pts.points if pts else 0
            internal_earned = pts_earned.earned if pts_earned else 0
            return {"credited": False, "balance": round(internal / 10, 1),
                    "today_earned": round(internal_earned / 10, 1)}

    # 未签到，加 50 内部单位 (= 5.0 显示积分)
    balance_internal = add_points(user_id, 50, "daily_login")
    return {"credited": True, "balance": round(balance_internal / 10, 1), "today_earned": 5.0}
