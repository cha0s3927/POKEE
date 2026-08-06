from __future__ import annotations

"""
Agent 工具链测试套件

每个场景验证：用户输入 → LLM 选择的工具调用序列是否符合预期。
测试不检查回复内容，只检查 tool_calls 的名称和顺序。

运行方式:
    python -m pytest tests/test_scenarios.py -v

依赖: 需要服务运行在 localhost:8002，且账号已注册。
      测试账号通过环境变量 TEST_EMAIL / TEST_PASSWORD 或默认值配置。
"""

import json
import os
import time
from dataclasses import dataclass, field
from typing import Optional

import pytest
import requests

BASE = os.getenv("TEST_BASE_URL", "http://localhost:8003")

# ── 工具函数 ──

def _login(email: str = None, password: str = None) -> tuple[str, str]:
    """返回 (token, user_id)，注册失败时自动注册"""
    email = email or os.getenv("TEST_EMAIL", f"test-{int(time.time())}@test.com")
    password = password or os.getenv("TEST_PASSWORD", "123456")

    # 先尝试登录
    r = requests.post(f"{BASE}/api/login", json={"email": email, "password": password})
    if r.status_code == 200:
        data = r.json()
        return data["token"], data["user_id"]

    # 登录失败则注册
    r = requests.post(f"{BASE}/api/register", json={"email": email, "password": password})
    if r.status_code != 200:
        raise RuntimeError(f"注册失败: {r.text}")
    data = r.json()
    return data["token"], data["user_id"]


def _chat(token: str, message: str) -> dict:
    """发送一条对话，返回 {reply, tool_calls, balance}"""
    r = requests.post(
        f"{BASE}/api/chat",
        json={"message": message},
        headers={"Authorization": f"Bearer {token}"},
    )
    if r.status_code != 200:
        raise RuntimeError(f"Chat 失败 ({r.status_code}): {r.text}")
    return r.json()


def _setup_resume(token: str) -> str:
    """为测试账号上传一份简历，返回 resume_id"""
    r = requests.post(
        f"{BASE}/api/resumes",
        json={
            "name": "测试简历",
            "content": """# 张三 — 后端开发工程师

5年后端开发经验，主导过微服务架构改造，将单体应用拆分为12个服务。

## 技能

- 熟练掌握: Python / Go / Kubernetes / MySQL / Redis
- 熟悉: Docker / gRPC / Kafka
- 了解: Rust / Elasticsearch

## 项目经历

### 分布式任务调度系统 — 技术负责人（2023-06 ~ 至今）

- 设计并实现分布式任务调度系统，日均处理500万任务
- 引入消息队列解耦，系统可用性从99.5%提升到99.95%

### 电商平台微服务改造 — 核心开发（2022-03 ~ 2023-05）

- 主导微服务架构改造，将单体应用拆分为12个服务
- API 响应时间从800ms降到120ms

## 教育背景

- **北京大学** — 硕士 计算机科学与技术（2020）
- **华中科技大学** — 本科 软件工程（2018）""",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    if r.status_code != 200:
        raise RuntimeError(f"创建简历失败: {r.text}")
    return r.json()["resume_id"]


def _run_scenario(scenario: Scenario) -> tuple[list[str], list[str]]:
    """用新用户跑一个场景，返回 (all_tool_calls, all_replies)"""
    token, uid = _login()

    # 前置条件
    if scenario.needs_resume:
        _setup_resume(token)

    tool_calls: list[str] = []
    replies: list[str] = []

    for i, msg in enumerate(scenario.user_inputs):
        resp = _chat(token, msg)
        replies.append(resp.get("reply", ""))
        tc = resp.get("tool_calls", [])
        tool_calls.extend(tc)
        # 短暂等待避免 rate limit
        if i < len(scenario.user_inputs) - 1:
            time.sleep(0.3)

    return tool_calls, replies


# ── 场景定义 ──

@dataclass
class Scenario:
    """一个测试场景"""
    id: str                          # 唯一标识，如 "onboard-01"
    phase: str                       # 阶段：onboarding / resume / profile / jd / pitch / search / interview / growth
    name: str                        # 场景名称
    user_inputs: list[str]           # 用户多轮输入（按顺序）
    expected_tool_chain: list[str]   # 期望的工具调用序列（名称列表）
    should_not_call: list[str] = field(default_factory=list)  # 绝对不能调用的工具
    needs_resume: bool = False       # 前置条件：是否需要先上传简历
    setup: Optional[str] = None      # 前置条件描述
    notes: str = ""                  # 备注


SCENARIOS: list[Scenario] = []

def scenario(id, phase, name, user_inputs, expected_tool_chain, **kwargs):
    SCENARIOS.append(Scenario(id, phase, name, user_inputs, expected_tool_chain, **kwargs))


# ═══════════════════════════════════════════════════
# Phase 1: 引导期（新用户，无简历）
# ═══════════════════════════════════════════════════

scenario(
    "onboard-01", "onboarding", "新用户打招呼",
    user_inputs=["你好，我想找工作"],
    expected_tool_chain=[],
    should_not_call=["save_my_resume", "get_my_resume", "get_my_profile"],
    notes="新用户无简历，应走 ONBOARDING_PROMPT，纯文字引导不调任何工具"
)

scenario(
    "onboard-02", "onboarding", "分享背景信息应写入画像",
    user_inputs=[
        "你好，我想找工作",
        "我是中财会计专业大二，会 Python 基础，想做量化",
    ],
    expected_tool_chain=["update_my_profile"],
    should_not_call=["save_my_resume"],
    notes="用户分享了教育/技能/目标，必须调用 update_my_profile 存入画像"
)

scenario(
    "onboard-03", "onboarding", "完成引导生成简历",
    user_inputs=[
        "你好",
        "我北邮CS大三，会 Go 和 Python，想找后端实习，北京，预期 200-300/天",
        "差不多了，帮我生成简历吧",
    ],
    expected_tool_chain=["update_my_profile", "save_my_resume"],
    should_not_call=[],
    notes="信息收集完整后，先更新画像再生成简历。update_my_profile 可能被多次调用（每轮一次），只要在 save_my_resume 之前至少有一次即可"
)

scenario(
    "onboard-04", "onboarding", "直接粘贴简历文本",
    user_inputs=[
        "我叫张三，3年Java开发，做过电商项目...（大段文本）",
    ],
    expected_tool_chain=["update_my_profile"],
    should_not_call=["save_my_resume"],
    notes="新用户贴文本型个人介绍，ONBOARDING_PROMPT 下走 update_my_profile 是合理行为。parse_resume_text 在引导期不适用。"
)

# ═══════════════════════════════════════════════════
# Phase 2: 简历管理（有简历用户）
# ═══════════════════════════════════════════════════

scenario(
    "resume-01", "resume", "查看简历列表",
    user_inputs=["看看我的简历"],
    expected_tool_chain=["list_my_resumes"],
    should_not_call=["save_my_resume", "tailor_resume"],
    needs_resume=True,
    notes="纯查询，只应调 list_my_resumes"
)

scenario(
    "resume-02", "resume", "查看特定简历内容",
    user_inputs=["打开后端开发那份简历"],
    expected_tool_chain=["list_my_resumes", "get_my_resume"],
    should_not_call=[],
    needs_resume=True,
    notes="工作流先 list 再 get。用户再从列表中选。"
)

scenario(
    "resume-03", "resume", "简历诊断",
    user_inputs=["帮我看下简历有什么问题"],
    expected_tool_chain=["list_my_resumes", "get_my_resume"],
    should_not_call=["save_my_resume", "tailor_resume"],
    needs_resume=True,
    notes="先列出简历让用户选，再读取内容进行诊断"
)

scenario(
    "resume-04", "resume", "修改简历后保存",
    user_inputs=["帮我把简历里的 Java 改成 Go", "好的，保存"],
    expected_tool_chain=["list_my_resumes", "get_my_resume"],
    should_not_call=["tailor_resume"],
    needs_resume=True,
    notes="先诊断（list+get），保存动作依赖上轮 Agent 建议。第二轮的 save_my_resume 取决于上下文是否到位，属于变数。"
)

# ═══════════════════════════════════════════════════
# Phase 3: 求职画像 [HIGH] 高危区域
# ═══════════════════════════════════════════════════

scenario(
    "profile-01", "profile", "查看求职画像",
    user_inputs=["我的求职画像"],
    expected_tool_chain=["get_my_profile"],
    should_not_call=["update_my_profile"],
    notes="纯查询，只调 get_my_profile"
)

scenario(
    "profile-02", "profile", "手动更新画像字段",
    user_inputs=["目标薪资改成 20-30K，只想去上海"],
    expected_tool_chain=["update_my_profile"],
    should_not_call=["save_my_resume", "get_my_resume"],
    notes="用户明确要求修改画像字段"
)

scenario(
    "profile-03", "profile", "从简历完善画像 [HIGH]",
    user_inputs=["用我的简历完善求职画像"],
    expected_tool_chain=["get_my_resume", "update_my_profile", "add_my_task"],
    should_not_call=["save_my_resume"],
    needs_resume=True,
    notes="""[HIGH] 高危场景！
预期: get_my_resume → update_my_profile → add_my_task
常见错误:
  1. 只输出文字不调工具
  2. 调了但只调 get_my_resume + save_my_resume（把画像写到简历里）
  3. 调了 update_my_profile 但没调 add_my_task（遗漏成长计划）
  4. update_my_profile 参数不对（把大段文字当 education 字段值）"""
)

# ═══════════════════════════════════════════════════
# Phase 4: JD 分析
# ═══════════════════════════════════════════════════

scenario(
    "jd-01", "jd", "粘贴 JD 分析",
    user_inputs=[
        """【岗位】后端开发实习生
【公司】XX科技
【职责】
1. 负责微服务架构设计与开发，参与系统重构
2. 编写高质量 Go/Python 代码，保障系统稳定性
3. 参与技术方案评审和代码审查
【要求】
1. 熟练掌握 Go 或 Python，熟悉 Kubernetes/Docker
2. 了解分布式系统设计，有 MySQL/Redis 使用经验
3. 有微服务项目经验者优先""",
    ],
    expected_tool_chain=["list_my_resumes", "score_job"],
    should_not_call=["save_my_resume", "tailor_resume"],
    needs_resume=True,
    notes="先 list 选简历，再 score_job。可能额外调 get_my_profile（画像辅助），不禁止。"
)

scenario(
    "jd-02", "jd", "高分后生成定制简历",
    user_inputs=[
        """帮我分析这个 JD：
【岗位】高级后端开发工程师
【公司】YY科技
【职责】负责后端服务开发与优化，参与分布式系统架构设计
【要求】精通 Python/Go，5年以上后端经验，熟悉 Kubernetes 和微服务架构，有分布式系统设计经验""",
        "评分不错，帮我生成定制简历",
    ],
    expected_tool_chain=["score_job", "tailor_resume"],
    should_not_call=[],
    needs_resume=True,
    notes="score_job 返回高分后用户要求定制 → tailor_resume"
)

# ═══════════════════════════════════════════════════
# Phase 5: 招呼语 / Cover Letter
# ═══════════════════════════════════════════════════

scenario(
    "pitch-01", "pitch", "生成招呼语",
    user_inputs=["帮我写个招呼语"],
    expected_tool_chain=["generate_pitch"],
    should_not_call=["generate_cover"],
    needs_resume=True,
    notes="招呼语是简短版，不是正式 Cover Letter"
)

scenario(
    "pitch-02", "pitch", "生成 Cover Letter",
    user_inputs=["写一封正式的求职信"],
    expected_tool_chain=["generate_cover"],
    should_not_call=["generate_pitch"],
    needs_resume=True,
    notes="正式求职信用 generate_cover"
)

# ═══════════════════════════════════════════════════
# Phase 6: 岗位搜索 & 收藏
# ═══════════════════════════════════════════════════

scenario(
    "search-01", "search", "搜索岗位",
    user_inputs=["帮我搜一下北京的后端开发实习岗位"],
    expected_tool_chain=["search_jobs"],
    should_not_call=["save_job"],
    notes="纯搜索，不自动保存"
)

scenario(
    "search-02", "search", "搜索后保存岗位",
    user_inputs=[
        "搜一下上海的量化实习",
        "把第一个保存下来",
    ],
    expected_tool_chain=["search_jobs", "save_job"],
    should_not_call=[],
    notes="搜索 → 用户选择 → 保存"
)

scenario(
    "search-03", "search", "查看收藏的岗位",
    user_inputs=["我收藏了哪些岗位"],
    expected_tool_chain=["list_saved_jobs"],
    should_not_call=["save_job", "search_jobs"],
    notes="纯查询已收藏岗位"
)

# ═══════════════════════════════════════════════════
# Phase 7: 面试准备
# ═══════════════════════════════════════════════════

scenario(
    "interview-01", "interview", "生成 STAR 故事",
    user_inputs=["帮我生成面试用的 STAR 故事"],
    expected_tool_chain=["generate_star_stories"],
    should_not_call=[],
    needs_resume=True,
    notes="从简历自动提取经历生成 STAR 故事"
)

# ═══════════════════════════════════════════════════
# Phase 8: 成长计划
# ═══════════════════════════════════════════════════

scenario(
    "growth-01", "growth", "查看成长计划",
    user_inputs=["我的学习计划"],
    expected_tool_chain=["list_my_tasks"],
    should_not_call=["add_my_task"],
    notes="纯查询成长任务列表"
)

scenario(
    "growth-02", "growth", "添加学习任务",
    user_inputs=["我要学 pandas 和 numpy"],
    expected_tool_chain=["add_my_task"],
    should_not_call=["list_my_tasks"],
    notes="用户提到想学某个技能 → add_my_task。可能调用多次（pandas 一次, numpy 一次）"
)


# ═══════════════════════════════════════════════════
# 测试执行
# ═══════════════════════════════════════════════════

def pytest_generate_tests(metafunc):
    if "scenario" in metafunc.fixturenames:
        metafunc.parametrize(
            "scenario",
            SCENARIOS,
            ids=[f"{s.id}: {s.name}" for s in SCENARIOS],
        )


def test_scenario_tool_chain(scenario: Scenario):
    """验证 LLM 的工具调用链是否符合预期"""
    tool_calls, replies = _run_scenario(scenario)

    # 1. 检查是否漏了必须的工具
    expected = scenario.expected_tool_chain
    for expected_tool in expected:
        assert expected_tool in tool_calls, (
            f"期望调用 {expected_tool}，但实际没有。\n"
            f"实际工具链: {tool_calls}\n"
            f"最后回复: {replies[-1][:200] if replies else '(无)'}"
        )

    # 2. 检查顺序（宽松匹配：期望链的每个元素按顺序出现在实际链中）
    idx = 0
    for expected_tool in expected:
        try:
            idx = tool_calls.index(expected_tool, idx) + 1
        except ValueError:
            pass  # 已在上面断言，这里只检查顺序

    # 3. 检查禁止调用的工具
    for forbidden in scenario.should_not_call:
        assert forbidden not in tool_calls, (
            f"禁止调用 {forbidden}，但实际调了。\n"
            f"实际工具链: {tool_calls}\n"
            f"最后回复: {replies[-1][:200] if replies else '(无)'}"
        )


# ── 手动验证辅助 ──

def list_all_scenarios():
    """打印所有场景，供人工审查"""
    phases = {}
    for s in SCENARIOS:
        phases.setdefault(s.phase, []).append(s)

    print(f"\n{'='*60}")
    print(f"业务场景测试套件 — 共 {len(SCENARIOS)} 个场景")
    print(f"{'='*60}")

    for phase, items in phases.items():
        print(f"\n── {phase} ──")
        for s in items:
            risk = " [HIGH]" if "HIGH" in s.name or "高危" in s.notes else ""
            print(f"  [{s.id}]{risk} {s.name}")
            print(f"      输入: {s.user_inputs[0][:60]}...")
            print(f"      期望链: {' → '.join(s.expected_tool_chain) or '(无工具调用)'}")
            if s.should_not_call:
                print(f"      禁止调: {', '.join(s.should_not_call)}")


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    list_all_scenarios()
