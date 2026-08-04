"""
调度器 — APScheduler + fire_reminder + SSE 推送
"""
from __future__ import annotations

import asyncio
import json
import urllib.request
import uuid
from datetime import datetime
from typing import Any

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import text
from sqlalchemy.orm import Session

from config import settings
from database import engine

TZ_NAME = settings.tz_name
try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo
TZ = ZoneInfo(TZ_NAME)


def now_iso():
    return datetime.now(TZ).isoformat()


# ── SSE clients ──
sse_clients: dict[str, asyncio.Queue] = {}
_main_loop: asyncio.AbstractEventLoop | None = None


def init_sse(loop: asyncio.AbstractEventLoop):
    global _main_loop
    _main_loop = loop


def push_sse(data: dict):
    loop = _main_loop
    if loop is None:
        return
    user_id = data.get("user_id", "")
    if user_id not in sse_clients:
        return
    q = sse_clients[user_id]
    try:
        asyncio.run_coroutine_threadsafe(_sse_put(q, data), loop)
    except Exception:
        pass


async def _sse_put(q: asyncio.Queue, data: dict):
    try:
        q.put_nowait(data)
    except asyncio.QueueFull:
        pass


# ── 调度器 ──
jobstores = {"default": SQLAlchemyJobStore(url=settings.database_url)}
executors = {"default": ThreadPoolExecutor(5)}
job_defaults = {"coalesce": True, "max_instances": 1}

scheduler = BackgroundScheduler(
    jobstores=jobstores, executors=executors, job_defaults=job_defaults,
    timezone=TZ,
)


def fire_reminder(reminder_id: str):
    with Session(engine) as session:
        row = session.execute(
            text("SELECT * FROM reminders WHERE id = :id"), {"id": reminder_id}
        ).fetchone()
    if not row:
        return
    reminder = dict(row._mapping)
    task = reminder["task"]
    run_at = reminder["run_at"]
    user_id = reminder["user_id"]

    with Session(engine) as session:
        session.execute(
            text("UPDATE reminders SET status = 'sent', sent_at = :t WHERE id = :id"),
            {"id": reminder_id, "t": now_iso()},
        )
        session.commit()

    try:
        print(f"[SCHEDULER] fire: {reminder_id} for user={user_id}")
    except Exception:
        pass
    payload: dict[str, Any] = {
        "type": "notification",
        "id": reminder_id,
        "task": task,
        "user_id": user_id,
        "run_at": run_at,
        "sent_at": now_iso(),
    }
    push_sse(payload)

    # 查询 user_im_bindings
    bindings = []
    with Session(engine) as session:
        rows = session.execute(
            text("SELECT platform, im_user_id FROM user_im_bindings WHERE user_id = :uid"),
            {"uid": user_id},
        ).fetchall()
        bindings = [(r.platform, r.im_user_id) for r in rows]

    # Fallback: user_id 本身是 platform:xxx 格式
    if not bindings:
        for prefix, platform in [("feishu:", "feishu"), ("wechat:", "wechat"), ("whatsapp:", "whatsapp"), ("linkedin:", "linkedin")]:
            if user_id.startswith(prefix):
                bindings.append((platform, user_id[len(prefix):]))
                break

    # 通过 adapter registry 推送（deferred import 避免循环依赖）
    from adapters import adapter_registry
    for platform, im_uid in bindings:
        adapter = adapter_registry.get(platform)
        if adapter:
            try:
                adapter.send_notification(im_uid, task, run_at, user_id)
            except Exception as e:
                print(f"[SCHEDULER] push error ({platform}): {e}")
        else:
            # Fallback HTTP push for platforms without an adapter loaded
            _fallback_push(platform, im_uid, task, run_at, user_id, payload)


def _fallback_push(platform: str, im_uid: str, task: str, run_at: str, user_id: str, payload: dict):
    """HTTP fallback 推送 — 用于 WeChat/WhatsApp 未走 adapter 的情况"""
    if platform == "wechat":
        try:
            req = urllib.request.Request(
                settings.wechat_push_url,
                data=json.dumps({**payload, "user_id": im_uid}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=5)
        except Exception as e:
            print(f"[WECHAT-PUSH] failed: {e}")

    elif platform == "whatsapp":
        try:
            req = urllib.request.Request(
                settings.whatsapp_push_url,
                data=json.dumps({**payload, "user_id": im_uid, "web_user_id": user_id}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=5)
        except Exception as e:
            print(f"[WHATSAPP-PUSH] failed: {e}")


def restore_jobs():
    with Session(engine) as session:
        rows = session.execute(
            text("SELECT * FROM reminders WHERE status = 'pending' AND run_at > :now"),
            {"now": now_iso()},
        ).fetchall()
    for row in rows:
        r = dict(row._mapping)
        run_date = datetime.fromisoformat(r["run_at"]).replace(tzinfo=TZ)
        scheduler.add_job(
            fire_reminder, trigger="date", run_date=run_date,
            args=[r["id"]], id=f"job_{r['id']}", replace_existing=True,
        )
    return len(rows)


def add_reminder_job(reminder_id: str, run_at: str):
    run_date = datetime.fromisoformat(run_at).replace(tzinfo=TZ)
    scheduler.add_job(fire_reminder, trigger="date", run_date=run_date,
                      args=[reminder_id], id=f"job_{reminder_id}")
