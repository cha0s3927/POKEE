"""
Tool Executor — LLM Agent 可调用的工具函数
"""
import json
import uuid
from datetime import datetime
try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.orm import Session

from config import settings
from database import engine

TZ = ZoneInfo(settings.tz_name)


def now_iso():
    return datetime.now(TZ).isoformat()


def execute_tool(name: str, args: dict) -> dict:
    user_id = args.get("user_id", "default")

    if name == "get_current_time":
        now = datetime.now(TZ)
        return {"current_time": now.isoformat(), "timestamp": int(now.timestamp()),
                "timezone": settings.tz_name}

    elif name == "create_reminder":
        rid = str(uuid.uuid4())[:8]
        task = args["task"]
        run_at = args["run_at"]
        created = now_iso()
        with Session(engine) as session:
            session.execute(
                text("INSERT INTO reminders (id, user_id, task, run_at, status, created_at) "
                     "VALUES (:id, :uid, :task, :run_at, 'pending', :created_at)"),
                {"id": rid, "uid": user_id, "task": task, "run_at": run_at, "created_at": created},
            )
            session.commit()
        # deferred import 避免循环依赖
        from scheduler import add_reminder_job
        add_reminder_job(rid, run_at)
        from database import add_points
        add_points(user_id, -1, "create_reminder", rid)
        return {"id": rid, "status": "pending", "run_at": run_at}

    elif name == "list_reminders":
        with Session(engine) as session:
            st = args.get("status")
            if st:
                rows = session.execute(
                    text("SELECT * FROM reminders WHERE user_id=:uid AND status=:st ORDER BY run_at DESC"),
                    {"uid": user_id, "st": st},
                ).fetchall()
            else:
                rows = session.execute(
                    text("SELECT * FROM reminders WHERE user_id=:uid ORDER BY run_at DESC"),
                    {"uid": user_id},
                ).fetchall()
        return [dict(r._mapping) for r in rows]

    elif name == "get_reminder":
        rid = args["reminder_id"]
        with Session(engine) as session:
            row = session.execute(
                text("SELECT * FROM reminders WHERE id=:id AND user_id=:uid"),
                {"id": rid, "uid": user_id},
            ).fetchone()
        if not row:
            return {"error": "not_found", "message": f"提醒 {rid} 不存在"}
        return dict(row._mapping)

    elif name == "cancel_reminder":
        rid = args["reminder_id"]
        with Session(engine) as session:
            row = session.execute(
                text("SELECT * FROM reminders WHERE id=:id AND user_id=:uid"),
                {"id": rid, "uid": user_id},
            ).fetchone()
            if not row:
                return {"error": "not_found", "message": f"提醒 {rid} 不存在"}
            session.execute(
                text("UPDATE reminders SET status='cancelled' WHERE id=:id"), {"id": rid}
            )
            session.commit()
        # deferred import 避免循环依赖
        from scheduler import scheduler
        try:
            scheduler.remove_job(f"job_{rid}")
        except Exception:
            pass
        return {"id": rid, "status": "cancelled"}

    elif name == "get_notifications":
        with Session(engine) as session:
            rows = session.execute(
                text("SELECT * FROM reminders WHERE user_id=:uid AND status='sent' AND seen_at IS NULL ORDER BY sent_at"),
                {"uid": user_id},
            ).fetchall()
        return [dict(r._mapping) for r in rows]

    elif name == "ack_notifications":
        with Session(engine) as session:
            result = session.execute(
                text("UPDATE reminders SET seen_at=:t WHERE user_id=:uid AND status='sent' AND seen_at IS NULL"),
                {"t": now_iso(), "uid": user_id},
            )
            session.commit()
        return {"acked": result.rowcount}

    elif name == "set_persona":
        persona = args["persona"]
        with Session(engine) as session:
            session.execute(
                text("INSERT OR REPLACE INTO persona_prefs (user_id, persona) VALUES (:uid, :p)"),
                {"uid": user_id, "p": persona},
            )
            session.commit()
        return {"status": "ok", "persona": persona}

    elif name == "get_balance":
        from database import get_user_points
        return get_user_points(user_id)

    return {"error": "unknown_tool", "message": f"未知工具: {name}"}
