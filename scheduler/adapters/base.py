"""
BaseIMAdapter — 所有 IM 平台的统一接口
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class BaseIMAdapter(ABC):
    platform: str  # "wechat" | "whatsapp" | "feishu" | "linkedin"

    def __init__(self, agent, execute_tool):
        self.agent = agent
        self.execute_tool = execute_tool

    # ── 子类必须实现 ──

    @abstractmethod
    def get_status(self, user_id: str) -> dict:
        """返回 {connected: bool, qr_available: bool, ...}"""
        ...

    @abstractmethod
    def get_qr(self, user_id: str) -> dict:
        """返回 QR/配对码信息，如 {qr: str, pairing_code: str, connected: bool}"""
        ...

    @abstractmethod
    def send_notification(self, im_user_id: str, task: str, run_at: str, web_user_id: str = "") -> bool:
        """主动推送提醒通知"""
        ...

    @abstractmethod
    def start(self):
        """启动 adapter（连接/轮询/恢复 session）"""
        ...

    @abstractmethod
    def stop(self):
        """停止 adapter"""
        ...

    # ── 通用实现 ──

    def handle_incoming(self, message: str, user_id: str) -> str:
        """收到消息 → agent.chat() → ack → 返回 reply"""
        reply = self.agent.chat(user_message=message, user_id=user_id)
        self.execute_tool("ack_notifications", {"user_id": user_id})
        return reply

    def resolve_binding(self, im_user_id: str) -> str | None:
        """查 user_im_bindings 表，返回 web_user_id 或 None"""
        from database import engine
        from sqlalchemy import text
        from sqlalchemy.orm import Session
        with Session(engine) as session:
            row = session.execute(
                text("SELECT user_id FROM user_im_bindings WHERE platform=:p AND im_user_id=:imuid"),
                {"p": self.platform, "imuid": im_user_id},
            ).fetchone()
        return row.user_id if row else None
