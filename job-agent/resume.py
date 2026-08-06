"""
简历管理 — 多简历 CRUD + 定制生成（纯 Markdown）
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

TAILOR_PROMPT = """你是简历优化专家。你会收到候选人的完整简历（Markdown 格式）和一份目标岗位的 JD。
你的任务是从完整简历中选取最相关的经历，生成一份针对该岗位的定制简历。

## 核心原则

1. **只从已有内容中选取和重排**：绝不编造公司名、职位、技能、指标、证书。如果候选人的某段经历与 JD 不完全匹配但有关联，可以调整表述侧重，但不能添加原本不存在的事实。
2. **关键词匹配**：找出 JD 中的核心技术栈和关键词，优先保留候选人简历中与之匹配的经历和技能。
3. **顺序优化**：把最相关的经历放在最前面，不相关的经历可以删减或简化。
4. **量化保留**：保留候选人简历中的量化指标（如 "性能提升 40%""管理 5 人团队"）。
5. **简洁有力**：一句话亮点用主动语态，用动词开头（主导、设计、实现、优化）。

## 输出格式

输出纯 Markdown（不含代码块标记）：

```
# [姓名] — [目标岗位]

[1-2 句个人总结，紧扣 JD 需求]

## 技能

- 技能1
- 技能2
...

## 工作经历

### [公司/项目名] — [角色]（[时间段]）

- [最相关的亮点1]
- [最相关的亮点2]
...

## 教育背景

- **学校名** — 学位 专业（年份）
```

如果候选人简历中有项目经历且与 JD 相关，加在工作经历之后。
如果某个部分为空（如没有证书），直接省略该部分。"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def list_resumes(user_id: str) -> list[dict]:
    """列出用户的所有简历"""
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT id, user_id, name, is_default, created_at, updated_at FROM resumes WHERE user_id = :uid ORDER BY created_at DESC"),
            {"uid": user_id},
        ).fetchall()
    return [
        {
            "id": r.id, "user_id": r.user_id, "name": r.name,
            "is_default": bool(r.is_default), "created_at": r.created_at, "updated_at": r.updated_at,
        }
        for r in rows
    ]


def get_resume(resume_id: str) -> dict | None:
    """获取单个简历的完整内容（content 为 Markdown 字符串）"""
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT id, user_id, name, content, is_default, created_at, updated_at FROM resumes WHERE id = :rid"),
            {"rid": resume_id},
        ).fetchone()
    if not row:
        return None
    return {
        "id": row.id, "user_id": row.user_id, "name": row.name,
        "content": row.content, "is_default": bool(row.is_default),
        "created_at": row.created_at, "updated_at": row.updated_at,
    }


def get_default_resume(user_id: str) -> dict | None:
    """获取用户的默认简历"""
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT id, user_id, name, content, is_default, created_at, updated_at FROM resumes WHERE user_id = :uid ORDER BY is_default DESC, updated_at DESC LIMIT 1"),
            {"uid": user_id},
        ).fetchone()
    if not row:
        return None
    return {
        "id": row.id, "user_id": row.user_id, "name": row.name,
        "content": row.content, "is_default": bool(row.is_default),
        "created_at": row.created_at, "updated_at": row.updated_at,
    }


def get_resume_content(resume_id: str) -> str | None:
    """只获取简历 Markdown 内容（不含元数据）"""
    r = get_resume(resume_id)
    return r["content"] if r else None


def get_default_resume_content(user_id: str) -> str | None:
    """获取默认简历的 Markdown 内容"""
    r = get_default_resume(user_id)
    return r["content"] if r else None


def save_resume(user_id: str, name: str, content: str, resume_id: str | None = None) -> dict:
    """创建或更新简历。content 为 Markdown 字符串。"""
    now = now_iso()

    with engine.connect() as conn:
        if resume_id:
            existing = conn.execute(
                text("SELECT id FROM resumes WHERE id = :rid AND user_id = :uid"),
                {"rid": resume_id, "uid": user_id},
            ).fetchone()
            if not existing:
                raise ValueError("简历不存在")
            conn.execute(
                text("UPDATE resumes SET name = :name, content = :content, updated_at = :now WHERE id = :rid"),
                {"name": name, "content": content, "now": now, "rid": resume_id},
            )
        else:
            resume_id = str(uuid.uuid4())
            count = conn.execute(
                text("SELECT COUNT(*) FROM resumes WHERE user_id = :uid"),
                {"uid": user_id},
            ).fetchone()[0]
            is_default = 1 if count == 0 else 0
            conn.execute(
                text("INSERT INTO resumes (id, user_id, name, content, is_default, created_at, updated_at) "
                     "VALUES (:id, :uid, :name, :content, :is_def, :now, :now)"),
                {"id": resume_id, "uid": user_id, "name": name, "content": content, "is_def": is_default, "now": now},
            )
        conn.commit()

    return {"status": "ok", "resume_id": resume_id, "updated_at": now}


def delete_resume(resume_id: str, user_id: str) -> bool:
    with engine.connect() as conn:
        count = conn.execute(
            text("SELECT COUNT(*) FROM resumes WHERE user_id = :uid"),
            {"uid": user_id},
        ).fetchone()[0]
        if count <= 1:
            raise ValueError("至少保留一份简历")
        conn.execute(text("DELETE FROM resumes WHERE id = :rid AND user_id = :uid"), {"rid": resume_id, "uid": user_id})
        conn.commit()
    return True


def set_default_resume(resume_id: str, user_id: str) -> dict:
    with engine.connect() as conn:
        conn.execute(text("UPDATE resumes SET is_default = 0 WHERE user_id = :uid"), {"uid": user_id})
        conn.execute(text("UPDATE resumes SET is_default = 1 WHERE id = :rid AND user_id = :uid"), {"rid": resume_id, "uid": user_id})
        conn.commit()
    return {"status": "ok"}


def resume_to_markdown(content) -> str:
    """将简历内容转换为 Markdown。如果已经是 Markdown 则直接返回，如果是 JSON dict 则转换。"""
    if isinstance(content, str):
        return content
    # 兼容旧 JSON 格式
    lines = []
    name = content.get("name", "")
    title = content.get("title", "")
    summary = content.get("summary", "")

    if name:
        header = f"# {name}"
        if title:
            header += f" — {title}"
        lines.append(header)
        lines.append("")

    if summary:
        lines.append(summary)
        lines.append("")

    target = content.get("target") or {}
    if target.get("roles"):
        lines.append(f"**目标岗位**: {' / '.join(target['roles'])}")
    if target.get("locations"):
        lines.append(f"**期望城市**: {' / '.join(target['locations'])}")
    if target.get("salary_min") or target.get("salary_max"):
        smin = target.get("salary_min") or ""
        smax = target.get("salary_max") or ""
        lines.append(f"**期望薪资**: {smin} - {smax} 元/月")
    if target:
        lines.append("")

    skills = content.get("skills")
    if skills:
        lines.append("## 技能")
        lines.append("")
        if isinstance(skills, dict):
            for cat, items in [("master", "熟练掌握"), ("familiar", "熟悉"), ("exposed", "了解"), ("ai_tools", "AI 辅助开发")]:
                if skills.get(cat):
                    lines.append(f"- {items}: {' / '.join(skills[cat])}")
        elif isinstance(skills, list):
            lines.append(" | ".join(f"`{s}`" for s in skills))
        lines.append("")

    experiences = content.get("experience") or []
    if experiences:
        lines.append("## 工作经历")
        lines.append("")
        for exp in experiences:
            company = exp.get("company", "")
            exp_title = exp.get("title", "")
            start = exp.get("start", "")
            end = exp.get("end", "") or "至今"
            period = f"{start} - {end}" if start else ""
            lines.append(f"### {company} — {exp_title}")
            if period:
                lines.append(f"*{period}*")
            lines.append("")
            highlights = exp.get("highlights") or []
            for h in highlights:
                lines.append(f"- {h}")
            lines.append("")

    projects = content.get("projects") or []
    if projects:
        lines.append("## 项目经历")
        lines.append("")
        for proj in projects:
            proj_name = proj.get("name", "")
            proj_role = proj.get("role", "") or proj.get("title", "")
            proj_start = proj.get("start", "")
            proj_end = proj.get("end", "")
            period = f"{proj_start} - {proj_end}" if proj_start else proj.get("date", "")
            if proj_name:
                header = f"### {proj_name}"
                if proj_role:
                    header += f" — {proj_role}"
                lines.append(header)
                if period:
                    lines.append(f"*{period}*")
                lines.append("")
                highlights = proj.get("highlights") or proj.get("achievements") or []
                for h in highlights:
                    lines.append(f"- {h}")
                lines.append("")

    education = content.get("education") or []
    if education:
        lines.append("## 教育背景")
        lines.append("")
        for edu in education:
            school = edu.get("school", "")
            degree = edu.get("degree", "")
            major = edu.get("major", "")
            year = edu.get("year", "")
            edu_line = f"**{school}**"
            if degree or major:
                edu_line += f" — {degree} {major}"
            if year:
                edu_line += f"（{year}）"
            lines.append(f"- {edu_line}")
        lines.append("")

    certs = content.get("certifications") or []
    if certs:
        lines.append("## 证书")
        lines.append("")
        for c in certs:
            if isinstance(c, str):
                lines.append(f"- {c}")
            else:
                cname = c.get("name", "")
                cdate = c.get("date", "")
                lines.append(f"- {cname}" + (f"（{cdate}）" if cdate else ""))
        lines.append("")

    return "\n".join(lines).strip()


def tailor_resume(resume_id: str, jd_text: str) -> str:
    """根据 JD 从指定简历中选取经历，生成定制 Markdown 简历"""
    r = get_resume(resume_id)
    if not r:
        raise ValueError("简历不存在")

    markdown = r["content"]

    response = client.chat.completions.create(
        model=settings.llm_model,
        messages=[
            {"role": "system", "content": TAILOR_PROMPT},
            {"role": "user", "content": f"## 候选人完整简历\n\n{markdown}\n\n## 目标岗位 JD\n{jd_text[:8000]}\n\n请生成定制简历。"},
        ],
        temperature=0.4,
    )

    return response.choices[0].message.content or ""
