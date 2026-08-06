"""
面试准备 — STAR 故事生成 + 面试准备
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone

import httpx
from openai import OpenAI
from sqlalchemy import text

from config import settings
from database import engine

logger = logging.getLogger(__name__)

client = OpenAI(
    api_key=settings.llm_api_key,
    base_url=settings.llm_base_url,
    http_client=httpx.Client(transport=httpx.HTTPTransport(retries=0), timeout=httpx.Timeout(90.0, connect=10.0)),
)

STAR_EXTRACT_PROMPT = """你是一名面试教练。根据候选人的简历，提取 3-5 个最能体现其能力的经历，扩展为 STAR（情境-任务-行动-结果）格式的面试故事。

## 为每个经历输出以下 JSON：
```json
{
  "stories": [
    {
      "title": "简短标题（8字以内）",
      "situation": "当时所处的背景和环境",
      "task": "你面临的具体任务或挑战",
      "action": "你采取了哪些具体行动",
      "result": "行动带来的量化结果",
      "tags": ["标签1", "标签2"]
    }
  ]
}
```

## 要求
- 只从简历中提取已有内容，不编造
- 优先选取有量化结果的经历
- 覆盖面要广（技术能力、团队协作、项目管理等不同维度）
- 每个故事聚焦一个核心能力"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def generate_star_stories(user_id: str, resume_markdown: str, resume_id: str) -> list[dict]:
    """从简历中自动提取 STAR 故事并存入数据库"""

    response = client.chat.completions.create(
        model=settings.llm_model,
        messages=[
            {"role": "system", "content": STAR_EXTRACT_PROMPT},
            {"role": "user", "content": f"## 候选人简历\n\n{resume_markdown}\n\n请提取 STAR 故事。"},
        ],
        temperature=0.5,
    )

    raw = response.choices[0].message.content or ""

    # Extract JSON
    json_str = raw.strip()
    if json_str.startswith("```"):
        idx = json_str.find("\n")
        if idx != -1:
            json_str = json_str[idx + 1:]
        if json_str.rstrip().endswith("```"):
            json_str = json_str.rstrip()[:json_str.rstrip().rfind("```")]
    start = json_str.find("{")
    end = json_str.rfind("}")
    if start != -1 and end != -1:
        json_str = json_str[start:end + 1]

    try:
        data = json.loads(json_str)
        stories = data.get("stories", [])
    except json.JSONDecodeError:
        logger.warning("Failed to parse STAR stories JSON")
        return []

    # 存入数据库
    saved = []
    with engine.connect() as conn:
        for s in stories:
            sid = str(uuid.uuid4())
            now = now_iso()
            try:
                conn.execute(
                    text("INSERT INTO star_stories (id, user_id, resume_id, title, situation, task, action, result, tags, created_at) "
                         "VALUES (:id, :uid, :rid, :title, :situation, :task, :action, :result, :tags, :now)"),
                    {
                        "id": sid, "uid": user_id, "rid": resume_id,
                        "title": s.get("title", ""),
                        "situation": s.get("situation", ""),
                        "task": s.get("task", ""),
                        "action": s.get("action", ""),
                        "result": s.get("result", ""),
                        "tags": json.dumps(s.get("tags", []), ensure_ascii=False),
                        "now": now,
                    },
                )
                saved.append({**s, "id": sid})
            except Exception:
                logger.exception("Failed to insert STAR story")
        conn.commit()

    return saved


def list_star_stories(user_id: str, resume_id: str | None = None) -> list[dict]:
    """列出用户的 STAR 故事"""
    with engine.connect() as conn:
        if resume_id:
            rows = conn.execute(
                text("SELECT * FROM star_stories WHERE user_id = :uid AND resume_id = :rid ORDER BY created_at DESC"),
                {"uid": user_id, "rid": resume_id},
            ).fetchall()
        else:
            rows = conn.execute(
                text("SELECT * FROM star_stories WHERE user_id = :uid ORDER BY created_at DESC"),
                {"uid": user_id},
            ).fetchall()

    return [
        {
            "id": r.id, "user_id": r.user_id, "resume_id": r.resume_id,
            "title": r.title, "situation": r.situation, "task": r.task,
            "action": r.action, "result": r.result,
            "tags": json.loads(r.tags) if r.tags else [],
            "created_at": r.created_at,
        }
        for r in rows
    ]


def delete_star_story(story_id: str, user_id: str) -> bool:
    """删除一个 STAR 故事"""
    with engine.connect() as conn:
        conn.execute(
            text("DELETE FROM star_stories WHERE id = :sid AND user_id = :uid"),
            {"sid": story_id, "uid": user_id},
        )
        conn.commit()
    return True
