"""
认证 — 注册 / 登录 / Token（与 POKEE 提醒服务共享 users 表）
"""
import hashlib
import os
import re
import secrets
import uuid
from datetime import datetime

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import text

from database import engine

router = APIRouter(tags=["auth"])

EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")
ITERATIONS = 600_000


def _now_iso() -> str:
    return datetime.utcnow().isoformat()


def hash_password(password: str) -> str:
    salt = os.urandom(32)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS)
    return f"pbkdf2:sha256:{ITERATIONS}:{salt.hex()}:{dk.hex()}"


def verify_password(password: str, hashed: str) -> bool:
    try:
        _, algo, iters, salt_hex, dk_hex = hashed.split(":")
        salt = bytes.fromhex(salt_hex)
        dk = bytes.fromhex(dk_hex)
        new_dk = hashlib.pbkdf2_hmac(algo, password.encode(), salt, int(iters))
        return secrets.compare_digest(dk, new_dk)
    except Exception:
        return False


def new_token() -> str:
    return secrets.token_hex(32)


# ── Pydantic models ──

class RegisterRequest(BaseModel):
    email: str = Field(description="邮箱")
    password: str = Field(min_length=6, description="密码（至少6位）")


class LoginRequest(BaseModel):
    email: str
    password: str


# ── Endpoints ──

@router.post("/api/register", summary="注册")
def register(req: RegisterRequest):
    email = req.email.strip().lower()
    if not email or "@" not in email:
        raise HTTPException(400, "请输入有效的邮箱地址")

    with engine.connect() as conn:
        existing = conn.execute(
            text("SELECT id FROM users WHERE email = :email"), {"email": email}
        ).fetchone()
        if existing:
            raise HTTPException(409, "该邮箱已注册")

        uid = str(uuid.uuid4())
        password_hash = hash_password(req.password)
        token = new_token()
        now = _now_iso()

        conn.execute(
            text("INSERT INTO users (id, email, password_hash, token, created_at, persona, points, lang) "
                 "VALUES (:id, :email, :ph, :token, :now, 'default', 50, 'zh')"),
            {"id": uid, "email": email, "ph": password_hash, "token": token, "now": now},
        )
        conn.commit()

    return {"token": token, "user_id": uid, "email": email}


@router.post("/api/login", summary="登录")
def login(req: LoginRequest):
    email = req.email.strip().lower()

    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT id, password_hash, token FROM users WHERE email = :email"),
            {"email": email},
        ).fetchone()

    if not row:
        raise HTTPException(401, "邮箱或密码错误")
    if not verify_password(req.password, row.password_hash):
        raise HTTPException(401, "邮箱或密码错误")

    token = new_token()
    now = _now_iso()

    with engine.connect() as conn:
        conn.execute(
            text("UPDATE users SET token = :token, last_login_at = :now WHERE id = :uid"),
            {"token": token, "uid": row.id, "now": now},
        )
        conn.commit()

    return {"token": token, "user_id": row.id, "email": email}


# ── Auth dependency ──

def auth_user(req: Request) -> dict:
    """从 Authorization header 解析 token，返回用户信息"""
    auth_header = req.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(401, "请先登录")

    token = auth_header[7:]
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT id, email, points FROM users WHERE token = :t"),
            {"t": token},
        ).fetchone()

    if not row:
        raise HTTPException(401, "登录已过期，请重新登录")

    return {"id": row.id, "email": row.email, "points": row.points}
