"""
LLM Agent — tool-calling loop via DeepSeek API (OpenAI-compatible).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

from openai import OpenAI

logger = logging.getLogger(__name__)
TZ = ZoneInfo("Asia/Shanghai")

SYSTEM_PROMPT = """你是 POKEE，一个帮用户记事的助手。你的个性：话不多但贴心，像个记性好的朋友，而不是客服机器人。

对话原则：
- 直接说人话。用户说"10秒后提醒我起床"，你就回"好，10秒后叫你"——不要格式化，不要列清单
- 确认提醒时自然地带上时间和内容，融入句子里。比如："记下了，下午三点提醒你开会，记得提前看看议程"
- 别用加粗、标题、列表。你不是在写文档，是在聊天
- 关心的内容要和具体任务相关，不要每句话都塞一句万能叮嘱。叫起床时可以说"十秒后叫你，先闭会儿眼"，提醒开会时可以说"提前看看议程"，但提醒喝水就不需要硬加一句叮嘱
- emoji 偶尔用一两个可以，别每条消息都来
- 取消提醒时可以调侃一下
- 用户没要求的事别加戏

技术约束（内部使用，不要在回复中提及）：
- 涉及相对时间时先调 get_current_time 获取当前时间
- 时间格式 ISO 8601，时区 Asia/Shanghai (UTC+8)
- user_id 默认 "default"
- 创建提醒后自然确认，列出、查询、取消同理"""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_current_time",
            "description": "获取当前日期和时间（Asia/Shanghai 时区），用于计算相对时间表达式",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_reminder",
            "description": "创建一个定时提醒，到时间后系统会通知用户",
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {"type": "string", "description": "提醒内容"},
                    "run_at": {"type": "string", "description": "触发时间，ISO 8601 格式，如 2026-07-31T15:00:00"},
                    "user_id": {"type": "string", "description": "用户标识，默认 default"},
                },
                "required": ["task", "run_at"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_reminders",
            "description": "查询提醒列表，可按状态筛选",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string", "description": "用户标识，默认 default"},
                    "status": {
                        "type": "string",
                        "enum": ["pending", "sent", "cancelled"],
                        "description": "筛选状态：pending=待触发, sent=已触发, cancelled=已取消。不传则返回全部",
                    },
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_reminder",
            "description": "查看单个提醒的详细信息",
            "parameters": {
                "type": "object",
                "properties": {
                    "reminder_id": {"type": "string", "description": "提醒 ID"},
                },
                "required": ["reminder_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_reminder",
            "description": "取消一个尚未触发的提醒",
            "parameters": {
                "type": "object",
                "properties": {
                    "reminder_id": {"type": "string", "description": "提醒 ID"},
                },
                "required": ["reminder_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_notifications",
            "description": "获取用户尚未查看的已触发通知。Agent 应在每次对话开始时调用，检查是否有刚触发的提醒",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string", "description": "用户标识，默认 default"},
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ack_notifications",
            "description": "将未读通知标记为已读（用户已看到）",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string", "description": "用户标识，默认 default"},
                },
                "additionalProperties": False,
            },
        },
    },
]

MAX_TOOL_ITERATIONS = 5
MAX_MESSAGES = 101  # system prompt + 100 messages


class Agent:
    def __init__(self, api_key: str, base_url: str, model: str, tool_executor):
        self.client = OpenAI(api_key=api_key, base_url=base_url)
        self.model = model
        self.execute_tool = tool_executor
        self.sessions: dict[str, list[dict]] = {}

    def chat(self, user_message: str, user_id: str) -> str:
        if user_id not in self.sessions:
            self.sessions[user_id] = [{"role": "system", "content": SYSTEM_PROMPT}]

        messages = self.sessions[user_id]

        # Check for pending notifications before processing
        notifications = self.execute_tool("get_notifications", {"user_id": user_id})
        if notifications:
            note_lines = ["[系统] 以下提醒已触发，请注意："]
            for n in notifications:
                note_lines.append(f"  - {n['task']}（{n['run_at']}）")
            messages.append({"role": "system", "content": "\n".join(note_lines)})

        # 预注入当前时间，省去 get_current_time 工具调用（减少一次 API 往返）
        tz_now = datetime.now(TZ).strftime("%Y-%m-%dT%H:%M:%S")
        messages.append({"role": "system", "content": f"[当前时间: {tz_now} Asia/Shanghai]"})
        messages.append({"role": "user", "content": user_message})

        for _ in range(MAX_TOOL_ITERATIONS):
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=TOOLS,
                tool_choice="auto",
            )

            msg = response.choices[0].message

            if msg.tool_calls:
                # Record assistant's tool call request
                tool_call_entries = []
                for tc in msg.tool_calls:
                    tool_call_entries.append({
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    })

                messages.append({
                    "role": "assistant",
                    "content": msg.content,
                    "tool_calls": tool_call_entries,
                })

                # Execute each tool call
                for tc in msg.tool_calls:
                    name = tc.function.name
                    try:
                        args = json.loads(tc.function.arguments)
                    except json.JSONDecodeError:
                        args = {}
                    args["user_id"] = user_id  # 强制覆写，防止 LLM 越权
                    logger.info("tool_call: %s(%s)", name, args)
                    result = self.execute_tool(name, args)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps(result, ensure_ascii=False, default=str),
                    })
            else:
                # Final text response
                messages.append({"role": "assistant", "content": msg.content})

                # Trim old messages: keep system prompt + last 100, never break tool-call pairs
                if len(messages) > MAX_MESSAGES:
                    keep_from = len(messages) - 100
                    while keep_from > 1 and messages[keep_from].get("role") == "tool":
                        keep_from -= 1
                    self.sessions[user_id] = [messages[0]] + messages[keep_from:]

                return msg.content

        return "抱歉，处理超时，请重试。"

    def clear_session(self, user_id: str):
        self.sessions.pop(user_id, None)
