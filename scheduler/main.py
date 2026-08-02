"""
Reminder Agent — 定时提醒助手
FastAPI + APScheduler + LLM Agent + Email Auth + Web Chat UI
"""
import asyncio
import base64
import io
import json
import os
import random
import re
import socket
import string
import time
import urllib.request
import uuid
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# 自动加载 .env（优先找当前目录，再找上级目录）
from dotenv import load_dotenv
for _env_dir in (Path(__file__).parent, Path(__file__).parent.parent):
    _env_path = _env_dir / ".env"
    if _env_path.exists():
        load_dotenv(_env_path)

import yaml
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, HTTPException, Query, Path as PathParam, Request, Depends, Header
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from starlette.responses import Response

from agent import Agent
from auth import hash_password, verify_password, new_token, validate_email, get_current_user, now_iso as auth_now
from channels.feishu import FeishuBot
from channels.linkedin import LinkedInBot

TZ = ZoneInfo("Asia/Shanghai")


def now_iso():
    return datetime.now(TZ).isoformat()


# ── 配置 ────────────────────────────────────────────
config_path = Path(__file__).parent / "config.yaml"
with open(config_path, encoding="utf-8") as f:
    raw = f.read()
raw = re.sub(r'\$\{(\w+):-([^}]*)\}', lambda m: os.environ.get(m.group(1), m.group(2)), raw)
raw = re.sub(r'\$\{(\w+)\}', lambda m: os.environ.get(m.group(1), ''), raw)
config = yaml.safe_load(raw)

DATABASE_URL = config.get("database", {}).get("url", "sqlite:///data/reminders.db")
SERVER_URL = config.get("server_url", "http://localhost:8000")

LLM_CONFIG = config.get("llm", {})
LLM_API_KEY = LLM_CONFIG.get("api_key", "")
LLM_BASE_URL = LLM_CONFIG.get("base_url", "https://api.deepseek.com/v1")
LLM_MODEL = LLM_CONFIG.get("model", "deepseek-chat")

WECHAT_SECRET = config.get("wechat", {}).get("secret", os.environ.get("WECHAT_SECRET", "wechat-secret-change-me"))
WECHAT_PUSH_URL = config.get("wechat", {}).get("push_url", os.environ.get("WECHAT_PUSH_URL", "http://localhost:8765/push"))

FEISHU_CONFIG = config.get("feishu", {})
FEISHU_APP_ID = FEISHU_CONFIG.get("app_id", os.environ.get("FEISHU_APP_ID", ""))
FEISHU_APP_SECRET = FEISHU_CONFIG.get("app_secret", os.environ.get("FEISHU_APP_SECRET", ""))
feishu_bot: FeishuBot | None = None

WHATSAPP_SECRET = config.get("whatsapp", {}).get("secret", os.environ.get("WHATSAPP_SECRET", "whatsapp-secret-change-me"))
WHATSAPP_PUSH_URL = config.get("whatsapp", {}).get("push_url", os.environ.get("WHATSAPP_PUSH_URL", "http://localhost:8767/push"))

LINKEDIN_CONFIG = config.get("linkedin", {})
LINKEDIN_EMAIL = LINKEDIN_CONFIG.get("email", os.environ.get("LINKEDIN_EMAIL", ""))
LINKEDIN_PASSWORD = LINKEDIN_CONFIG.get("password", os.environ.get("LINKEDIN_PASSWORD", ""))
LINKEDIN_SECRET = LINKEDIN_CONFIG.get("secret", os.environ.get("LINKEDIN_SECRET", "reminder-agent-linkedin-2026"))
LINKEDIN_LI_AT = LINKEDIN_CONFIG.get("li_at", os.environ.get("LINKEDIN_LI_AT", ""))
LINKEDIN_JSESSIONID = LINKEDIN_CONFIG.get("jsessionid", os.environ.get("LINKEDIN_JSESSIONID", ""))
linkedin_bot: LinkedInBot | None = None

# ── 数据库 ────────────────────────────────────────────
Path("data").mkdir(exist_ok=True)
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})

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
    conn.commit()

# ── 配对码（飞书 per-user 绑定）──────────────────────
_pairing_codes: dict[str, dict] = {}  # {code: {user_id, expires_at}}


def generate_pairing_code() -> str:
    """生成 6 位随机配对码，清理过期码"""
    now = time.time()
    expired = [c for c, v in _pairing_codes.items() if v["expires"] < now]
    for c in expired:
        del _pairing_codes[c]
    code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
    _pairing_codes[code] = {"user_id": "", "expires": now + 300}
    return code


def consume_pairing_code(code: str, open_id: str) -> str | None:
    """消费配对码，返回 user_id 或 None"""
    now = time.time()
    entry = _pairing_codes.get(code)
    if not entry or entry["expires"] < now:
        if entry:
            del _pairing_codes[code]
        return None
    user_id = entry["user_id"]
    del _pairing_codes[code]
    return user_id


# ── 待绑定（WeChat/WhatsApp auto-bind）───────────────
_pending_bindings: dict[str, dict] = {}  # {user_id: {platform, expires}}


def set_pending_binding(user_id: str, platform: str):
    """标记用户正在等待 IM 绑定"""
    _pending_bindings[user_id] = {"platform": platform, "expires": time.time() + 300}


def try_auto_bind(platform: str, im_user_id: str) -> str | None:
    """尝试自动绑定 IM 身份到等待中的 web 用户，返回 web_user_id 或 None"""
    now = time.time()
    # 先清理过期条目
    expired_users = [uid for uid, v in _pending_bindings.items() if v["expires"] < now]
    for uid in expired_users:
        del _pending_bindings[uid]
    # 找该平台最近一个 pending 用户
    candidates = [(uid, v) for uid, v in _pending_bindings.items() if v["platform"] == platform]
    if not candidates:
        return None
    # 取最早的那个
    candidates.sort(key=lambda x: x[1]["expires"])
    user_id = candidates[0][0]
    del _pending_bindings[user_id]
    # 写入绑定
    with Session(engine) as session:
        session.execute(
            text("INSERT OR IGNORE INTO user_im_bindings (id, user_id, platform, im_user_id, created_at) "
                 "VALUES (:id, :uid, :platform, :imuid, :now)"),
            {"id": str(uuid.uuid4())[:12], "uid": user_id, "platform": platform,
             "imuid": im_user_id, "now": now_iso()},
        )
        session.commit()
    print(f"[AUTO-BIND] {platform}:{im_user_id} -> user_id={user_id}")
    return user_id

# ── 调度器 ────────────────────────────────────────────
jobstores = {"default": SQLAlchemyJobStore(url=DATABASE_URL)}
executors = {"default": ThreadPoolExecutor(5)}
job_defaults = {"coalesce": True, "max_instances": 1}

scheduler = BackgroundScheduler(
    jobstores=jobstores, executors=executors, job_defaults=job_defaults,
    timezone=TZ,
)

# SSE 推送 — 每个用户只保留一条连接
sse_clients: dict[str, asyncio.Queue] = {}
_main_loop: asyncio.AbstractEventLoop | None = None


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

    print(f"[SCHEDULER] fire: {reminder_id} '{task}' for user={user_id}")
    payload = {
        "type": "notification",
        "id": reminder_id,
        "task": task,
        "user_id": user_id,
        "run_at": run_at,
        "sent_at": now_iso(),
    }
    push_sse(payload)

    # 查询 user_im_bindings，推送到所有已绑定的 IM 平台
    bindings = []
    with Session(engine) as session:
        rows = session.execute(
            text("SELECT platform, im_user_id FROM user_im_bindings WHERE user_id = :uid"),
            {"uid": user_id},
        ).fetchall()
        bindings = [(r.platform, r.im_user_id) for r in rows]

    # 兼容 user_id 本身就是 platform:xxx 格式（未绑定或旧数据）
    if not bindings:
        for prefix, platform in [("feishu:", "feishu"), ("wechat:", "wechat"), ("whatsapp:", "whatsapp"), ("linkedin:", "linkedin")]:
            if user_id.startswith(prefix):
                bindings.append((platform, user_id[len(prefix):]))
                break

    for platform, im_uid in bindings:
        if platform == "feishu" and feishu_bot:
            feishu_bot.send_notification(im_uid, task, run_at)

        elif platform == "wechat":
            try:
                import urllib.request
                req = urllib.request.Request(
                    WECHAT_PUSH_URL,
                    data=json.dumps({**payload, "user_id": im_uid}).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                urllib.request.urlopen(req, timeout=5)
            except Exception as e:
                print(f"[WECHAT-PUSH] failed: {e}")

        elif platform == "whatsapp":
            try:
                import urllib.request
                req = urllib.request.Request(
                    WHATSAPP_PUSH_URL,
                    data=json.dumps({**payload, "user_id": im_uid}).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                urllib.request.urlopen(req, timeout=5)
            except Exception as e:
                print(f"[WHATSAPP-PUSH] failed: {e}")

        elif platform == "linkedin" and linkedin_bot:
            linkedin_bot.send_notification(im_uid, task, run_at)


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


# ── Tool Executor ───────────────────────────────────
def execute_tool(name: str, args: dict) -> dict:
    user_id = args.get("user_id", "default")

    if name == "get_current_time":
        now = datetime.now(TZ)
        return {"current_time": now.isoformat(), "timestamp": int(now.timestamp()),
                "timezone": "Asia/Shanghai"}

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
        run_date = datetime.fromisoformat(run_at).replace(tzinfo=TZ)
        scheduler.add_job(fire_reminder, trigger="date", run_date=run_date,
                          args=[rid], id=f"job_{rid}")
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

    return {"error": "unknown_tool", "message": f"未知工具: {name}"}


# ── Agent ───────────────────────────────────────────
agent = Agent(
    api_key=LLM_API_KEY,
    base_url=LLM_BASE_URL,
    model=LLM_MODEL,
    tool_executor=execute_tool,
)

# ── FastAPI ─────────────────────────────────────────
app = FastAPI(
    title="Reminder Agent",
    version="3.1.0",
    description="自然语言定时提醒助手 — Email Auth + LLM Agent + APScheduler",
)


@app.on_event("startup")
def startup():
    global _main_loop, feishu_bot, linkedin_bot
    _main_loop = asyncio.get_running_loop()
    scheduler.start()
    n = restore_jobs()
    print(f"[STARTUP] {n} pending reminders restored")

    if FEISHU_APP_ID and FEISHU_APP_SECRET:

        def handle_feishu_pairing(msg_text: str, open_id: str) -> bool:
            """检查并消费配对码，创建 user↔飞书 绑定"""
            code = msg_text.strip().upper()
            print(f"[FEISHU-PAIR] checking code='{code}' against {len(_pairing_codes)} active codes: {list(_pairing_codes.keys())}")
            user_id = consume_pairing_code(code, open_id)
            print(f"[FEISHU-PAIR] consume result: user_id={user_id}")
            if not user_id:
                return False
            with Session(engine) as session:
                session.execute(
                    text("INSERT OR IGNORE INTO user_im_bindings (id, user_id, platform, im_user_id, created_at) "
                         "VALUES (:id, :uid, :platform, :imuid, :now)"),
                    {"id": str(uuid.uuid4())[:12], "uid": user_id, "platform": "feishu",
                     "imuid": open_id, "now": now_iso()},
                )
                session.commit()
            print(f"[FEISHU] bound open_id={open_id} to user_id={user_id}")
            return True

        def resolve_feishu_user(open_id: str) -> str:
            """根据 open_id 查找绑定的 web 用户，未绑定则返回 feishu:xxx"""
            with Session(engine) as session:
                row = session.execute(
                    text("SELECT user_id FROM user_im_bindings WHERE platform='feishu' AND im_user_id=:imuid"),
                    {"imuid": open_id},
                ).fetchone()
            if row:
                return row.user_id
            return f"feishu:{open_id}"

        feishu_bot = FeishuBot(
            FEISHU_APP_ID, FEISHU_APP_SECRET, agent, execute_tool,
            pairing_handler=handle_feishu_pairing,
            resolve_user=resolve_feishu_user,
        )
        feishu_bot.start()
        print("[STARTUP] Feishu bot started")

    if LINKEDIN_EMAIL and LINKEDIN_PASSWORD:
        cookies = None
        if LINKEDIN_LI_AT:
            cookies = {"li_at": LINKEDIN_LI_AT, "JSESSIONID": LINKEDIN_JSESSIONID}
        linkedin_bot = LinkedInBot(LINKEDIN_EMAIL, LINKEDIN_PASSWORD, agent, execute_tool, cookies=cookies)
        linkedin_bot.start()
        print("[STARTUP] LinkedIn bot started")


@app.on_event("shutdown")
def shutdown():
    scheduler.shutdown()


# ── Rate limiter ────────────────────────────────────
_rate_store: dict[str, list[float]] = defaultdict(list)


def check_rate(key: str, max_req: int = 10, window: int = 60) -> bool:
    """滑动窗口限流，默认每分钟 10 次。"""
    now = time.time()
    cutoff = now - window
    _rate_store[key] = [t for t in _rate_store[key] if t > cutoff]
    if len(_rate_store[key]) >= max_req:
        return False
    _rate_store[key].append(now)
    return True


# ── Auth dependency ─────────────────────────────────
def auth_user(authorization: str | None = Header(default=None)) -> dict:
    return get_current_user(engine, authorization)


# ── Auth endpoints ──────────────────────────────────
class RegisterRequest(BaseModel):
    email: str = Field(description="邮箱地址")
    password: str = Field(description="密码，至少6位")


class LoginRequest(BaseModel):
    email: str = Field(description="邮箱地址")
    password: str = Field(description="密码")


@app.post("/api/register", summary="注册")
def register(req: RegisterRequest, request: Request):
    if not check_rate(request.client.host, max_req=5, window=60):
        raise HTTPException(status_code=429, detail="请求太频繁，请稍后再试")
    if not validate_email(req.email):
        raise HTTPException(status_code=400, detail="邮箱格式不正确")
    if len(req.password) < 6:
        raise HTTPException(status_code=400, detail="密码至少6位")
    with engine.connect() as conn:
        existing = conn.execute(
            text("SELECT id FROM users WHERE email = :e"), {"e": req.email}
        ).fetchone()
        if existing:
            raise HTTPException(status_code=409, detail="该邮箱已注册")
        uid = str(uuid.uuid4())[:12]
        token = new_token()
        conn.execute(
            text("INSERT INTO users (id, email, password_hash, token, created_at) "
                 "VALUES (:id, :email, :ph, :token, :now)"),
            {"id": uid, "email": req.email, "ph": hash_password(req.password),
             "token": token, "now": auth_now()},
        )
        conn.commit()
    return {"token": token, "user_id": uid, "email": req.email}


@app.post("/api/login", summary="登录")
def login(req: LoginRequest, request: Request):
    if not check_rate(request.client.host, max_req=5, window=60):
        raise HTTPException(status_code=429, detail="请求太频繁，请稍后再试")
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT * FROM users WHERE email = :e"), {"e": req.email}
        ).fetchone()
    if not row:
        raise HTTPException(status_code=401, detail="邮箱或密码错误")
    user = dict(row._mapping)
    if not verify_password(req.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="邮箱或密码错误")
    with engine.connect() as conn:
        conn.execute(
            text("UPDATE users SET last_login_at = :now WHERE id = :id"),
            {"now": auth_now(), "id": user["id"]},
        )
        conn.commit()
    return {"token": user["token"], "user_id": user["id"], "email": user["email"]}


# ── Chat API ─────────────────────────────────────────
class ChatRequest(BaseModel):
    message: str = Field(description="用户消息")


@app.post("/api/chat", summary="Agent 对话")
def chat(req: ChatRequest, user: dict = Depends(auth_user)):
    if not check_rate(user["id"], max_req=20):
        raise HTTPException(status_code=429, detail="请求太频繁，请稍后再试")
    reply = agent.chat(user_message=req.message, user_id=user["id"])
    execute_tool("ack_notifications", {"user_id": user["id"]})
    return {"reply": reply}


@app.post("/api/reset", summary="重置会话")
def reset_session(user: dict = Depends(auth_user)):
    agent.clear_session(user["id"])
    return {"status": "ok"}


# ── WeChat 接入 ───────────────────────────────────────
class WechatChatRequest(BaseModel):
    message: str = Field(description="用户消息")
    conversation_id: str = Field(description="微信会话 ID，用作 user_id")
    secret: str = Field(description="共享密钥，验证请求来源")


@app.post("/api/wechat/chat", summary="微信 Agent 对话")
def wechat_chat(req: WechatChatRequest):
    if req.secret != WECHAT_SECRET:
        raise HTTPException(status_code=403, detail="密钥错误")
    im_id = req.conversation_id
    # 查绑定
    with Session(engine) as session:
        row = session.execute(
            text("SELECT user_id FROM user_im_bindings WHERE platform='wechat' AND im_user_id=:imuid"),
            {"imuid": im_id},
        ).fetchone()
    if row:
        user_id = row.user_id
    else:
        bound = try_auto_bind("wechat", im_id)
        user_id = bound or f"wechat:{im_id}"
    reply = agent.chat(user_message=req.message, user_id=user_id)
    execute_tool("ack_notifications", {"user_id": user_id})
    return {"reply": reply}


class WechatResetRequest(BaseModel):
    conversation_id: str = Field(description="微信会话 ID")
    secret: str = Field(description="共享密钥")


@app.post("/api/wechat/reset", summary="重置微信会话")
def wechat_reset(req: WechatResetRequest):
    if req.secret != WECHAT_SECRET:
        raise HTTPException(status_code=403, detail="密钥错误")
    with Session(engine) as session:
        row = session.execute(
            text("SELECT user_id FROM user_im_bindings WHERE platform='wechat' AND im_user_id=:imuid"),
            {"imuid": req.conversation_id},
        ).fetchone()
    user_id = row.user_id if row else f"wechat:{req.conversation_id}"
    agent.clear_session(user_id)
    return {"status": "ok"}


# ── WhatsApp 接入 ─────────────────────────────────────
class WhatsappChatRequest(BaseModel):
    message: str = Field(description="用户消息")
    conversation_id: str = Field(description="WhatsApp JID，用作 user_id")
    secret: str = Field(description="共享密钥，验证请求来源")


@app.post("/api/whatsapp/chat", summary="WhatsApp Agent 对话")
def whatsapp_chat(req: WhatsappChatRequest):
    if req.secret != WHATSAPP_SECRET:
        raise HTTPException(status_code=403, detail="密钥错误")
    im_id = req.conversation_id
    # 查绑定
    with Session(engine) as session:
        row = session.execute(
            text("SELECT user_id FROM user_im_bindings WHERE platform='whatsapp' AND im_user_id=:imuid"),
            {"imuid": im_id},
        ).fetchone()
    if row:
        user_id = row.user_id
    else:
        bound = try_auto_bind("whatsapp", im_id)
        user_id = bound or f"whatsapp:{im_id}"
    reply = agent.chat(user_message=req.message, user_id=user_id)
    execute_tool("ack_notifications", {"user_id": user_id})
    return {"reply": reply}


class WhatsappResetRequest(BaseModel):
    conversation_id: str = Field(description="WhatsApp JID")
    secret: str = Field(description="共享密钥")


@app.post("/api/whatsapp/reset", summary="重置 WhatsApp 会话")
def whatsapp_reset(req: WhatsappResetRequest):
    if req.secret != WHATSAPP_SECRET:
        raise HTTPException(status_code=403, detail="密钥错误")
    with Session(engine) as session:
        row = session.execute(
            text("SELECT user_id FROM user_im_bindings WHERE platform='whatsapp' AND im_user_id=:imuid"),
            {"imuid": req.conversation_id},
        ).fetchone()
    user_id = row.user_id if row else f"whatsapp:{req.conversation_id}"
    agent.clear_session(user_id)
    return {"status": "ok"}


# ── LinkedIn 接入 ─────────────────────────────────────
class LinkedinChatRequest(BaseModel):
    message: str = Field(description="用户消息")
    conversation_id: str = Field(description="LinkedIn conversation URN，用作 user_id")
    secret: str = Field(description="共享密钥，验证请求来源")


@app.post("/api/linkedin/chat", summary="LinkedIn Agent 对话")
def linkedin_chat(req: LinkedinChatRequest):
    if req.secret != LINKEDIN_SECRET:
        raise HTTPException(status_code=403, detail="密钥错误")
    user_id = f"linkedin:{req.conversation_id}"
    reply = agent.chat(user_message=req.message, user_id=user_id)
    execute_tool("ack_notifications", {"user_id": user_id})
    # 如果 linkedin_bot 在运行，发送回复
    if linkedin_bot and linkedin_bot.api:
        linkedin_bot.api.send_message(reply, conversation_urn_id=req.conversation_id)
    return {"reply": reply}


class LinkedinResetRequest(BaseModel):
    conversation_id: str = Field(description="LinkedIn conversation URN")
    secret: str = Field(description="共享密钥")


@app.post("/api/linkedin/reset", summary="重置 LinkedIn 会话")
def linkedin_reset(req: LinkedinResetRequest):
    if req.secret != LINKEDIN_SECRET:
        raise HTTPException(status_code=403, detail="密钥错误")
    user_id = f"linkedin:{req.conversation_id}"
    agent.clear_session(user_id)
    return {"status": "ok"}


# ── REST API（需认证）────────────────────────────────
@app.get("/reminders", summary="查询提醒列表")
def list_reminders(
    user: dict = Depends(auth_user),
    status: str | None = Query(default=None),
):
    return execute_tool("list_reminders", {"user_id": user["id"], "status": status})


@app.get("/reminders/{reminder_id}", summary="查看单个提醒")
def get_reminder(reminder_id: str, user: dict = Depends(auth_user)):
    result = execute_tool("get_reminder", {"reminder_id": reminder_id, "user_id": user["id"]})
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["message"])
    return result


@app.delete("/reminders/{reminder_id}", summary="取消提醒")
def cancel_reminder(reminder_id: str, user: dict = Depends(auth_user)):
    result = execute_tool("cancel_reminder", {"reminder_id": reminder_id, "user_id": user["id"]})
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["message"])
    return result


@app.get("/notifications", summary="获取未读通知")
def get_notifications(user: dict = Depends(auth_user)):
    return execute_tool("get_notifications", {"user_id": user["id"]})


@app.post("/notifications/ack", summary="标记通知已读")
def ack_notifications(user: dict = Depends(auth_user)):
    return execute_tool("ack_notifications", {"user_id": user["id"]})


@app.get("/now", summary="获取当前时间")
def get_current_time():
    return execute_tool("get_current_time", {})


def _tcp_ping(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        s = socket.create_connection((host, port), timeout=timeout)
        s.close()
        return True
    except Exception:
        return False


@app.get("/api/platforms/status", summary="IM 平台连接状态（per-user）")
def platforms_status(user: dict = Depends(auth_user)):
    web_user_id = user["id"]
    with Session(engine) as session:
        rows = session.execute(
            text("SELECT platform FROM user_im_bindings WHERE user_id = :uid"),
            {"uid": web_user_id},
        ).fetchall()
    bindings = {r.platform for r in rows}
    return {
        "web": {"connected": True, "label": "Web", "has_qr": False},
        "wechat": {
            "connected": "wechat" in bindings,
            "label": "微信",
            "how": "在微信中给 Bot 发消息即可使用",
            "has_qr": True,
        },
        "feishu": {
            "connected": "feishu" in bindings,
            "label": "飞书",
            "how": "在飞书中与 Bot 对话即可使用",
            "has_qr": True,
        },
        "whatsapp": {
            "connected": "whatsapp" in bindings,
            "label": "WhatsApp",
            "how": "在 WhatsApp 中给自己发消息即可使用",
            "has_qr": True,
        },
    }


def _qr_image(text: str) -> str:
    """生成 QR 码图片，返回 base64 data URL"""
    import qrcode
    img = qrcode.make(text, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


@app.get("/api/platforms/qr/wechat", summary="微信登录二维码")
def qr_wechat(user: dict = Depends(auth_user)):
    web_user_id = user["id"]
    with Session(engine) as session:
        row = session.execute(
            text("SELECT im_user_id FROM user_im_bindings WHERE user_id=:uid AND platform='wechat'"),
            {"uid": web_user_id},
        ).fetchone()
    if row:
        return {"connected": True, "qr_image": ""}
    set_pending_binding(web_user_id, "wechat")
    try:
        req = urllib.request.Request("http://127.0.0.1:8765/qr")
        resp = urllib.request.urlopen(req, timeout=3)
        data = json.loads(resp.read())
        qr_url = data.get("qr_url", "")
        return {"connected": False, "qr_image": _qr_image(qr_url) if qr_url else "", "qr_url": qr_url}
    except Exception:
        return {"connected": False, "error": "adapter not reachable"}


@app.get("/api/platforms/qr/whatsapp", summary="WhatsApp 登录二维码")
def qr_whatsapp(user: dict = Depends(auth_user)):
    web_user_id = user["id"]
    with Session(engine) as session:
        row = session.execute(
            text("SELECT im_user_id FROM user_im_bindings WHERE user_id=:uid AND platform='whatsapp'"),
            {"uid": web_user_id},
        ).fetchone()
    if row:
        return {"connected": True, "qr_image": ""}
    set_pending_binding(web_user_id, "whatsapp")
    try:
        req = urllib.request.Request("http://127.0.0.1:8767/qr")
        resp = urllib.request.urlopen(req, timeout=3)
        data = json.loads(resp.read())
        qr_text = data.get("qr", "")
        pairing_code = data.get("pairing_code", "")
        result = {"connected": False, "qr": qr_text}
        if pairing_code:
            result["pairing_code"] = pairing_code
        elif qr_text:
            result["qr_image"] = _qr_image(qr_text)
        return result
    except Exception:
        return {"connected": False, "error": "adapter not reachable"}


@app.post("/api/platforms/qr/whatsapp/pairing", summary="请求 WhatsApp 配对码")
def whatsapp_pairing(user: dict = Depends(auth_user), body: dict = None):
    try:
        data = json.dumps(body or {}).encode()
        req = urllib.request.Request("http://127.0.0.1:8767/pairing", data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        resp = urllib.request.urlopen(req, timeout=10)
        return json.loads(resp.read())
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/platforms/qr/feishu", summary="飞书 Bot 连接（per-user）")
def qr_feishu(user: dict = Depends(auth_user)):
    if not FEISHU_APP_ID:
        return {"error": "feishu not configured"}
    web_user_id = user["id"]
    with Session(engine) as session:
        row = session.execute(
            text("SELECT im_user_id FROM user_im_bindings WHERE user_id=:uid AND platform='feishu'"),
            {"uid": web_user_id},
        ).fetchone()
    if row:
        return {
            "connected": True,
        }
    # 复用已有的配对码，避免每次轮询生成新码
    now = time.time()
    code = None
    for c, v in _pairing_codes.items():
        if v.get("user_id") == web_user_id and v.get("expires", 0) > now:
            code = c
            break
    if not code:
        code = generate_pairing_code()
        _pairing_codes[code]["user_id"] = web_user_id
    return {
        "connected": False,
        "pairing_code": code,
    }


@app.get("/health", summary="健康检查")
def health():
    return {"status": "ok", "scheduler_running": scheduler.running}


# ── SSE（token 走 query param，因为 EventSource 不支持 header）─────────
@app.get("/api/sse", summary="实时通知推送")
async def sse_stream(request: Request, token: str = ""):
    # 验证 token
    if token:
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT * FROM users WHERE token = :t"), {"t": token}
            ).fetchone()
        if not row:
            return Response(status_code=401)
        user = dict(row._mapping)
    else:
        return Response(status_code=401)

    uid = user["id"]
    queue: asyncio.Queue = asyncio.Queue(maxsize=50)
    old = sse_clients.pop(uid, None)
    sse_clients[uid] = queue
    print(f"[SSE] user={uid} connected, total={len(sse_clients)}")

    async def event_generator():
        try:
            yield ": ok\n\n"
            while True:
                try:
                    data = await asyncio.wait_for(queue.get(), timeout=15)
                    print(f"[SSE] -> {uid}: {data.get('task', '')}")
                    yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        except Exception:
            pass
        finally:
            sse_clients.pop(uid, None)
            print(f"[SSE] user={uid} disconnected, total={len(sse_clients)}")

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── 静态文件 ──────────────────────────────────────────
class NoCacheStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope) -> Response:
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        # 去掉 ETag/Last-Modified，防止浏览器做条件请求拿 304
        for _h in ("etag", "last-modified"):
            if _h in response.headers:
                del response.headers[_h]
        return response


static_dir = Path(__file__).parent / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/", NoCacheStaticFiles(directory=str(static_dir), html=True), name="static")
