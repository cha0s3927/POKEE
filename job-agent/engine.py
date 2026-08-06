"""
JD 评分引擎 — 7维评分 + LLM 调用
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Dict, List

import httpx
from openai import OpenAI

from config import settings

logger = logging.getLogger(__name__)

client = OpenAI(
    api_key=settings.llm_api_key,
    base_url=settings.llm_base_url,
    http_client=httpx.Client(transport=httpx.HTTPTransport(retries=0), timeout=httpx.Timeout(90.0, connect=10.0)),
)

SCORING_PROMPT = """你是求职顾问，帮候选人评估岗位匹配度。你需要严格基于候选人的简历与 JD 的对比，给出客观评分。

## 评分维度（0-100 分）

1. **技能匹配**（权重 30%）：候选人技能栈与 JD 要求的技术/工具重叠度。完全匹配 90+，核心缺 2 项以上不超过 60。
2. **经验年限**（权重 15%）：JD 要求的年限 vs 候选人实际年限。在要求范围内 90+，偏低 1-2 年 65-75，偏高过多（可能 overqualified）60-70。
3. **薪资匹配**（权重 20%）：JD 薪资范围 vs 候选人期望薪资。在范围内 90+，略低于下限 65-75，显著偏离 40-55。JD 未注明薪资则给 70（中性）。
4. **地点/远程**（权重 10%）：工作地点 vs 候选人偏好。完全匹配 95+，同城不同区 80-85，跨省但支持远程 65-75，强制 on-site 异地 30-45。
5. **公司阶段**（权重 10%）：公司类型（创业/成长/成熟）vs 候选人偏好。完全匹配 90+，不匹配但不排斥 65-75。
6. **成长空间**（权重 10%）：岗位描述的晋升路径、技术成长、scope 大小 vs 候选人职业阶段。
7. **面试可能性**（权重 5%）：综合上述维度，估计拿到面试邀约的概率。

## 输出格式

严格按以下 JSON 格式输出，不要输出其他内容：

```json
{
  "total": 75,
  "dimensions": {
    "技能匹配": 80,
    "经验年限": 85,
    "薪资匹配": 70,
    "地点/远程": 90,
    "公司阶段": 75,
    "成长空间": 65,
    "面试可能性": 60
  },
  "strengths": ["Go 和 Kubernetes 经验完全匹配", "..."],
  "weaknesses": ["期望薪资高于 JD 上限 15%", "..."],
  "verdict": "apply",
  "verdict_reason": "核心技能高度匹配，薪资差距可谈，建议投递。"
}
```

## verdict 判定规则
- strong_match: total >= 80，核心技能高度重叠，强烈建议投递
- apply: total >= 60，值得投递，有些小瑕疵可以面试再谈
- maybe: total >= 40，有明显不匹配但并非全无机会，可作为备选
- skip: total < 40，严重不匹配或硬伤（如强制 on-site 异地），不建议浪费时间"""


@dataclass
class JobScore:
    total: float
    dimensions: Dict[str, float]
    strengths: List[str]
    weaknesses: List[str]
    verdict: str
    verdict_reason: str


def score_job(resume_markdown: str, jd_text: str) -> JobScore:
    """根据用户简历 Markdown 和 JD 文本，返回多维评分"""

    user_message = f"""## 候选人简历

{resume_markdown}

## 岗位 JD
{jd_text[:8000]}

请按评分维度逐一分析并输出 JSON。"""

    response = client.chat.completions.create(
        model=settings.llm_model,
        messages=[
            {"role": "system", "content": SCORING_PROMPT},
            {"role": "user", "content": user_message},
        ],
        temperature=0.3,
    )

    raw = response.choices[0].message.content or ""
    logger.debug("Scoring raw response: %s", raw[:500])

    # Extract JSON from response (may be wrapped in ```json)
    json_str = _extract_json(raw)
    data = json.loads(json_str)

    # Clamp total to 0-100
    total = max(0, min(100, int(data.get("total", 50))))

    return JobScore(
        total=total,
        dimensions=data.get("dimensions", {}),
        strengths=data.get("strengths", []),
        weaknesses=data.get("weaknesses", []),
        verdict=data.get("verdict", "maybe"),
        verdict_reason=data.get("verdict_reason", ""),
    )


def _extract_json(raw: str) -> str:
    """从 LLM 响应中提取 JSON，处理 ```json 包裹和尾部逗号"""
    raw = raw.strip()
    if raw.startswith("```"):
        # Remove opening fence
        idx = raw.find("\n")
        if idx != -1:
            raw = raw[idx + 1:]
        # Remove closing fence
        if raw.rstrip().endswith("```"):
            raw = raw.rstrip()[: raw.rstrip().rfind("```")]
    # Find JSON object boundaries
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end != -1 and end > start:
        raw = raw[start:end + 1]
    return raw.strip()
