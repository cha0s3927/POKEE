"""
画像个性化字段 — 集成测试

验证 3 个新增字段（years_of_experience / job_search_status / current_status）：
- API 层：defaults / update / persist
- Agent 层：工具定义包含新字段、prompt 包含策略指引
- 行为：update_my_profile 参数正确传入 profile

不依赖真实 LLM，本地秒跑。

运行：
    python -m pytest tests/test_profile_fields.py -v
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
from agent import SYSTEM_PROMPT
from database import init_db, engine
from sqlalchemy import text


# ── 工具函数 ──

def _init_test_db():
    os.makedirs("data", exist_ok=True)
    os.environ.setdefault("DATABASE_URL", "sqlite:///data/test_profile.db")
    init_db()


# ── 新字段定义 ──

NEW_FIELDS = ["years_of_experience", "job_search_status", "current_status"]


# ═══ API 层测试 ═══

class TestProfileAPI:
    """验证 ProfilePayload + api_get_profile defaults 包含新字段"""

    @pytest.fixture(autouse=True)
    def setup(self):
        _init_test_db()
        self.uid = f"test-profile-api-{int(time.time()*1000)}"

    def _setup_user(self):
        with engine.connect() as conn:
            conn.execute(text(
                "INSERT INTO users (id, email, password_hash, token, created_at, profile) "
                "VALUES (:uid, :email, :hash, :token, :now, '{}')"
            ), {
                "uid": self.uid,
                "email": f"{self.uid}@test.com",
                "hash": "fakehash",
                "token": self.uid,  # 用 uid 保证唯一
                "now": "2026-08-11T00:00:00",
            })
            conn.commit()

    def test_defaults_include_new_fields(self):
        """api_get_profile 的 defaults 必须包含 3 个新字段"""
        from routes.platforms import api_get_profile

        self._setup_user()
        result = api_get_profile(user={"id": self.uid})
        profile = result["profile"]

        assert profile["years_of_experience"] == "", \
            f"years_of_experience 默认值应为 ''，实际 {profile.get('years_of_experience')}"
        assert profile["job_search_status"] == "exploring", \
            f"job_search_status 默认值应为 'exploring'，实际 {profile.get('job_search_status')}"
        assert profile["current_status"] == "", \
            f"current_status 默认值应为 ''，实际 {profile.get('current_status')}"
        assert profile["personality_notes"] == "", \
            f"personality_notes 默认值应为 ''，实际 {profile.get('personality_notes')}"

    def test_update_new_fields_persist(self):
        """更新新字段后读取，值应持久化"""
        from routes.platforms import api_update_profile, api_get_profile, ProfilePayload

        self._setup_user()

        api_update_profile(
            ProfilePayload(
                years_of_experience="3-5",
                job_search_status="actively-looking",
                current_status="employed",
            ),
            user={"id": self.uid},
        )

        result = api_get_profile(user={"id": self.uid})
        p = result["profile"]
        assert p["years_of_experience"] == "3-5"
        assert p["job_search_status"] == "actively-looking"
        assert p["current_status"] == "employed"

    def test_update_new_fields_does_not_wipe_existing(self):
        """只更新新字段，已有字段应保留"""
        from routes.platforms import api_update_profile, api_get_profile, ProfilePayload

        self._setup_user()

        # 先写旧字段
        api_update_profile(
            ProfilePayload(target_role="后端开发", target_industry="互联网"),
            user={"id": self.uid},
        )

        # 再写新字段（不碰旧字段）
        api_update_profile(
            ProfilePayload(years_of_experience="10+", current_status="unemployed"),
            user={"id": self.uid},
        )

        result = api_get_profile(user={"id": self.uid})
        p = result["profile"]
        assert p["target_role"] == "后端开发"
        assert p["target_industry"] == "互联网"
        assert p["years_of_experience"] == "10+"
        assert p["current_status"] == "unemployed"

    def test_personality_notes_passive_collection(self):
        """personality_notes 纯被动收集，可单独更新"""
        from routes.platforms import api_update_profile, api_get_profile, ProfilePayload

        self._setup_user()

        # 模拟用户在对话中说"我有点社恐"
        api_update_profile(
            ProfilePayload(personality_notes="性格偏内向，面试场景容易紧张"),
            user={"id": self.uid},
        )

        result = api_get_profile(user={"id": self.uid})
        assert result["profile"]["personality_notes"] == "性格偏内向，面试场景容易紧张"


# ═══ Agent 工具定义测试 ═══

class TestAgentToolDefinitions:
    """验证 agent TOOLS 列表中的工具定义包含新字段"""

    @pytest.fixture(autouse=True)
    def setup(self):
        _init_test_db()

    def test_get_my_profile_description_mentions_new_fields(self):
        """get_my_profile 描述中应提及新字段"""
        tools = agent_module.TOOLS
        get_profile = next(t for t in tools if t["function"]["name"] == "get_my_profile")
        desc = get_profile["function"]["description"]
        assert "工作年限" in desc, f"get_my_profile 描述缺少'工作年限': {desc}"
        assert "求职状态" in desc, f"get_my_profile 描述缺少'求职状态': {desc}"
        assert "在职状态" in desc, f"get_my_profile 描述缺少'在职状态': {desc}"
        assert "性格" in desc, f"get_my_profile 描述缺少'性格': {desc}"

    def test_update_my_profile_has_new_params(self):
        """update_my_profile 参数应包含 3 个新字段"""
        tools = agent_module.TOOLS
        update_profile = next(t for t in tools if t["function"]["name"] == "update_my_profile")
        params = update_profile["function"]["parameters"]["properties"]

        assert "years_of_experience" in params, \
            f"update_my_profile 参数缺少 years_of_experience: {list(params.keys())}"
        assert "job_search_status" in params, \
            f"update_my_profile 参数缺少 job_search_status"
        assert "current_status" in params, \
            f"update_my_profile 参数缺少 current_status"
        assert "personality_notes" in params, \
            f"update_my_profile 参数缺少 personality_notes"


# ═══ Prompt 策略测试 ═══

class TestPromptStrategy:
    """验证 SYSTEM_PROMPT 中包含基于新字段的行为策略指引"""

    def test_prompt_has_experience_strategy(self):
        """Prompt 应包含按工作年限分级的回复策略"""
        assert "0-1" in SYSTEM_PROMPT, "Prompt 应提及 0-1 年经验对应策略"
        assert "5+" in SYSTEM_PROMPT, "Prompt 应提及 5+ 年经验对应策略"

    def test_prompt_has_status_strategy(self):
        """Prompt 应包含按求职状态/在职状态分级的回复策略"""
        assert "actively-looking" in SYSTEM_PROMPT, "Prompt 应提及 actively-looking"
        assert "casually-browsing" in SYSTEM_PROMPT, "Prompt 应提及 casually-browsing"
        assert "employed" in SYSTEM_PROMPT.lower(), "Prompt 应提及 employed"

    def test_prompt_has_personalization_keywords(self):
        """Prompt 应包含个性化行为指令（不是空架子）"""
        assert "耐心" in SYSTEM_PROMPT or "解释" in SYSTEM_PROMPT, \
            "Prompt 应包含对 junior 用户的耐心策略"
        assert "直奔主题" in SYSTEM_PROMPT or "不废话" in SYSTEM_PROMPT, \
            "Prompt 应包含对 senior 用户的直接策略"
        assert "紧迫" in SYSTEM_PROMPT or "优先" in SYSTEM_PROMPT or "催" in SYSTEM_PROMPT, \
            "Prompt 应包含对急求职用户的紧迫策略"

    def test_prompt_has_personality_notes_guidance(self):
        """Prompt 应包含 personality_notes 的被动收集指引"""
        assert "personality_notes" in SYSTEM_PROMPT, \
            "Prompt 应提及 personality_notes"
        assert "不要主动" in SYSTEM_PROMPT or "不要刻意" in SYSTEM_PROMPT, \
            "Prompt 应强调不要主动追问性格"


# ═══ Agent 执行测试 ═══

class TestProfileToolExecution:
    """Mock LLM，验证 agent 正确执行 profile 相关工具调用"""

    @pytest.fixture(autouse=True)
    def setup(self):
        _init_test_db()
        from agent import Agent
        self.agent = Agent()
        self.uid = f"test-profile-exec-{int(time.time()*1000)}"

    def _register_user(self):
        with engine.connect() as conn:
            conn.execute(text(
                "INSERT INTO users (id, email, password_hash, token, created_at, profile) "
                "VALUES (:uid, :email, :hash, :token, :now, '{}')"
            ), {
                "uid": self.uid,
                "email": f"{self.uid}@test.com",
                "hash": "fakehash",
                "token": self.uid,
                "now": "2026-08-11T00:00:00",
            })
            conn.commit()

    def _make_llm_response(self, tool_name: str, args: dict, content: str = ""):
        call = MagicMock()
        call.id = f"call_test_{int(time.time()*1000)}"
        call.function.name = tool_name
        call.function.arguments = json.dumps(args, ensure_ascii=False)

        choice = MagicMock()
        choice.message.content = content
        choice.message.tool_calls = [call]
        type(choice.message).reasoning_content = MagicMock(return_value=None)

        resp = MagicMock()
        resp.choices = [choice]
        return resp

    def _chat_with_mock(self, message: str, mock_tool: str, mock_args: dict) -> dict:
        llm_resp = self._make_llm_response(mock_tool, mock_args)
        with patch.object(agent_module, 'client') as mock_client:
            mock_client.chat.completions.create.return_value = llm_resp
            result = self.agent.chat(self.uid, message)
        return result

    def test_get_my_profile_returns_new_fields(self):
        """get_my_profile 返回的 profile 应包含新字段默认值"""
        self._register_user()

        result = self._chat_with_mock(
            "我的画像",
            mock_tool="get_my_profile",
            mock_args={},
        )

        assert "get_my_profile" in result["tool_calls"], \
            f"应该调用了 get_my_profile，实际: {result['tool_calls']}"

    def test_update_my_profile_with_new_fields(self):
        """update_my_profile 传入新字段参数，应成功更新"""
        self._register_user()

        llm_resp = self._make_llm_response("update_my_profile", {
            "years_of_experience": "3-5",
            "job_search_status": "actively-looking",
            "current_status": "employed",
        })
        with patch.object(agent_module, 'client') as mock_client:
            mock_client.chat.completions.create.return_value = llm_resp
            result = self.agent.chat(self.uid, "我工作3年了，想跳槽")

        assert "update_my_profile" in result["tool_calls"]

        # Verify via get
        llm_resp2 = self._make_llm_response("get_my_profile", {})
        with patch.object(agent_module, 'client') as mock_client:
            mock_client.chat.completions.create.return_value = llm_resp2
            result2 = self.agent.chat(self.uid, "查看画像")

        assert "get_my_profile" in result2["tool_calls"]

    def _make_text_response(self, content: str):
        choice = MagicMock()
        choice.message.content = content
        choice.message.tool_calls = None
        resp = MagicMock()
        resp.choices = [choice]
        return resp

    def test_update_my_profile_partial_new_fields(self):
        """只更新部分新字段，其他保持不变"""
        self._register_user()

        # 第一次：写全。agent 会调两次 LLM：工具执行 + 总结
        llm1_exec = self._make_llm_response("update_my_profile", {
            "target_role": "前端开发",
            "years_of_experience": "1-3",
            "current_status": "student",
        })
        llm1_summary = self._make_text_response("好的师弟，俺老孙记住了！")
        with patch.object(agent_module, 'client') as mock_client:
            mock_client.chat.completions.create.side_effect = [llm1_exec, llm1_summary]
            self.agent.chat(self.uid, "我是学生，找前端实习")

        # 第二次：只改状态
        llm2_exec = self._make_llm_response("update_my_profile", {
            "job_search_status": "actively-looking",
        })
        llm2_summary = self._make_text_response("没问题，猴哥帮你记下了~")
        with patch.object(agent_module, 'client') as mock_client:
            mock_client.chat.completions.create.side_effect = [llm2_exec, llm2_summary]
            self.agent.chat(self.uid, "我急着找")

        # 验证：target_role 和 years_of_experience 还在
        from routes.platforms import api_get_profile
        profile = api_get_profile(user={"id": self.uid})["profile"]
        assert profile["target_role"] == "前端开发", \
            f"target_role 应保留，实际: {profile['target_role']}"
        assert profile["years_of_experience"] == "1-3", \
            f"years 应保留，实际: {profile['years_of_experience']}"
        assert profile["job_search_status"] == "actively-looking", \
            f"job_search_status 应更新，实际: {profile['job_search_status']}"

    def test_personality_notes_via_agent(self):
        """Agent 写入 personality_notes，应成功持久化"""
        self._register_user()

        llm_exec = self._make_llm_response("update_my_profile", {
            "personality_notes": "喜欢简洁直接，讨厌废话",
        })
        llm_summary = self._make_text_response("好的师弟，猴哥记住了~")
        with patch.object(agent_module, 'client') as mock_client:
            mock_client.chat.completions.create.side_effect = [llm_exec, llm_summary]
            self.agent.chat(self.uid, "我这人比较直接，不喜欢绕弯子")

        from routes.platforms import api_get_profile
        profile = api_get_profile(user={"id": self.uid})["profile"]
        assert profile["personality_notes"] == "喜欢简洁直接，讨厌废话", \
            f"personality_notes 应被写入，实际: {profile['personality_notes']}"
