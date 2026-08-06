"""
认证 — 注册 / 登录 / JWT
"""
import uuid
from datetime import datetime, timezone, timedelta

import bcrypt
import jwt
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import text

from config import settings
from database import engine

router = APIRouter(tags=["auth"])

JWT_SECRET = settings.jwt_secret
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_HOURS = 720  # 30 days


# ── Pydantic models ──

class RegisterRequest(BaseModel):
    email: str = Field(description="邮箱")
    password: str = Field(min_length=6, description="密码（至少6位）")


class LoginRequest(BaseModel):
    email: str
    password: str


# ── Helpers ──

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _create_token(user_id: str) -> str:
    payload = {
        "sub": user_id,
        "iat": datetime.now(timezone.utc),
        "exp": datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRE_HOURS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


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
        password_hash = bcrypt.hashpw(req.password.encode(), bcrypt.gensalt()).decode()
        token = _create_token(uid)
        now = _now_iso()

        conn.execute(
            text("INSERT INTO users (id, email, password_hash, token, created_at) "
                 "VALUES (:id, :email, :ph, :token, :now)"),
            {"id": uid, "email": email, "ph": password_hash, "token": token, "now": now},
        )
        conn.commit()

    return {"token": token, "user_id": uid, "email": email}


@router.post("/api/login", summary="登录")
def login(req: LoginRequest):
    email = req.email.strip().lower()

    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT id, password_hash FROM users WHERE email = :email"),
            {"email": email},
        ).fetchone()

    if not row:
        raise HTTPException(401, "邮箱或密码错误")

    if not bcrypt.checkpw(req.password.encode(), row.password_hash.encode()):
        raise HTTPException(401, "邮箱或密码错误")

    token = _create_token(row.id)

    with engine.connect() as conn:
        conn.execute(
            text("UPDATE users SET token = :token WHERE id = :uid"),
            {"token": token, "uid": row.id},
        )
        conn.commit()

    return {"token": token, "user_id": row.id, "email": email}


# ── Auth dependency ──

def auth_user(req: Request) -> dict:
    """从 Authorization header 解析 JWT，返回用户信息"""
    auth_header = req.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(401, "请先登录")

    token = auth_header[7:]
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "登录已过期，请重新登录")
    except jwt.InvalidTokenError:
        raise HTTPException(401, "无效的登录凭证")

    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(401, "无效的登录凭证")

    # Verify user still exists
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT id, email FROM users WHERE id = :uid"),
            {"uid": user_id},
        ).fetchone()
    if not row:
        raise HTTPException(401, "用户不存在")

    return {"id": row.id, "email": row.email}
