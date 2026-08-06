"""
轻量工作流路由 — 意图分类 + 确定性工具链执行

命中意图 → 按固定顺序逐个执行工具（tool_choice 强制指定），LLM 仅填充参数。
未命中 → fallback 到 agent 自由 tool-calling。
"""
from __future__ import annotations

import json
import logging
from typing import Optional

import httpx
from openai import OpenAI

from config import settings

logger = logging.getLogger(__name__)

client = OpenAI(
    api_key=settings.llm_api_key,
    base_url=settings.llm_base_url,
    http_client=httpx.Client(transport=httpx.HTTPTransport(retries=0), timeout=httpx.Timeout(90.0, connect=10.0)),
)

# ── 意图分类 ──

INTENT_PROMPT = """分析用户消息，只返回一个 JSON: {"intent":"意图名"}

意图列表及典型表述:
- perfect_profile: "用简历完善画像""根据简历生成求职画像""帮我完善求职画像"
- score_jd: 用户贴了岗位描述（含"岗位职责""任职要求""JD""招聘"等关键词）→ score_jd
- tailor_resume: "定制简历""针对这个岗位改简历""生成定制版简历"
- generate_pitch: "帮我写个招呼语""写招呼语""沟通话术""打招呼的话""怎么跟HR说""BOSS上怎么聊"
- generate_cover: "写一封正式的求职信""写求职信""Cover Letter""写封cover letter"
- search_jobs: "搜索岗位""找一下XX的工作""帮我搜""有什么岗位"
- save_job: "收藏这个岗位""保存岗位""把这个存下来"
- list_saved_jobs: "我收藏的岗位""已保存的岗位""看看收藏"
- view_resume: "看看我的简历""查看简历""打开简历"
- diagnose_resume: "简历有什么问题""帮我改简历""简历诊断""简历哪里不好"
- generate_stories: "帮我生成面试用的STAR故事""生成STAR""面试故事""准备面试故事"
- view_profile: "查看画像""我的求职画像""我的画像"
- update_profile: "修改画像""更新画像""薪资改成""改成XX"
- view_tasks: "学习计划""成长计划""我的任务""待办"
- add_task: "我要学""添加任务""加入计划""帮我把XX加入成长计划"
- chat: 闲聊、打招呼、咨询建议、问"你觉得我适合什么方向"等开放式问题

规则:
1. 优先匹配具体意图，不要轻易选 chat
2. 用户贴了大段岗位描述文本（>=100字且含"职责/要求/经验"等词）→ score_jd
3. "完善画像"≠修改画像字段，前者是 perfect_profile，后者是 update_profile
4. "我要学X""我想学X""帮我加个任务X" → add_task，不是 update_profile"""


def classify_intent(user_message: str) -> str:
    """分类用户意图，返回 intent name。失败时返回 'chat'。"""
    try:
        resp = client.chat.completions.create(
            model=settings.llm_model,
            messages=[
                {"role": "system", "content": INTENT_PROMPT},
                {"role": "user", "content": user_message[:2000]},
            ],
            temperature=0.0,
            max_tokens=30,
        )
        raw = resp.choices[0].message.content.strip()
        data = json.loads(raw)
        intent = data.get("intent", "chat")
        logger.info("intent: %s → %s", user_message[:60], intent)
        return intent
    except Exception as e:
        logger.warning("Intent classification failed: %s", e)
        return "chat"


# ── 工作流定义 ──

WORKFLOWS: dict[str, list[str]] = {
    "perfect_profile":   ["get_my_resume", "update_my_profile", "add_my_task"],
    "score_jd":          ["list_my_resumes", "score_job"],
    "tailor_resume":     ["tailor_resume"],
    "generate_pitch":    ["generate_pitch"],
    "generate_cover":    ["generate_cover"],
    "search_jobs":       ["search_jobs"],
    "save_job":          ["save_job"],
    "list_saved_jobs":   ["list_saved_jobs"],
    "view_resume":       ["list_my_resumes", "get_my_resume"],
    "diagnose_resume":   ["list_my_resumes", "get_my_resume"],
    "generate_stories":  ["generate_star_stories"],
    "view_profile":      ["get_my_profile"],
    "update_profile":    ["update_my_profile"],
    "view_tasks":        ["list_my_tasks"],
    "add_task":          ["add_my_task"],
}


def is_workflow_intent(intent: str) -> bool:
    return intent in WORKFLOWS


# ── 工作流执行器 ──

def execute_workflow(
    intent: str,
    user_id: str,
    user_message: str,
    messages: list[dict],
    tools: list[dict],
    _execute_fn,
) -> tuple[str, list[str]]:
    """按固定工具链执行工作流，LLM 只负责为每步填充参数。

    返回 (final_reply, tool_calls_made)。
    """
    tool_chain = WORKFLOWS[intent]
    tool_calls_made: list[str] = []

    # 把相关工具定义过滤出来
    tool_map = {t["function"]["name"]: t for t in tools}
    relevant_tools = [tool_map[name] for name in tool_chain if name in tool_map]

    # 添加用户消息
    msgs = list(messages)
    msgs.append({"role": "user", "content": user_message})

    for step_idx, tool_name in enumerate(tool_chain):
        if tool_name not in tool_map:
            logger.warning("Unknown tool in workflow %s: %s", intent, tool_name)
            continue

        try:
            resp = client.chat.completions.create(
                model=settings.llm_model,
                messages=msgs,
                tools=relevant_tools,
                tool_choice={"type": "function", "function": {"name": tool_name}},
            )
        except Exception as e:
            logger.exception("LLM call failed for workflow %s step %s", intent, tool_name)
            break

        msg = resp.choices[0].message

        if not msg.tool_calls:
            logger.warning("Workflow %s: LLM refused to call %s", intent, tool_name)
            # 尝试不强制 tool_choice 重新请求
            try:
                resp = client.chat.completions.create(
                    model=settings.llm_model,
                    messages=msgs,
                    tools=relevant_tools,
                    tool_choice="auto",
                )
                msg = resp.choices[0].message
                if not msg.tool_calls:
                    break
            except Exception:
                break

        tc = msg.tool_calls[0]
        name = tc.function.name
        try:
            args = json.loads(tc.function.arguments)
        except json.JSONDecodeError:
            args = {}

        logger.info("workflow[%s] step %d: %s args=%s", intent, step_idx + 1, name,
                     {k: str(v)[:80] for k, v in args.items()})

        result = _execute_fn(name, args, user_id)
        tool_calls_made.append(name)

        # 将 tool call + result 追加到消息中，供下一步参考
        msgs.append({
            "role": "assistant",
            "content": msg.content,
            "tool_calls": [{
                "id": tc.id,
                "type": "function",
                "function": {"name": name, "arguments": tc.function.arguments},
            }],
        })
        msgs.append({
            "role": "tool",
            "tool_call_id": tc.id,
            "content": json.dumps(result, ensure_ascii=False, default=str),
        })

    # 生成最终回复
    try:
        resp = client.chat.completions.create(
            model=settings.llm_model,
            messages=msgs + [{"role": "user", "content": "请用自然语言总结刚才完成的操作，不要用列表格式。"}],
        )
        reply = resp.choices[0].message.content
    except Exception:
        reply = "操作已完成。"
        if tool_calls_made:
            reply += f" 执行了: {', '.join(tool_calls_made)}"

    return reply, tool_calls_made
