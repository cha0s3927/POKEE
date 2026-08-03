"""
Authentication — password hashing, token management, auth dependency.
No extra dependencies, uses stdlib hashlib only.
"""
from __future__ import annotations

import hashlib
import os
import re
import secrets
from datetime import datetime
try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

from typing import Optional

from fastapi import Header, HTTPException
from sqlalchemy import text

TZ = ZoneInfo("Asia/Shanghai")
EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")

ITERATIONS = 600_000


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


def now_iso() -> str:
    return datetime.now(TZ).isoformat()


def validate_email(email: str) -> bool:
    return bool(EMAIL_RE.match(email))


def get_current_user(engine, authorization: Optional[str] = Header(default=None)) -> dict:
    """FastAPI 依赖：从 Authorization header 提取 token，返回当前用户。"""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="请先登录")
    token = authorization[7:]
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT * FROM users WHERE token = :t"), {"t": token}
        ).fetchone()
    if not row:
        raise HTTPException(status_code=401, detail="登录已过期，请重新登录")
    return dict(row._mapping)
