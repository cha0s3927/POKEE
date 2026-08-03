"""
Feishu Bot — WebSocket 长连接 + 消息收发 + 主动推送
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import traceback

import lark_oapi
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    P2ImMessageReceiveV1,
)
from lark_oapi.event.dispatcher_handler import EventDispatcherHandler
from lark_oapi.ws import Client as WsClient

logger = logging.getLogger(__name__)

# ── 多 bot event loop 隔离 ───────────────────────────────────
# lark_oapi 的 WsClient 使用模块级全局变量 `loop`（lark_oapi.ws.client.loop），
# 多个 WsClient 实例会互相覆盖导致 "event loop already running"。
# 这里用一个 thread-local proxy 替换掉全局 loop，使得每个线程的
# WsClient._connect / _receive_message_loop 中的 loop.xxx() 调用
# 都自动路由到本线程的 event loop。

_loop_store = threading.local()

class _ThreadLoopProxy:
    def run_until_complete(self, fut):
        return _loop_store.loop.run_until_complete(fut)

    def create_task(self, coro):
        return _loop_store.loop.create_task(coro)

    # 以下属性透传，防止 lark_oapi 内部做 hasattr 检查时报错
    def __getattr__(self, name):
        return getattr(_loop_store.loop, name)

import lark_oapi.ws.client as _ws_client_mod
_ws_client_mod.loop = _ThreadLoopProxy()


class FeishuBot:
    def __init__(self, app_id: str, app_secret: str, agent, execute_tool,
                 pairing_handler=None, resolve_user=None, on_open_id=None):
        self.agent = agent
        self.execute_tool = execute_tool
        self.pairing_handler = pairing_handler  # (text, open_id) -> bool
        self.resolve_user = resolve_user  # (open_id) -> str
        self.on_open_id = on_open_id  # (open_id) -> None, 收到消息时通知 caller

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
            log_level=lark_oapi.LogLevel.DEBUG,
        )

        # Monkey-patch _handle_message to log ALL incoming WebSocket frames
        self._patch_ws_client()

        self._thread: threading.Thread | None = None
        self._seen_msg_ids: set[str] = set()

    def _patch_ws_client(self):
        """拦截 WsClient._handle_message，打印所有收到的帧类型和 payload"""
        import lark_oapi.ws.enum as ws_enum
        original_handle_message = self.ws_client._handle_message

        async def patched_handle_message(msg: bytes):
            try:
                from lark_oapi.ws.pb.pbbp2_pb2 import Frame
                frame = Frame()
                frame.ParseFromString(msg)
                ft = ws_enum.FrameType(frame.method)
                print(f"[FEISHU-WS] received frame: type={ft.name if hasattr(ft, 'name') else ft.value}")

                if ft == ws_enum.FrameType.DATA:
                    # 读取 headers 和 payload
                    for h in frame.headers:
                        print(f"[FEISHU-WS]   header: {h.key} = {h.value}")
                    payload_str = frame.payload.decode("utf-8", errors="replace")[:500]
                    print(f"[FEISHU-WS]   payload: {payload_str}")
            except Exception as e:
                print(f"[FEISHU-WS] parse error: {e}")

            return await original_handle_message(msg)

        self.ws_client._handle_message = patched_handle_message

    def start(self):
        def _run():
            _loop = asyncio.new_event_loop()
            asyncio.set_event_loop(_loop)
            _loop_store.loop = _loop  # thread-local，不会影响其他 bot
            self.ws_client.start()

        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()
        logger.info("[feishu] WebSocket started")
        print("[FEISHU] WebSocket thread started")
        if self.pairing_handler:
            print("[FEISHU] pairing_handler is registered")

    def stop(self):
        pass

    def _on_message(self, event: P2ImMessageReceiveV1):
        try:
            self._on_message_impl(event)
        except Exception as e:
            print(f"[FEISHU] ERROR in _on_message: {e}")
            traceback.print_exc()

    def _on_message_impl(self, event: P2ImMessageReceiveV1):
        print(f"[FEISHU] _on_message called, event={event.event is not None}, msg={event.event and event.event.message is not None}")
        if not event.event or not event.event.message:
            print(f"[FEISHU] _on_message skipped: no event or message")
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

        print(f"[FEISHU] received: open_id={open_id} text={text}")

        # 通知 caller 实际的 open_id（per-user bot 的 device-code 注册可能没有返回值）
        if self.on_open_id:
            self.on_open_id(open_id)

        # 检查是否为配对码
        if self.pairing_handler:
            print(f"[FEISHU] checking pairing: text='{text}', open_id={open_id}")
            handled = self.pairing_handler(text, open_id)
            print(f"[FEISHU] pairing_handler returned: {handled}")
            if handled:
                self._send_text(open_id, "绑定成功！现在你可以通过飞书接收提醒了。")
                return

        # 解析用户 ID：绑定用户用 web_user_id，否则 fallback
        if self.resolve_user:
            user_id = self.resolve_user(open_id)
        else:
            user_id = f"feishu:{open_id}"
        logger.info("[feishu] message from %s: %s", user_id, text)

        reply = self.agent.chat(user_message=text, user_id=user_id, persona="default")
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
