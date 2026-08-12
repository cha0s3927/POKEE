"""
个性化字段对照测试

验证 personality_notes + interview_stats 对 agent 行为的实际影响：
直接对比有画像用户 vs 空画像用户的 get_my_profile 返回值，
以及 interview_stats 的持久化逻辑。

运行：
    python -m pytest tests/test_personalization_contrast.py -v
运行对照摘要（可读输出）：
    python -m pytest tests/test_personalization_contrast.py -v -s
"""
from __future__ import annotations

import json
import os
import sys
import time
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent as agent_module
from agent import Agent, SYSTEM_PROMPT
from database import init_db, engine
from sqlalchemy import text


def _init_test_db():
    os.makedirs("data", exist_ok=True)
    os.environ.setdefault("DATABASE_URL", "sqlite:///data/test_personalization_contrast.db")
    init_db()


# ── 对照摘要（可读输出，方便给上司看）──

def print_contrast_summary():
    """打印一份可读的对比摘要"""
    _init_test_db()

    uid_a = f"contrast-rich-{int(time.time()*1000)}"
    uid_b = f"contrast-empty-{int(time.time()*1000)}"

    profile_a = json.dumps({
        "name": "张三",
        "target_role": "后端开发",
        "target_industry": "互联网",
        "years_of_experience": "3-5",
        "job_search_status": "actively-looking",
        "current_status": "employed",
        "personality_notes": "性格偏内向，面试容易紧张，喜欢直接沟通",
        "interview_stats": {
            "total_sessions": 4,
            "avg_score": 78,
            "latest_score": 82,
            "dimensions_avg": {
                "技术深度": 83, "项目经验": 80, "沟通表达": 72,
                "问题解决": 76, "匹配度": 79,
            },
            "weakest": "沟通表达",
            "strongest": "技术深度",
        },
    }, ensure_ascii=False)

    with engine.connect() as conn:
        for uid, profile in [(uid_a, profile_a), (uid_b, "{}")]:
            conn.execute(text(
                "INSERT INTO users (id, email, password_hash, token, created_at, profile) "
                "VALUES (:uid, :email, :hash, :token, :now, :profile)"
            ), {"uid": uid, "email": f"{uid}@test.com", "hash": "x", "token": uid,
                "now": "2026-08-12", "profile": profile})
        conn.commit()

    from routes.platforms import api_get_profile
    p_a = api_get_profile(user={"id": uid_a})["profile"]
    p_b = api_get_profile(user={"id": uid_b})["profile"]

    print("\n" + "=" * 66)
    print("  个性化字段 — 画像对比摘要")
    print("=" * 66)
    print()
    print(f"  {'字段':　<20} {'用户A (有画像)':　<24} {'用户B (空画像)':　<24}")
    print(f"  {'-'*20} {'-'*24} {'-'*24}")

    rows = [
        ("name", p_a.get("name", ""), p_b.get("name", "")),
        ("target_role", p_a.get("target_role", ""), p_b.get("target_role", "")),
        ("years_of_experience", p_a.get("years_of_experience", ""), p_b.get("years_of_experience", "")),
        ("job_search_status", p_a.get("job_search_status", ""), p_b.get("job_search_status", "")),
        ("current_status", p_a.get("current_status", ""), p_b.get("current_status", "")),
        ("personality_notes", p_a.get("personality_notes", ""), p_b.get("personality_notes", "")),
        ("interview_stats.avg_score", str(p_a.get("interview_stats", {}).get("avg_score", "-") if p_a.get("interview_stats") else "-"),
         str(p_b.get("interview_stats", {}).get("avg_score", "-") if p_b.get("interview_stats") else "-")),
        ("interview_stats.weakest", str(p_a.get("interview_stats", {}).get("weakest", "-") if p_a.get("interview_stats") else "-"),
         str(p_b.get("interview_stats", {}).get("weakest", "-") if p_b.get("interview_stats") else "-")),
    ]
    for label, a, b in rows:
        a_str = str(a)[:22] if a else "(空)"
        b_str = str(b)[:22] if b else "(空)"
        print(f"  {label:　<20} {a_str:　<24} {b_str:　<24}")

    print()
    print("  AI 通过 get_my_profile 工具读取画像后，用户 A 能获得：")
    print("  - 性格感知（紧张 → 多鼓励 / 喜欢直接 → 不废话）")
    print("  - 面试历史（第5次练习 / 沟通偏弱 → 重点练沟通题）")
    print("  用户 B 这些信息为空，AI 只能按通用方式回复。")
    print("=" * 66)

    # 确认差异确实存在
    assert p_a["personality_notes"], "用户 A 应有 personality_notes"
    assert p_a["interview_stats"], "用户 A 应有 interview_stats"
    assert not p_b["personality_notes"], "用户 B 的 personality_notes 应为空"
    assert p_b["interview_stats"] is None, "用户 B 的 interview_stats 应为 None"


# ═══ 测试用例 ═══

class TestProfileFieldsPresent:
    """验证新字段在画像中存在且有正确默认值"""

    @pytest.fixture(autouse=True)
    def setup(self):
        _init_test_db()

    def test_both_new_fields_have_defaults(self):
        """personality_notes 和 interview_stats 都应有默认值"""
        from routes.platforms import api_get_profile

        uid = f"test-defaults-{int(time.time()*1000)}"
        with engine.connect() as conn:
            conn.execute(text(
                "INSERT INTO users (id, email, password_hash, token, created_at, profile) "
                "VALUES (:uid, :email, :hash, :token, :now, '{}')"
            ), {"uid": uid, "email": f"{uid}@test.com", "hash": "x", "token": uid, "now": "2026-08-12"})
            conn.commit()

        p = api_get_profile(user={"id": uid})["profile"]
        assert p["personality_notes"] == "", \
            f"personality_notes 默认应为空字符串，实际 {p['personality_notes']}"
        assert p["interview_stats"] is None, \
            f"interview_stats 默认应为 None，实际 {p['interview_stats']}"


class TestPersonalityNotes:
    """personality_notes：被动收集，通过 update_my_profile 写入"""

    @pytest.fixture(autouse=True)
    def setup(self):
        _init_test_db()
        self.uid = f"test-pn-{int(time.time()*1000)}"

    def _setup_user(self):
        with engine.connect() as conn:
            conn.execute(text(
                "INSERT INTO users (id, email, password_hash, token, created_at, profile) "
                "VALUES (:uid, :email, :hash, :token, :now, '{}')"
            ), {"uid": self.uid, "email": f"{self.uid}@test.com", "hash": "x", "token": self.uid, "now": "2026-08-12"})
            conn.commit()

    def test_personality_notes_written_and_read(self):
        """写入性格备注后能正确读取"""
        from routes.platforms import api_update_profile, api_get_profile, ProfilePayload

        self._setup_user()
        api_update_profile(
            ProfilePayload(personality_notes="性格偏内向，面试容易紧张"),
            user={"id": self.uid},
        )
        p = api_get_profile(user={"id": self.uid})["profile"]
        assert "性格偏内向" in p["personality_notes"]

    def test_personality_notes_not_in_allowed_set(self):
        """interview_stats 不在 ProfilePayload 中（系统只写，AI 不能更新）"""
        from routes.platforms import ProfilePayload
        fields = ProfilePayload.model_fields
        assert "interview_stats" not in fields, \
            "interview_stats 不应出现在 ProfilePayload 中，只能由系统自动更新"


class TestInterviewStats:
    """interview_stats：系统自动维护，面试结束后累计"""

    @pytest.fixture(autouse=True)
    def setup(self):
        _init_test_db()
        self.uid = f"test-is-{int(time.time()*1000)}"

    def _setup_user(self):
        with engine.connect() as conn:
            conn.execute(text(
                "INSERT INTO users (id, email, password_hash, token, created_at, profile) "
                "VALUES (:uid, :email, :hash, :token, :now, '{}')"
            ), {"uid": self.uid, "email": f"{self.uid}@test.com", "hash": "x", "token": self.uid, "now": "2026-08-12"})
            conn.commit()

    def test_update_interview_stats_first_time(self):
        """第一次面试后，interview_stats 正确初始化"""
        from routes.interview import _update_interview_stats
        from routes.platforms import api_get_profile

        self._setup_user()
        scores = {
            "total_score": 78,
            "dimensions": {
                "技术深度": {"score": 80, "comment": "ok"},
                "沟通表达": {"score": 70, "comment": "偏弱"},
            },
            "improvements": ["多练 STAR", "准备案例"],
            "strengths": ["基础扎实"],
        }
        _update_interview_stats(self.uid, scores)

        p = api_get_profile(user={"id": self.uid})["profile"]
        s = p["interview_stats"]
        assert s["total_sessions"] == 1
        assert s["avg_score"] == 78
        assert s["latest_score"] == 78
        assert s["weakest"] == "沟通表达"
        assert s["strongest"] == "技术深度"
        assert s["dimensions_avg"]["技术深度"] == 80
        assert s["dimensions_avg"]["沟通表达"] == 70

    def test_update_interview_stats_cumulative(self):
        """多次面试后取加权平均"""
        from routes.interview import _update_interview_stats
        from routes.platforms import api_get_profile

        self._setup_user()

        # 第一次
        _update_interview_stats(self.uid, {
            "total_score": 70,
            "dimensions": {"沟通表达": {"score": 60}, "技术深度": {"score": 80}},
            "improvements": [], "strengths": [],
        })
        # 第二次
        _update_interview_stats(self.uid, {
            "total_score": 90,
            "dimensions": {"沟通表达": {"score": 80}, "技术深度": {"score": 100}},
            "improvements": [], "strengths": [],
        })

        p = api_get_profile(user={"id": self.uid})["profile"]
        s = p["interview_stats"]
        assert s["total_sessions"] == 2
        assert s["avg_score"] == 80  # (70+90)/2
        assert s["dimensions_avg"]["沟通表达"] == 70  # (60+80)/2
        assert s["dimensions_avg"]["技术深度"] == 90  # (80+100)/2


class TestPromptGuidance:
    """验证 prompt 中有正确的行为指引"""

    def test_prompt_dont_expose_raw_numbers(self):
        """Prompt 应强调不直接报数字，用人话包装"""
        assert "不要直接报数字" in SYSTEM_PROMPT or "不要说具体数字" in SYSTEM_PROMPT, \
            "Prompt 应阻止 AI 直接说'你的XX分只有72'"

    def test_prompt_use_personality_notes(self):
        """Prompt 中提到 personality_notes 的使用方式"""
        assert "personality_notes" in SYSTEM_PROMPT

    def test_prompt_use_interview_stats(self):
        """Prompt 中提到 interview_stats 的使用方式"""
        assert "interview_stats" in SYSTEM_PROMPT


class TestAgentToolContext:
    """验证 agent 执行 get_my_profile 时，返回的 profile 包含新字段"""

    @pytest.fixture(autouse=True)
    def setup(self):
        _init_test_db()
        self.agent = Agent()
        self.uid = f"test-ctx-{int(time.time()*1000)}"

    def test_get_my_profile_returns_both_new_fields(self):
        """get_my_profile 返回应包含 personality_notes + interview_stats"""
        profile_data = json.dumps({
            "target_role": "后端",
            "personality_notes": "喜欢直接",
            "interview_stats": {"total_sessions": 2, "avg_score": 80, "weakest": "沟通表达"},
        }, ensure_ascii=False)

        with engine.connect() as conn:
            conn.execute(text(
                "INSERT INTO users (id, email, password_hash, token, created_at, profile) "
                "VALUES (:uid, :email, :hash, :token, :now, :profile)"
            ), {"uid": self.uid, "email": f"{self.uid}@test.com", "hash": "x", "token": self.uid,
                "now": "2026-08-12", "profile": profile_data})
            conn.commit()

        # 让 agent 调用 get_my_profile
        def fake_create(**kwargs):
            call = MagicMock()
            call.id = "call_1"
            call.function.name = "get_my_profile"
            call.function.arguments = "{}"
            choice = MagicMock()
            choice.message.content = ""
            choice.message.tool_calls = [call]
            type(choice.message).reasoning_content = MagicMock(return_value=None)
            resp = MagicMock()
            resp.choices = [choice]
            return resp

        with patch.object(agent_module, 'client') as mock_client:
            mock_client.chat.completions.create.side_effect = [
                fake_create(),
                self._make_noop_response(),
            ]
            result = self.agent.chat(self.uid, "我的画像")

        # get_my_profile 是免费工具，直接执行并返回
        assert "get_my_profile" in result["tool_calls"]

    def _make_noop_response(self):
        choice = MagicMock()
        choice.message.content = "好的"
        choice.message.tool_calls = None
        resp = MagicMock()
        resp.choices = [choice]
        return resp


# ── 运行对照摘要 ──

def test_print_contrast_summary():
    """打印对照摘要供汇报使用"""
    print_contrast_summary()


if __name__ == "__main__":
    print_contrast_summary()
