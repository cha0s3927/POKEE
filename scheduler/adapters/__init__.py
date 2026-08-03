"""
IM Adapter Registry — 统一管理所有 IM 平台适配器
"""
from __future__ import annotations

import time
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from adapters.base import BaseIMAdapter

adapter_registry: dict[str, BaseIMAdapter] = {}

# 待绑定（WeChat/WhatsApp auto-bind）
_pending_bindings: dict[str, dict] = {}  # {user_id: {platform, expires}}


def register(adapter: BaseIMAdapter):
    adapter_registry[adapter.platform] = adapter


def get(platform: str) -> BaseIMAdapter | None:
    return adapter_registry.get(platform)


def set_pending_binding(user_id: str, platform: str):
    _pending_bindings[user_id] = {"platform": platform, "expires": time.time() + 300}


def try_auto_bind(platform: str, im_user_id: str) -> str | None:
    """尝试自动绑定 IM 身份到等待中的 web 用户，返回 web_user_id 或 None"""
    now = time.time()
    expired_users = [uid for uid, v in _pending_bindings.items() if v["expires"] < now]
    for uid in expired_users:
        del _pending_bindings[uid]

    candidates = [(uid, v) for uid, v in _pending_bindings.items() if v["platform"] == platform]
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[1]["expires"])
    user_id = candidates[0][0]
    del _pending_bindings[user_id]

    from database import engine
    with Session(engine) as session:
        session.execute(
            text("INSERT OR IGNORE INTO user_im_bindings (id, user_id, platform, im_user_id, created_at) "
                 "VALUES (:id, :uid, :platform, :imuid, :now)"),
            {"id": str(uuid.uuid4())[:12], "uid": user_id, "platform": platform,
             "imuid": im_user_id, "now": _now_iso()},
        )
        session.commit()
    print(f"[AUTO-BIND] {platform}:{im_user_id} -> user_id={user_id}")
    return user_id


def _now_iso() -> str:
    from datetime import datetime
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        from backports.zoneinfo import ZoneInfo
    from config import settings
    return datetime.now(ZoneInfo(settings.tz_name)).isoformat()
