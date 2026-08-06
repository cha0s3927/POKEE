"""
招呼语 / Cover Letter 生成
"""
from __future__ import annotations

import json
import logging

import httpx
from openai import OpenAI

from config import settings
from resume import get_resume_content, get_default_resume_content

logger = logging.getLogger(__name__)

client = OpenAI(
    api_key=settings.llm_api_key,
    base_url=settings.llm_base_url,
    http_client=httpx.Client(transport=httpx.HTTPTransport(retries=0), timeout=httpx.Timeout(60.0, connect=10.0)),
)

PITCH_PROMPT = """你是求职沟通专家。根据候选人的简历和目标岗位 JD，生成一段简短的打招呼话术。

## 场景
这是招聘平台（如 BOSS 直聘）上的"立即沟通"场景，用户需要一段 80-150 字的话术发给 HR。HR 每天收到大量招呼，你的话术需要在 3 秒内抓住注意力。

## 要求
- 一句话开头亮出核心匹配点（不要说"您好，我对这个岗位感兴趣"——太模板）
- 用 1-2 个具体的数据或成果证明能力
- 自然收尾，不要让 HR 觉得是群发模板
- 可以带 1 个 emoji，但不能多
- 不要编造候选人没有的经历"""

COVER_PROMPT = """你是求职信写作专家。根据候选人的简历和目标岗位 JD，生成一封正式的 Cover Letter。

## 格式
- 称呼：如果 JD 中提到了招聘负责人名字则用名字，否则用"招聘负责人您好"
- 正文 3 段：
  1. 自我介绍 + 对公司和岗位的理解（展示你做了功课）
  2. 2-3 个核心匹配点，用具体成果支撑
  3. 表达意愿 + 联系方式
- 落款
- 总字数 250-400 字

## 要求
- 不要重复简历内容，而是建立经历与岗位需求的关联
- 展示对公司的了解（从 JD 中提取）
- 不编造经历"""


def _get_resume_text(resume_id: str | None, user_id: str) -> str:
    """获取简历 Markdown，优先使用 resume_id，fallback 到默认简历"""
    content = None
    if resume_id:
        content = get_resume_content(resume_id)
    if not content:
        content = get_default_resume_content(user_id)
    if not content:
        raise ValueError("请先上传简历")
    return content


def generate_pitch(user_id: str, jd_text: str, resume_id: str | None = None) -> str:
    """生成 BOSS 直聘场景的简短招呼语"""
    resume_text = _get_resume_text(resume_id, user_id)

    response = client.chat.completions.create(
        model=settings.llm_model,
        messages=[
            {"role": "system", "content": PITCH_PROMPT},
            {"role": "user", "content": f"## 候选人简历\n\n{resume_text}\n\n## 目标岗位 JD\n{jd_text[:6000]}\n\n请生成招呼语。"},
        ],
        temperature=0.6,
    )
    return response.choices[0].message.content or ""


def generate_cover(user_id: str, jd_text: str, resume_id: str | None = None) -> str:
    """生成正式 Cover Letter"""
    resume_text = _get_resume_text(resume_id, user_id)

    response = client.chat.completions.create(
        model=settings.llm_model,
        messages=[
            {"role": "system", "content": COVER_PROMPT},
            {"role": "user", "content": f"## 候选人简历\n\n{resume_text}\n\n## 目标岗位 JD\n{jd_text[:6000]}\n\n请生成 Cover Letter。"},
        ],
        temperature=0.5,
    )
    return response.choices[0].message.content or ""
