"""
Auth routes — /api/register, /api/login
"""
from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import datetime
try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import text

from auth import hash_password, verify_password, new_token, validate_email, get_current_user
from config import settings
from database import engine

router = APIRouter(tags=["auth"])

TZ = ZoneInfo(settings.tz_name)


def _now():
    return datetime.now(TZ).isoformat()


# Rate limiter
_rate_store: dict[str, list[float]] = defaultdict(list)


def check_rate(key: str, max_req: int = 10, window: int = 60) -> bool:
    import time
    now = time.time()
    cutoff = now - window
    _rate_store[key] = [t for t in _rate_store[key] if t > cutoff]
    if len(_rate_store[key]) >= max_req:
        return False
    _rate_store[key].append(now)
    return True


# Auth dependency
def auth_user(authorization: Optional[str] = Header(default=None)) -> dict:
    return get_current_user(engine, authorization)


class RegisterRequest(BaseModel):
    email: str = Field(description="邮箱地址")
    password: str = Field(description="密码，至少6位")


class LoginRequest(BaseModel):
    email: str = Field(description="邮箱地址")
    password: str = Field(description="密码")


@router.post("/api/register", summary="注册")
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
             "token": token, "now": _now()},
        )
        conn.commit()
    return {"token": token, "user_id": uid, "email": req.email}


@router.post("/api/login", summary="登录")
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
            {"now": _now(), "id": user["id"]},
        )
        conn.commit()
    return {"token": user["token"], "user_id": user["id"], "email": user["email"]}
