"""
Feishu Bot — WebSocket 长连接 + 消息收发 + 主动推送
"""
import json
import logging
import threading

import lark_oapi
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    P2ImMessageReceiveV1,
)
from lark_oapi.event.dispatcher_handler import EventDispatcherHandler
from lark_oapi.ws import Client as WsClient

logger = logging.getLogger(__name__)


class FeishuBot:
    def __init__(self, app_id: str, app_secret: str, agent, execute_tool):
        self.agent = agent
        self.execute_tool = execute_tool

        # API client（发消息用）
        self.api_client = (
            lark_oapi.Client.builder()
            .app_id(app_id)
            .app_secret(app_secret)
            .build()
        )

        # WebSocket client（收消息用）
        handler = (
            EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self._on_message)
            .build()
        )
        self.ws_client = WsClient(
            app_id=app_id,
            app_secret=app_secret,
            event_handler=handler,
            log_level=lark_oapi.LogLevel.WARNING,
        )

        self._thread: threading.Thread | None = None
        self._seen_msg_ids: set[str] = set()

    def start(self):
        self._thread = threading.Thread(target=self.ws_client.start, daemon=True)
        self._thread.start()
        logger.info("[feishu] WebSocket started")

    def stop(self):
        pass

    def _on_message(self, event: P2ImMessageReceiveV1):
        if not event.event or not event.event.message:
            return

        msg = event.event.message

        # 去重：飞书可能重复投递同一消息
        msg_id = msg.message_id or ""
        if msg_id and msg_id in self._seen_msg_ids:
            return
        if msg_id:
            self._seen_msg_ids.add(msg_id)
            # 限制 set 大小
            if len(self._seen_msg_ids) > 10000:
                self._seen_msg_ids.clear()

        # 只处理文本消息
        if msg.message_type != "text":
            return

        try:
            content = json.loads(msg.content)
            text = content.get("text", "")
        except (json.JSONDecodeError, TypeError):
            return

        if not text.strip():
            return

        # 提取用户 open_id
        open_id = ""
        if event.event.sender and event.event.sender.sender_id:
            open_id = event.event.sender.sender_id.open_id or ""

        if not open_id:
            return

        user_id = f"feishu:{open_id}"
        logger.info("[feishu] message from %s: %s", user_id, text)

        reply = self.agent.chat(user_message=text, user_id=user_id)
        self.execute_tool("ack_notifications", {"user_id": user_id})

        self._send_text(open_id, reply)

    def _send_text(self, open_id: str, text: str):
        # Windows GBK 环境下过滤掉 emoji，避免编码异常
        text = text.encode("gbk", errors="ignore").decode("gbk", errors="ignore")
        try:
            req = (
                CreateMessageRequest.builder()
                .receive_id_type("open_id")
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(open_id)
                    .msg_type("text")
                    .content(json.dumps({"text": text}, ensure_ascii=True))
                    .build()
                )
                .build()
            )
            resp = self.api_client.im.v1.message.create(req)
            if not resp.success():
                logger.error("[feishu] send error: %s", resp.msg)
        except Exception as e:
            logger.error("[feishu] send exception: %s", e)

    def send_notification(self, open_id: str, task: str, run_at: str):
        text = f"叮咚！你的提醒到啦 🔔\n\n{task}\n时间：{run_at}"
        self._send_text(open_id, text)
