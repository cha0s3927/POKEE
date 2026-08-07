"""
Multi-agent mock interview engine.
Adapted from offerMaster: 出题官 → 追问决策官 → 评分官
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

import httpx
from openai import OpenAI

from config import settings

logger = logging.getLogger(__name__)

client = OpenAI(
    api_key=settings.llm_api_key,
    base_url=settings.llm_base_url,
    http_client=httpx.Client(
        transport=httpx.HTTPTransport(retries=0),
        timeout=httpx.Timeout(90.0, connect=10.0),
    ),
)

# ── Prompts ──

MATCH_CHECK_SYSTEM = """你是招聘专家。快速评估候选人的简历与目标岗位的匹配度。

根据简历内容和目标岗位（以及 JD，如有），判断这份简历有多大机会通过该岗位的简历筛选。

返回 JSON:
{
  "score": 65,
  "level": "medium",
  "note": "简历以量化交易为主，与Java后端岗位的技能栈有交集（Python/数据处理），但缺少Java相关经验。如果JD中接受转语言可加分。",
  "suggestion": "建议在面试中侧重考察候选人的计算机基础和学习能力，而非特定语言经验。"
}

level: high(70-100) / medium(40-69) / low(0-39)
note: 2-3句话说明匹配点和风险点
suggestion: 给面试官的出题建议"""

PLAN_SYSTEM = """你是资深面试官。根据岗位要求设计面试题，简历仅作参考。

## 出题优先级
1. **JD 中有明确技术栈要求** → 围绕这些技术出题（如 JD 要求 Java/Spring，就出 Java 相关题）
2. **只有岗位名称没有 JD** → 按该岗位的通用面试标准出题（如"Java后端"就出 JVM/并发/框架题）
3. **简历中的经验** → 用作题目深度的参考（候选人熟悉什么就深挖什么），但不作为出题方向

## 出题规则
- 覆盖 4 类：技术能力（2题）、项目深挖（1题）、行为面试（1题）、问题解决（1题）
- 技术题目必须围绕目标岗位的技术栈，不是围绕候选人的技术栈
- 如果候选人简历缺少岗位所需技能，技术题仍要出——真实面试就是这样的
- 每道题包含：主问题 + 2-3 个追问方向 + 期望听到的要点
- 按要求的题数出题

返回 JSON（不要 markdown 代码块）:
{
  "questions": [
    {
      "id": 1,
      "category": "技术能力",
      "question": "主问题",
      "followup_hints": ["追问1", "追问2"],
      "expected_points": ["要点1", "要点2"]
    }
  ]
}"""

DECIDE_SYSTEM = """你是面试官，评估候选人回答后决定下一步。

决策规则：
- 回答笼统缺细节 → 追问具体细节（追问要和原问题相关，不要跳话题）
- 回答有亮点但没展开 → 追问让展开
- 回答暴露矛盾 → 追问澄清
- 回答完整充分 → 进入下一题
- 每题最多追问 2 次，第 3 次必须进入下一题

返回 JSON:
{
  "action": "followup",
  "message": "追问内容（自然口语，像面试官在对话）",
  "reason": "简短理由"
}

或

{
  "action": "next",
  "message": "好的，我们聊下一个话题。",
  "reason": "简短理由"
}"""

SCORE_SYSTEM = """你是资深面试评估官。根据整场面试的问答记录，给出综合评分。

评分维度（总分 100）：
- 技术深度 (25%): 技术原理理解、架构思考
- 项目经验 (25%): 项目复杂度、个人贡献
- 沟通表达 (20%): 逻辑清晰度、结构化表达
- 问题解决 (15%): 分析思路、方案合理性
- 匹配度 (15%): 与岗位要求的契合度

返回 JSON:
{
  "total_score": 82,
  "dimensions": {
    "技术深度": {"score": 80, "comment": "短评"},
    "项目经验": {"score": 85, "comment": "短评"},
    "沟通表达": {"score": 78, "comment": "短评"},
    "问题解决": {"score": 83, "comment": "短评"},
    "匹配度": {"score": 85, "comment": "短评"}
  },
  "overall": "2-3 句综合评价",
  "strengths": ["亮点1", "亮点2"],
  "improvements": ["改进建议1", "改进建议2"]
}"""


def _call_llm(system: str, user: str, temperature: float = 0.3,
              max_tokens: int = 4096) -> dict:
    resp = client.chat.completions.create(
        model=settings.llm_model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=temperature,
        max_tokens=max_tokens,
    )
    text = resp.choices[0].message.content or ""
    finish_reason = resp.choices[0].finish_reason or ""
    text = text.strip()
    # Strip markdown code fences
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:])
        if text.endswith("```"):
            text = text[:-3]
    text = text.strip()

    def _try_parse(t: str) -> dict:
        try:
            return json.loads(t)
        except json.JSONDecodeError:
            pass
        # Try to extract JSON from the response
        import re
        m = re.search(r'\{[\s\S]*\}', t)
        if m:
            try:
                return json.loads(m.group())
            except json.JSONDecodeError:
                pass
        raise ValueError(f"Failed to parse LLM response as JSON: {t[:300]}")

    # If truncated, try to repair by closing open brackets
    if finish_reason == "length" or (text.count("{") > text.count("}")):
        logger.warning("LLM response appears truncated (finish_reason=%s), attempting repair", finish_reason)
        # Count and close unclosed structures
        open_braces = text.count("{") - text.count("}")
        open_brackets = text.count("[") - text.count("]")
        repairs = "}" * open_braces + "]" * open_brackets
        if text.rstrip().endswith(","):
            text = text.rstrip()[:-1]  # Remove trailing comma before closing
        text = text + repairs
        try:
            return _try_parse(text)
        except ValueError:
            pass  # Repair failed, fall through to retry with more tokens

    try:
        return _try_parse(text)
    except ValueError:
        if max_tokens < 8192:
            logger.warning("JSON parse failed, retrying with more tokens")
            return _call_llm(system, user, temperature, max_tokens=8192)
        raise


def check_match(resume_md: str, position: str, jd_text: str = "") -> dict:
    """匹配度检查：评估简历与目标岗位的匹配程度."""
    user = f"## 目标岗位\n{position}\n\n## 候选人简历\n{resume_md[:3000]}"
    if jd_text and len(jd_text) > 20:
        user += f"\n\n## 岗位 JD\n\n{jd_text[:3000]}"
    return _call_llm(MATCH_CHECK_SYSTEM, user, temperature=0.2)


def plan_questions(resume_md: str, position: str = "", jd_text: str = "",
                   match_note: str = "", question_count: int = 5) -> list[dict]:
    """出题官：以岗位要求为主、简历为辅，生成面试题."""
    cat_count = max(1, question_count // 5 + (1 if question_count % 5 > 0 else 0))
    user = f"## 目标岗位\n{position or '通用岗位'}\n\n## 要求\n生成 {question_count} 道面试题。"
    if jd_text and len(jd_text) > 20:
        user += f"\n\n## 岗位 JD\n\n{jd_text[:3000]}"
    else:
        user += f"\n\n（无 JD，请按「{position}」岗位的通用面试标准出题，覆盖该岗位核心技术栈）"
    user += f"\n\n## 候选人简历（仅作深度参考，不作为出题方向）\n\n{resume_md[:3000]}"
    if match_note:
        user += f"\n\n## 匹配度参考\n{match_note}"
    return _call_llm(PLAN_SYSTEM, user).get("questions", [])


def decide_followup(question: dict, answer: str, followup_count: int,
                    history: list[dict]) -> dict:
    """追问决策官：判断追问还是下一题."""
    q_text = question.get("question", "")
    hints = question.get("followup_hints", [])
    cat = question.get("category", "")
    qid = question.get("id", "?")

    user = f"## 当前题目 (第{qid}题, {cat})\n{q_text}\n\n## 追问方向\n" + \
           "\n".join(f"- {h}" for h in hints) + \
           f"\n\n## 候选人回答\n{answer[:2000]}" + \
           f"\n\n## 本题已追问次数\n{followup_count}/2"

    if history:
        hist_text = "\n\n## 对话历史\n" + "\n".join(
            f"- {h['role']}: {h['content'][:200]}" for h in history[-6:]
        )
        user += hist_text

    result = _call_llm(DECIDE_SYSTEM, user, temperature=0.2)
    return result


def score_interview(qa_log: list[dict], position: str = "") -> dict:
    """评分官：综合评估整场面试."""
    transcript = []
    for i, entry in enumerate(qa_log):
        q = entry.get("question", "")
        a = entry.get("answer", "")
        transcript.append(f"Q{i+1}: {q}\nA{i+1}: {a}")

    user = f"## 面试岗位\n{position or '通用岗位'}\n\n" + \
           "## 面试记录\n\n" + "\n\n".join(transcript)

    result = _call_llm(SCORE_SYSTEM, user, temperature=0.3)
    return result


# ── State Machine ──

@dataclass
class InterviewSession:
    user_id: str
    position: str = ""
    company: str = ""
    questions: list = field(default_factory=list)
    current_idx: int = 0
    followup_count: int = 0
    qa_log: list = field(default_factory=list)
    history: list = field(default_factory=list)
    status: str = "idle"  # idle → ready → asking → waiting → done

    def start(self, resume_md: str, jd_text: str = "",
              position: str = "", company: str = "", force: bool = False,
              question_count: int = 5):
        self.position = position or "通用岗位"
        self.company = company

        self._match_score = 0
        self._match_note = ""
        self._match_suggestion = ""

        try:
            self.questions = plan_questions(
                resume_md, self.position, jd_text, "", question_count,
            )
        except Exception as e:
            logger.exception("plan_questions failed")
            raise ValueError(f"生成面试题失败: {e}") from e
        self.current_idx = 0
        self.followup_count = 0
        self.qa_log = []
        self.history = []
        self.status = "ready"

    def first_question(self) -> str | None:
        if not self.questions:
            return None
        q = self.questions[0]
        self.status = "waiting"
        intro = f"面试开始。岗位：**{self.position}**"
        if self.company:
            intro += f" @ **{self.company}**"
        intro += f"\n\n共 {len(self.questions)} 道题，准备好了就开始。\n\n**Q1** [{q.get('category', '')}] {q.get('question', '')}"
        self.history.append({"role": "assistant", "content": intro})
        return intro

    def handle_answer(self, answer: str) -> str | None:
        if self.status != "waiting":
            return None

        q = self.questions[self.current_idx]
        self.qa_log.append({
            "question": q.get("question", ""),
            "category": q.get("category", ""),
            "answer": answer,
        })
        self.history.append({"role": "user", "content": answer})

        try:
            decision = decide_followup(q, answer, self.followup_count, self.history)
        except Exception as e:
            logger.exception("decide_followup failed")
            decision = {"action": "next", "message": "好的，我们进入下一题。", "reason": f"决策失败: {e}"}

        action = decision.get("action", "next")
        message = decision.get("message", "好的，进入下一题。")

        if action == "followup" and self.followup_count < 2:
            self.followup_count += 1
            msg = f"**追问** {message}"
            self.history.append({"role": "assistant", "content": msg})
            return msg
        else:
            self.followup_count = 0
            self.current_idx += 1

            if self.current_idx >= len(self.questions):
                self.status = "done"
                report = self._generate_report()
                self.history.append({"role": "assistant", "content": report})
                return report

            next_q = self.questions[self.current_idx]
            n = self.current_idx + 1
            msg = f"{message}\n\n**Q{n}** [{next_q.get('category', '')}] {next_q.get('question', '')}"
            self.status = "waiting"
            self.history.append({"role": "assistant", "content": msg})
            return msg

    def _generate_report(self) -> str:
        if not self.qa_log:
            return "面试结束。没有足够的问答记录生成报告。"

        try:
            result = score_interview(self.qa_log, self.position)
        except Exception as e:
            logger.exception("score_interview error")
            return f"面试结束！共完成 {len(self.qa_log)} 道题。评分生成失败，请重试。"

        lines = [
            "## 面试结束 — 评估报告",
            "",
            f"### 综合评分: {result.get('total_score', 'N/A')}/100",
            "",
            "| 维度 | 得分 | 评价 |",
            "|------|------|------|",
        ]
        for dim, detail in result.get("dimensions", {}).items():
            s = detail.get("score", "-")
            c = detail.get("comment", "")
            lines.append(f"| {dim} | {s} | {c} |")

        overall = result.get("overall", "")
        if overall:
            lines.append(f"\n{overall}")

        strengths = result.get("strengths", [])
        if strengths:
            lines.append("\n### 亮点")
            for s in strengths:
                lines.append(f"- {s}")

        improvements = result.get("improvements", [])
        if improvements:
            lines.append("\n### 改进建议")
            for imp in improvements:
                lines.append(f"- {imp}")

        lines.append(f"\n共完成 {len(self.qa_log)} 道题 + 追问，面试结束。")

        return "\n".join(lines)


# ── Global session store ──
_sessions: dict[str, InterviewSession] = {}


def get_session(user_id: str) -> InterviewSession:
    if user_id not in _sessions:
        _sessions[user_id] = InterviewSession(user_id=user_id)
    return _sessions[user_id]


def clear_session(user_id: str):
    _sessions.pop(user_id, None)
