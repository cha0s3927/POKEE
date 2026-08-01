"""
LinkedIn Bot — 轮询消息 + 收发消息 + 主动推送
使用 linkedin-api (Voyager API 逆向)，纯 Python，无需 Node.js bridge
"""
from __future__ import annotations

import logging
import threading
import time

from linkedin_api import Linkedin

logger = logging.getLogger(__name__)

POLL_INTERVAL = 5  # seconds between polling


class LinkedInBot:
    def __init__(self, email: str, password: str, agent, execute_tool, cookies: dict | None = None):
        self.email = email
        self.password = password
        self.agent = agent
        self.execute_tool = execute_tool
        self.api: Linkedin | None = None
        self._running = False
        self._thread: threading.Thread | None = None
        self._last_msg_body: dict[str, str] = {}
        self._recent_sent: dict[str, float] = {}
        self._cookies = cookies

    def start(self):
        if self._cookies:
            import requests
            jar = requests.cookies.RequestsCookieJar()
            for name, value in self._cookies.items():
                jar.set(name, value, domain=".linkedin.com")
            self.api = Linkedin(self.email, self.password, cookies=jar)
        else:
            self.api = Linkedin(self.email, self.password)
        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        logger.info("[linkedin] polling started")

    def stop(self):
        self._running = False

    def _poll_loop(self):
        while self._running:
            try:
                conversations = self.api.get_conversations()
                # API 可能返回 dict 或 list
                conv_list = conversations if isinstance(conversations, list) else (
                    conversations.get("elements", [])
                )
                for conv in conv_list:
                    self._check_conversation(conv)
            except Exception as e:
                logger.error("[linkedin] poll error: %s", e)
            time.sleep(POLL_INTERVAL)

    def _check_conversation(self, conv: dict):
        # LinkedIn Voyager API conversation 结构: id 可能是多种 key 名
        conv_id = (
            conv.get("id")
            or conv.get("conversationId")
            or conv.get("entityUrn")
            or ""
        )
        if not conv_id:
            return

        # 获取最新一条消息
        msg = self._extract_latest_message(conv)
        if not msg:
            return

        msg_body = self._extract_body(msg)
        if not msg_body.strip():
            return

        # 去重：同一条消息不处理两次
        last_body = self._last_msg_body.get(conv_id, "")
        if msg_body == last_body:
            return
        self._last_msg_body[conv_id] = msg_body

        # 防回环：跳过 bot 刚发出的回复
        if msg_body in self._recent_sent:
            if time.time() - self._recent_sent[msg_body] < 10:
                return
            del self._recent_sent[msg_body]

        user_id = f"linkedin:{conv_id}"
        logger.info("[linkedin] message from %s: %s", user_id, msg_body[:80])

        reply = self.agent.chat(user_message=msg_body, user_id=user_id)
        self.execute_tool("ack_notifications", {"user_id": user_id})

        try:
            self.api.send_message(reply, conversation_urn_id=conv_id)
            self._recent_sent[reply] = time.time()
            logger.info("[linkedin] replied to %s", user_id)
        except Exception as e:
            logger.error("[linkedin] send error: %s", e)

    def send_notification(self, conv_id: str, task: str, run_at: str):
        """主动推送提醒"""
        text = f"叮咚！你的提醒到啦 🔔\n\n{task}\n时间：{run_at}"
        if self.api:
            self.api.send_message(text, conversation_urn_id=conv_id)

    def _extract_latest_message(self, conv: dict) -> dict | None:
        """从 conversation 中提取最新一条消息"""
        messages = conv.get("messages") or conv.get("elements") or []
        if not messages:
            try:
                detail = self.api.get_conversation(conv.get("id") or conv.get("entityUrn") or "")
                messages = detail.get("messages") or detail.get("elements") or []
            except Exception:
                return None
        if not messages:
            return None
        if isinstance(messages, list):
            return messages[0]
        return messages

    def _extract_body(self, msg: dict) -> str:
        """从 message 中提取文本"""
        if isinstance(msg, str):
            return msg
        body = msg.get("body") or msg.get("messageBody") or {}
        if isinstance(body, str):
            return body
        return body.get("text") or msg.get("text") or ""
