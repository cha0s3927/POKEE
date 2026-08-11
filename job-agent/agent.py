"""
求职助手 Agent — tool-calling loop
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import httpx
from openai import OpenAI

from config import settings
logger = logging.getLogger(__name__)

client = OpenAI(
    api_key=settings.llm_api_key,
    base_url=settings.llm_base_url,
    http_client=httpx.Client(transport=httpx.HTTPTransport(retries=0), timeout=httpx.Timeout(90.0, connect=10.0)),
)

ONBOARDING_PROMPT = """你是孙悟空，花果山求职道场的掌门。金箍棒不仅能打架还能改简历，俺老孙专门帮师弟师妹们找到好工作。

用户是新来的师弟/师妹，还没想清楚方向，也没有简历。你的任务是用聊天的方式摸清他的底：

1. **了解现状**：现在在做什么？会什么本事？闯过什么关？
2. **明确方向**：想打什么岗位？想去哪路神仙的地盘？
3. **确认偏好**：想要多少仙丹（薪资）？想在哪个山头（城市）？

一次只问 1-2 个问题，别跟审犯人似的。每轮对话中，只要收集到了新信息，就立刻调用 update_my_profile 存入画像。信息收集齐了，先确认画像，再调 save_my_resume 帮他打造第一根金箍棒（简历），简历名用"{岗位方向} - 初始版"。

## 工具使用规则

- 收集到任何个人信息（学校/技能/经历/目标岗位/薪资/城市）→ 立刻调 update_my_profile
- 用户粘贴了大段简历文本（≥100字，含个人经历/技能）→ 调 parse_resume_text 帮他整理
- 用户说想学什么/要做什么 → 调 add_my_task
- 引导期不要调用 score_job / tailor_resume / generate_pitch / generate_cover / search_jobs ——还没简历呢

## 人设
- 自称"俺老孙"或"猴哥"，称用户"师弟"或"师妹"
- 嘴毒心软，偶尔自恋（"俺老孙当年大闹天宫的时候……"但别过度）
- 不用加粗、标题、列表，说人话
- 偶尔夹杂西游记梗，但别影响回答质量
- 对外要毒舌（烂JD、坑爹公司），对师弟要护犊子
- 不提"亲""小伙伴""宝宝"等电商客服腔
- 涉及钱（薪资谈判）时要严肃专业，别开玩笑"""

SYSTEM_PROMPT = """你是孙悟空，花果山求职道场的掌门。金箍棒不仅能打架，还能改简历、分析JD、写招呼语。帮师弟师妹们找工作是你的新事业——当年大闹天宫的劲儿，现在全用在帮人拿下offer上了。

## 首次对话

师弟师妹可能有简历但不知道该做什么。第一句话自然招呼，用以下风格举例说明你能干什么：

> 师弟来啦！俺老孙能帮你干的活儿可多了——贴个 JD 过来帮你看看匹配度，或者说「帮我写个招呼语」「生成面试故事」「看看我的简历」。想做针对性的准备，说「用简历完善画像」就行。

列 4-5 个就够了，用引号括起来的短句就是师弟可以直接用的说法。

## 工具选择规则（最重要！每条都要遵守）

遇到以下情况，必须调用对应工具，不要只说话不做事：

### 简历
- 用户粘贴大段个人经历文本（≥100字，含学校/技能/工作经历/项目经验等个人特征）→ **parse_resume_text**。注意：先确认简历名称再调，不要自己瞎编名字
- 用户说"看看我的简历""有哪些简历"（没指定哪份）→ **list_my_resumes**
- 用户指定了某份简历要看内容 / 诊断简历问题 → **get_my_resume**
- 用户说"保存""存下来"（已展示了简历内容后）→ **save_my_resume**，新建不传 resume_id，修改传入
- tailor_resume 后用户说"保存" → **save_last_resume**（自动用暂存内容，不用传 markdown）
- 用户说"用简历完善画像" → 先 get_my_resume 读简历，再 update_my_profile 写入提取字段，再 add_my_task 加行动项

### JD / 岗位
- 用户粘贴大段招聘文本（含"岗位职责""任职要求""薪资范围""招聘"等**招聘特征词**）→ **score_job**。重要：不要把简历文本当JD！简历含"姓名/学校/技能/工作经历"，JD含"岗位职责/任职要求"
- 用户要求"定制简历""针对JD改写" → **tailor_resume**（需要先有JD内容和简历）
- 用户要求"写招呼语""打招呼的话""沟通话术" → **generate_pitch**（需要JD内容）
- 用户要求"写求职信""Cover Letter" → **generate_cover**（需要JD内容）
- 用户要求"搜索XX岗位""帮我找XX工作" → **search_jobs**
- 用户说"收藏这个岗位""保存岗位" → **save_job**
- 用户说"看看收藏""我收藏的岗位" → **list_saved_jobs**

### 面试准备
- 用户要求"生成面试故事""准备STAR""面试用的" → **generate_star_stories**

### 求职画像
- 用户说"查看画像""我的画像" → **get_my_profile**
- 用户说"修改画像""更新XX""把XX改成YY"→ **update_my_profile**（只传要改的字段）
- 新用户引导期收集到新信息 → **update_my_profile**

### 成长计划
- 用户说"学习计划""我的任务" → **list_my_tasks**
- 用户说"我要学XX""把XX加入计划" → **add_my_task**
- 用户说"开始做""完成了""暂停""改标题" → **update_my_task**（需要 task_id，不知道先 list_my_tasks）
- 用户说"删掉""移除"某个任务 → **delete_my_task**（需要 task_id）

### 闲聊
- 用户只是打招呼/闲聊/咨询建议/问"你觉得我适合什么方向" → 不调工具，自然回复即可

## 通用规则
- 不确定用哪份简历时，先 list_my_resumes 让用户选
- 评分 60 以上，主动问"要不要猴哥帮你量身打造一份定制简历和招呼语？"
- 定制简历、招呼语、求职信展示后，问用户要不要保存
- 不要连续调多个工具而不给用户回复——每调完 1-2 个工具，就把目前的结果告诉用户

## 人设
- 自称"俺老孙"或"猴哥"，称用户"师弟"或"师妹"
- 嘴毒心软：分析 JD 时可以毒舌（"这薪资写的是认真的吗""JD 写得太水了"），但对师弟要护犊子
- 偶尔忆当年但别过度（最多一天一次），重点还是帮师弟解决问题
- 不用加粗、标题、列表格式，说人话
- 不提"亲""小伙伴""宝宝"等电商客服腔
- 涉及钱（薪资谈判）时要严肃专业，别开玩笑
- 分析 JD 一针见血：核心匹配点 2-3 条，主要风险 1-2 条，别啰嗦
- 评分结果别逐条报数字，抓住关键信息用自然语言说出来"""

TOOLS = [
    # ═══ 简历管理 ═══
    {
        "type": "function",
        "function": {
            "name": "list_my_resumes",
            "description": "列出你的所有简历（名称/ID/是否默认）。触发：「看看我的简历」「有哪些简历」「选一份简历」。不触发：只是闲聊中提到「简历」这个词但没有要看列表。"
            " — 通常在 get_my_resume / score_job / tailor_resume 之前调用，确认用哪份简历。",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_my_resume",
            "description": "读取一份简历的完整 Markdown 内容。触发：用户指定了某份简历要看内容、诊断简历写得怎么样、逐段给优化建议。不传 resume_id 则读默认简历。"
            " — 注意：不知道有哪些简历时先调 list_my_resumes。",
            "parameters": {
                "type": "object",
                "properties": {
                    "resume_id": {"type": "string", "description": "简历 ID（来自 list_my_resumes），不传则使用默认简历"},
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "parse_resume_text",
            "description": "用户在聊天中粘贴了大段个人简历文本（≥100字，含学校/技能/工作经历/项目等个人信息），用 AI 整理为规范 Markdown 格式并自动保存。"
            " 触发：用户消息中包含大量个人经历信息，明显是简历内容。不触发：用户只是说「帮我做简历」但没粘贴实际内容——先问他要。"
            " 重要：要先跟用户确认简历名称（如「后端开发-张三」）再调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "用户粘贴的简历全文"},
                    "name": {"type": "string", "description": "简历名称，如「后端开发」「产品经理」"},
                },
                "required": ["text", "name"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_my_resume",
            "description": "新建或更新简历。传入完整的 Markdown 简历内容。新建（不传 resume_id），修改已有简历（传 resume_id）。"
            " 触发：用户明确说「保存」「存下来」，且你已经展示了简历内容。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "简历名称"},
                    "markdown": {"type": "string", "description": "完整的 Markdown 简历全文（必传）"},
                    "resume_id": {"type": "string", "description": "修改已有简历时传入，新建不传"},
                },
                "required": ["name", "markdown"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_last_resume",
            "description": "保存最近一次 tailor_resume 生成的定制简历。与 save_my_resume 的区别：这个不需要传 markdown——后端已经暂存了定制结果。"
            " 只在 tailor_resume 之后用户说「保存」时使用。其他任何保存场景都用 save_my_resume。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "简历名称，如「后端开发-定制版」"},
                    "resume_id": {"type": "string", "description": "修改已有简历时传入，新建不传"},
                },
                "required": ["name"],
                "additionalProperties": False,
            },
        },
    },
    # ═══ JD 分析 & 投递 ═══
    {
        "type": "function",
        "function": {
            "name": "score_job",
            "description": "用你的简历对一份 JD 进行 7 维评分（技能匹配/经验匹配/薪资匹配/地点/成长空间/公司稳定性/投递性价比），总分 0-100。"
            " 触发：用户粘贴大段招聘文本（含「岗位职责」「任职要求」「薪资范围」等**招聘特征词**）。不触发：用户粘贴的是个人经历（简历）而非JD——简历用 parse_resume_text。"
            " 区分JD和简历的关键：JD有「岗位职责」「任职要求」，简历有「姓名」「学校」「工作经历」。"
            " 返回：总分/各维度得分/匹配亮点/风险点/投递建议。评分≥60 主动问要不要定制简历。",
            "parameters": {
                "type": "object",
                "properties": {
                    "jd_text": {"type": "string", "description": "岗位 JD 全文"},
                    "resume_id": {"type": "string", "description": "简历 ID，不传则使用默认简历"},
                },
                "required": ["jd_text"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tailor_resume",
            "description": "根据 JD 从你的简历中选取最相关的经历，重排顺序，生成一份针对该岗位的定制版 Markdown 简历。只选取和重排已有内容，绝不编造。"
            " 触发：用户明确要求「定制简历」「针对JD改写」「生成定制版」。前提：已通过 score_job 分析过JD，且有简历。"
            " 生成后展示结果，问用户要不要保存。用户说保存时用 save_last_resume。",
            "parameters": {
                "type": "object",
                "properties": {
                    "jd_text": {"type": "string", "description": "岗位 JD 全文"},
                    "resume_id": {"type": "string", "description": "简历 ID，不传则使用默认简历"},
                },
                "required": ["jd_text"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_pitch",
            "description": "生成 80-150 字的招呼语，用于 BOSS 直聘等平台的「立即沟通」。突出核心匹配点，简短有力。"
            " 触发：「帮我写招呼语」「怎么跟HR打招呼」「沟通话术」「BOSS上怎么聊」。需要 JD 内容。",
            "parameters": {
                "type": "object",
                "properties": {
                    "jd_text": {"type": "string", "description": "岗位 JD 全文"},
                    "resume_id": {"type": "string", "description": "简历 ID，不传则使用默认简历"},
                },
                "required": ["jd_text"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_cover",
            "description": "生成 250-400 字的正式求职信 / Cover Letter，适合邮件投递或海外岗位申请。"
            " 触发：「写求职信」「Cover Letter」「写封正式的」。需要 JD 内容。",
            "parameters": {
                "type": "object",
                "properties": {
                    "jd_text": {"type": "string", "description": "岗位 JD 全文"},
                    "resume_id": {"type": "string", "description": "简历 ID，不传则使用默认简历"},
                },
                "required": ["jd_text"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_jobs",
            "description": "搜索招聘网站上的岗位列表。触发：「搜索XX岗位」「帮我找XX工作」「有没有XX的招聘」「查一下XX」。"
            " site 参数：zhipin（BOSS直聘）/shixiseng（实习僧）/zhilian（智联）/51job/lagou/all。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词，如「Python 后端 上海」"},
                    "site": {"type": "string", "description": "限定网站，默认 all"},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_job",
            "description": "收藏一个岗位。触发：score_job 或 search_jobs 之后用户说「收藏这个」「保存这个岗位」。"
            " 至少传 title 和 company，其他字段有就传。",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "岗位名称"},
                    "company": {"type": "string", "description": "公司名"},
                    "platform": {"type": "string", "description": "来源: boss/zhilian/shixiseng/51job/lagou/other"},
                    "url": {"type": "string", "description": "岗位链接"},
                    "jd_text": {"type": "string", "description": "JD 全文"},
                    "score_total": {"type": "number", "description": "评分总分"},
                    "score_details": {"type": "object", "description": "评分维度明细"},
                    "verdict": {"type": "string", "description": "评分结论"},
                    "resume_id": {"type": "string", "description": "对应简历 ID"},
                },
                "required": ["title", "company"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_saved_jobs",
            "description": "列出已收藏的所有岗位。触发：「我收藏的岗位」「已保存的岗位」「看看收藏」「我的收藏」。",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    # ═══ 面试准备 ═══
    {
        "type": "function",
        "function": {
            "name": "generate_star_stories",
            "description": "从简历中自动提取经历，扩展为 STAR（情境-任务-行动-结果）格式的面试故事，用于面试准备。"
            " 触发：「生成面试故事」「准备STAR」「面试用的故事」「帮我准备面试」。需要至少有一份简历。",
            "parameters": {
                "type": "object",
                "properties": {
                    "resume_id": {"type": "string", "description": "简历 ID，不传则使用默认简历"},
                },
                "additionalProperties": False,
            },
        },
    },
    # ═══ 求职画像 ═══
    {
        "type": "function",
        "function": {
            "name": "get_my_profile",
            "description": "查看你的求职画像（教育/技能/经历摘要/项目/目标岗位/目标行业/薪资范围/意向城市）。"
            " 触发：「查看画像」「我的画像」「看看我的现状」。修改画像前也应先调用此工具看看现在有什么。",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_my_profile",
            "description": "更新求职画像中的字段。只传要更新的字段，不传的保持不变。"
            " 触发：「修改画像」「更新XX」「把薪资改成XX」。新用户引导期收集到个人信息后要调用此工具存入。"
            " 「用简历完善画像」时：先 get_my_resume 读简历，提取字段后调此工具写入。",
            "parameters": {
                "type": "object",
                "properties": {
                    "education": {"type": "object", "description": "{school, major, degree}"},
                    "skills": {"type": "array", "items": {"type": "string"}, "description": "技能列表"},
                    "experience_summary": {"type": "string", "description": "工作经历摘要"},
                    "projects": {"type": "array", "description": "[{name, description, url}]"},
                    "target_role": {"type": "string", "description": "目标岗位"},
                    "target_industry": {"type": "string", "description": "目标行业"},
                    "salary_min": {"type": "integer", "description": "最低薪资(K)"},
                    "salary_max": {"type": "integer", "description": "最高薪资(K)"},
                    "preferred_cities": {"type": "array", "items": {"type": "string"}, "description": "意向城市"},
                },
                "additionalProperties": False,
            },
        },
    },
    # ═══ 成长计划 ═══
    {
        "type": "function",
        "function": {
            "name": "list_my_tasks",
            "description": "列出你的成长计划任务（待学技能/待做事项）。触发：「学习计划」「成长计划」「我的任务」「待办」。"
            " update_my_task 和 delete_my_task 之前如果不知道 task_id，先调此工具。",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_my_task",
            "description": "向成长计划添加一个任务。触发：「我要学XX」「我想学XX」「把XX加入计划」「帮我加个任务」。"
            " category: skill（学技能）/project（做项目）/action（行动项），默认 skill。"
            " status: pending（待开始）/in_progress（进行中）/done（已完成），默认 pending。",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "任务标题"},
                    "category": {"type": "string", "description": "skill/project/action，默认 skill"},
                    "status": {"type": "string", "description": "pending/in_progress/done，默认 pending"},
                },
                "required": ["title"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_my_task",
            "description": "更新任务（状态/标题/分类）。触发：「开始做了」「完成了」「暂停」「改个标题」。"
            " task_id 来自 list_my_tasks。不知道 task_id 时先调 list_my_tasks。"
            " 只传要改的字段，不改的不用传。",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "任务 ID（必传，来自 list_my_tasks）"},
                    "status": {"type": "string", "description": "pending/in_progress/done/paused"},
                    "title": {"type": "string", "description": "新标题"},
                    "category": {"type": "string", "description": "skill/project/action"},
                },
                "required": ["task_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_my_task",
            "description": "删除一个成长任务。触发：「删掉这个任务」「移除」「不要了」。task_id 来自 list_my_tasks。"
            " 不知道 task_id 时先调 list_my_tasks。",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "任务 ID（必传，来自 list_my_tasks）"},
                },
                "required": ["task_id"],
                "additionalProperties": False,
            },
        },
    },
]

MAX_TOOL_ITERATIONS = 5
MAX_MESSAGES = 101


# Premium tools that require user confirmation before execution
PREMIUM_TOOLS = {
    "tailor_resume":     {"cost": 10, "label_zh": "AI 简历优化", "label_en": "Resume Tailor"},
    "parse_resume_text": {"cost": 6,  "label_zh": "AI 简历制作", "label_en": "Resume Parsing"},
    "score_job":         {"cost": 3,  "label_zh": "JD 匹配分析",  "label_en": "JD Analysis"},
    "generate_pitch":    {"cost": 8,  "label_zh": "AI 招呼语",   "label_en": "Self Pitch"},
    "generate_cover":    {"cost": 8,  "label_zh": "AI 求职信",   "label_en": "Cover Letter"},
}

class Agent:
    def __init__(self):
        self.sessions: dict[str, list[dict]] = {}
        self._last_tailored: dict[str, str] = {}  # user_id → markdown
        self._langs: dict[str, str] = {}  # user_id → lang
        self._pending_tool: dict[str, dict] = {}  # user_id → {name, args, cost, label}

    def _build_prompt(self, has_resume: bool, lang: str) -> str:
        prompt = SYSTEM_PROMPT if has_resume else ONBOARDING_PROMPT
        if lang == "en":
            prompt += "\n\n[IMPORTANT] You MUST respond in English only. Do NOT reply in Chinese. All tool call arguments (like title, name, content) must also be in English."
        else:
            prompt += "\n\n请始终用中文回复。"
        return prompt

    def chat(self, user_id: str, user_message: str) -> dict:
        """Returns {"reply": str, "tool_calls": [str], "confirm_needed": dict|None}"""
        from database import engine, get_user_lang
        from sqlalchemy import text

        lang = get_user_lang(user_id)

        # ── 初始化 session ──
        if user_id not in self.sessions:
            with engine.connect() as conn:
                row = conn.execute(
                    text("SELECT id FROM resumes WHERE user_id = :uid LIMIT 1"),
                    {"uid": user_id},
                ).fetchone()
            has_resume = row is not None
            prompt = self._build_prompt(has_resume, lang)
            self.sessions[user_id] = [{"role": "system", "content": prompt}]
            self._langs[user_id] = lang
        elif self._langs.get(user_id) != lang:
            has_resume = len(self.sessions[user_id]) > 0
            prompt = self._build_prompt(has_resume, lang)
            self.sessions[user_id][0] = {"role": "system", "content": prompt}
            self._langs[user_id] = lang

        messages = self.sessions[user_id]

        # ── 处理付费工具确认 ──
        if user_id in self._pending_tool and "[CONFIRM_PREMIUM]" in user_message:
            pending = self._pending_tool[user_id]
            # 移除上一次对话中 __confirm__ 污染的消息（最后两条：assistant tool_call + tool result）
            confirm_idx = None
            for i in range(len(messages) - 1, -1, -1):
                if (messages[i].get("role") == "tool"
                        and isinstance(messages[i].get("content"), str)
                        and '"__confirm__"' in messages[i].get("content", "")[:50]):
                    confirm_idx = i
                    break
            if confirm_idx is not None:
                del messages[confirm_idx]           # tool result with __confirm__
                if confirm_idx > 0 and messages[confirm_idx - 1].get("role") == "assistant":
                    del messages[confirm_idx - 1]   # assistant's tool_call that triggered it

            # _execute 内部会检查 _pending_tool 并自动 pop，所以这里不提前 pop
            result = self._execute(pending["name"], pending["args"], user_id)
            # 生成回复
            label = pending.get("label_zh" if lang == "zh" else "label_en", pending["name"])
            reply = f"好的！{label}已完成，来看看结果吧~"
            if result.get("markdown"):
                reply += "\n\n" + result["markdown"][:3000]
            elif result.get("text"):
                reply += "\n\n" + result["text"][:3000]
            elif result.get("total") is not None:
                reply += f"\n\n总分: {result['total']}/100\n建议: {result.get('verdict', '')}"
            return {"reply": reply, "tool_calls": [pending["name"]], "confirm_needed": None}

        # ── 添加用户消息 ──
        messages.append({"role": "user", "content": user_message})

        tool_calls_made: list[str] = []
        confirm_needed = None

        # ── Agent 循环 ──
        for iteration in range(MAX_TOOL_ITERATIONS):
            response = client.chat.completions.create(
                model=settings.llm_model,
                messages=messages,
                tools=TOOLS,
                tool_choice="auto",
            )

            msg = response.choices[0].message

            if msg.tool_calls:
                tool_call_entries = []
                for tc in msg.tool_calls:
                    tool_call_entries.append({
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                    })

                messages.append({
                    "role": "assistant",
                    "content": msg.content,
                    "tool_calls": tool_call_entries,
                })

                for tc in msg.tool_calls:
                    name = tc.function.name
                    tool_calls_made.append(name)
                    try:
                        args = json.loads(tc.function.arguments)
                    except json.JSONDecodeError:
                        args = {}

                    logger.info("tool_call: %s args=%s", name, {k: str(v)[:100] for k, v in args.items()})
                    result = self._execute(name, args, user_id)

                    if result.get("__confirm__"):
                        # 付费工具需要确认 → 撤销刚追加的 assistant tool_call，跳出循环
                        confirm_needed = result
                        del messages[-1]  # 撤销 assistant tool_call（tool result 还没追加）
                        break

                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps(result, ensure_ascii=False, default=str),
                    })

                if confirm_needed:
                    break
            else:
                # 无 tool call → LLM 认为已完成，返回结果
                messages.append({"role": "assistant", "content": msg.content})

                if len(messages) > MAX_MESSAGES:
                    keep_from = len(messages) - 100
                    while keep_from > 1 and messages[keep_from].get("role") == "tool":
                        keep_from -= 1
                    self.sessions[user_id] = [messages[0]] + messages[keep_from:]

                return {"reply": msg.content, "tool_calls": tool_calls_made, "confirm_needed": None}

        if confirm_needed:
            return {"reply": "确认处理中...", "tool_calls": tool_calls_made, "confirm_needed": confirm_needed}

        return {"reply": "抱歉，处理超时，请重试。", "tool_calls": tool_calls_made, "confirm_needed": None}

    def _execute(self, name: str, args: dict, user_id: str) -> dict:
        # Gate premium tools: require user confirmation before execution
        if name in PREMIUM_TOOLS:
            pending = self._pending_tool.get(user_id)
            if not pending or pending.get("name") != name:
                info = PREMIUM_TOOLS[name]
                self._pending_tool[user_id] = {"name": name, "args": args, "cost": info["cost"], "label_zh": info["label_zh"], "label_en": info["label_en"]}
                return {"__confirm__": True, "tool": name, "cost": info["cost"], "label_zh": info["label_zh"], "label_en": info["label_en"]}
            # User confirmed — clear pending and proceed
            self._pending_tool.pop(user_id, None)

        from engine import score_job
        from resume import (
            list_resumes, get_resume, save_resume, get_default_resume,
            get_resume_content, tailor_resume,
        )
        from cover_letter import generate_pitch, generate_cover

        if name == "parse_resume_text":
            text = args.get("text", "")
            name_val = args.get("name", "未命名简历")
            if len(text) < 50:
                return {"error": "简历内容太短，请粘贴完整的简历"}
            try:
                from database import spend_points
                spend_points(user_id, 50, "parse_resume")
                markdown = self._clean_resume_text(text, user_id)
                result = save_resume(user_id, name_val, markdown)
                logger.info(f"parse_resume_text OK: user={user_id} resume_id={result['resume_id']}")
                return {"status": "ok", "resume_id": result["resume_id"], "name": name_val, "message": f"简历「{name_val}」已解析并保存"}
            except Exception as e:
                logger.exception("parse_resume_text error")
                return {"error": f"解析失败: {e}。请到简历管理页面上传文件。"}

        elif name == "list_my_resumes":
            resumes = list_resumes(user_id)
            if not resumes:
                return {"resumes": [], "message": "你还没有上传简历。请在简历管理页面上传，或直接在聊天框发送你的 JSON 简历。"}
            return {"resumes": resumes}

        elif name == "get_my_resume":
            resume_id = args.get("resume_id")
            markdown = None
            if resume_id:
                markdown = get_resume_content(resume_id)
            if not markdown:
                r = get_default_resume(user_id)
                if r:
                    markdown = r["content"]
                    resume_id = r["id"]
            if not markdown:
                return {"error": "no_resume", "message": "请先上传简历"}
            return {"resume_id": resume_id, "markdown": markdown}

        elif name == "save_my_resume":
            name_val = args.get("name", "未命名简历")
            markdown = args.get("markdown", "")
            resume_id = args.get("resume_id")

            if not markdown or len(markdown) < 20:
                return {"error": "请传入完整的 Markdown 简历内容"}

            try:
                logger.info(f"save_my_resume called: user={user_id} name={name_val} len={len(markdown)} resume_id={resume_id}")
                result = save_resume(user_id, name_val, markdown, resume_id)
                action = "已更新" if resume_id else "已保存"
                logger.info(f"save_my_resume OK: resume_id={result['resume_id']}")
                return {"status": "ok", "resume_id": result["resume_id"], "message": f"简历「{name_val}」{action}"}
            except Exception as e:
                logger.exception("save_my_resume error")
                return {"error": str(e)}

        elif name == "score_job":
            resume_id = args.get("resume_id")
            markdown = None
            if resume_id:
                markdown = get_resume_content(resume_id)
            if not markdown:
                r = get_default_resume(user_id)
                if r:
                    markdown = r["content"]
                    resume_id = r["id"]
            if not markdown:
                return {"error": "no_resume", "message": "请先上传简历"}

            jd_text = args.get("jd_text", "")
            if len(jd_text) < 20:
                return {"error": "jd_too_short", "message": "JD 内容太短，请粘贴完整的岗位描述"}
            try:
                from database import spend_points
                spend_points(user_id, 20, "jd_score")
                result = score_job(markdown, jd_text)
                return {
                    "total": result.total,
                    "dimensions": result.dimensions,
                    "strengths": result.strengths,
                    "weaknesses": result.weaknesses,
                    "verdict": result.verdict,
                    "verdict_reason": result.verdict_reason,
                    "resume_id": resume_id,
                }
            except Exception as e:
                logger.exception("score_job error")
                return {"error": str(e), "message": "评分失败，请稍后重试"}

        elif name == "tailor_resume":
            resume_id = args.get("resume_id")
            if not resume_id:
                r = get_default_resume(user_id)
                resume_id = r["id"] if r else None
            if not resume_id:
                return {"error": "no_resume", "message": "请先上传简历"}
            jd_text = args.get("jd_text", "")
            if len(jd_text) < 20:
                return {"error": "jd_too_short", "message": "JD 内容太短"}
            try:
                from database import spend_points
                spend_points(user_id, 90, "tailor_resume")
                markdown = tailor_resume(resume_id, jd_text)
                self._last_tailored[user_id] = markdown
                return {"markdown": markdown}
            except Exception as e:
                logger.exception("tailor_resume error")
                return {"error": str(e), "message": "简历定制失败，请稍后重试"}

        elif name == "save_last_resume":
            markdown = self._last_tailored.get(user_id, "")
            if not markdown:
                return {"error": "no_tailored", "message": "没有待保存的简历。请先生成定制简历（tailor_resume）。"}
            name_val = args.get("name", "未命名简历")
            resume_id = args.get("resume_id")
            try:
                result = save_resume(user_id, name_val, markdown, resume_id)
                action = "已更新" if resume_id else "已保存"
                return {"status": "ok", "resume_id": result["resume_id"], "message": f"简历「{name_val}」{action}"}
            except Exception as e:
                logger.exception("save_last_resume error")
                return {"error": str(e)}

        elif name == "generate_pitch":
            jd_text = args.get("jd_text", "")
            resume_id = args.get("resume_id")
            if not resume_id:
                r = get_default_resume(user_id)
                resume_id = r["id"] if r else None
            if not resume_id:
                return {"error": "no_resume", "message": "请先上传简历"}
            try:
                from database import spend_points
                spend_points(user_id, 70, "cover_letter")
                text = generate_pitch(user_id, jd_text, resume_id)
                return {"text": text}
            except Exception as e:
                logger.exception("generate_pitch error")
                return {"error": str(e), "message": "生成招呼语失败，请稍后重试"}

        elif name == "generate_cover":
            jd_text = args.get("jd_text", "")
            resume_id = args.get("resume_id")
            if not resume_id:
                r = get_default_resume(user_id)
                resume_id = r["id"] if r else None
            if not resume_id:
                return {"error": "no_resume", "message": "请先上传简历"}
            try:
                from database import spend_points
                spend_points(user_id, 70, "cover_letter")
                text = generate_cover(user_id, jd_text, resume_id)
                return {"text": text}
            except Exception as e:
                logger.exception("generate_cover error")
                return {"error": str(e), "message": "生成求职信失败，请稍后重试"}

        elif name == "search_jobs":
            try:
                from search import search_jobs as do_search
                query = args.get("query", "")
                site = args.get("site", "all")
                results = do_search(query, site, user_id)
                return results
            except Exception as e:
                logger.exception("search_jobs error")
                return {"error": str(e), "results": [], "message": "搜索功能暂时不可用，请手动粘贴 JD。"}

        elif name == "save_job":
            try:
                from routes.platforms import save_job_for_user
                job_id = save_job_for_user(user_id, args)
                return {"status": "ok", "job_id": job_id, "message": "岗位已保存"}
            except Exception as e:
                logger.exception("save_job error")
                return {"error": str(e)}

        elif name == "list_saved_jobs":
            from routes.platforms import list_saved_jobs_for_user
            jobs = list_saved_jobs_for_user(user_id)
            return {"jobs": jobs}

        elif name == "generate_star_stories":
            resume_id = args.get("resume_id")
            markdown = None
            if resume_id:
                markdown = get_resume_content(resume_id)
            if not markdown:
                r = get_default_resume(user_id)
                if r:
                    markdown = r["content"]
                    resume_id = r["id"]
            if not markdown:
                return {"error": "no_resume", "message": "请先上传简历"}

            from interview import generate_star_stories
            try:
                stories = generate_star_stories(user_id, markdown, resume_id)
                return {"stories": stories, "message": f"已生成 {len(stories)} 个 STAR 故事"}
            except Exception as e:
                logger.exception("generate_star_stories error")
                return {"error": str(e), "message": "生成 STAR 故事失败，请稍后重试"}

        elif name == "get_my_profile":
            from routes.platforms import api_get_profile
            result = api_get_profile(user={"id": user_id})
            return result["profile"]

        elif name == "update_my_profile":
            from routes.platforms import api_update_profile, ProfilePayload
            allowed = {
                "name", "education", "skills", "experience_summary", "projects",
                "target_role", "target_industry", "salary_min", "salary_max", "preferred_cities",
                "summary",
            }
            payload = {}
            for k, v in args.items():
                if k not in allowed or v is None:
                    continue
                if k == "education" and not isinstance(v, dict):
                    continue  # LLM sometimes passes education as string
                if k in ("salary_min", "salary_max") and not isinstance(v, (int, float)):
                    continue  # LLM sometimes passes salary as string
                if k in ("skills", "projects", "preferred_cities") and not isinstance(v, list):
                    continue
                payload[k] = v
            if not payload:
                return {"error": "no_fields", "message": "请至少指定一个要更新的字段"}
            profile = api_update_profile(ProfilePayload(**payload), user={"id": user_id})
            return {"status": "ok", "profile": profile["profile"]}

        elif name == "list_my_tasks":
            from routes.platforms import api_list_growth_tasks
            result = api_list_growth_tasks(user={"id": user_id})
            return result

        elif name == "add_my_task":
            from routes.platforms import api_create_growth_task, GrowthTaskPayload
            title = args.get("title", "")
            if not isinstance(title, str) or not title.strip():
                return {"error": "missing_title", "message": "请指定任务标题"}
            category = args.get("category", "skill")
            if category not in ("skill", "project", "action"):
                category = "skill"
            status = args.get("status", "pending")
            if status not in ("pending", "in_progress", "done"):
                status = "pending"
            try:
                task = api_create_growth_task(GrowthTaskPayload(
                    title=title.strip(), category=category, status=status,
                ), user={"id": user_id})
                return task
            except Exception as e:
                logger.exception("add_my_task error")
                return {"error": str(e)}

        elif name == "update_my_task":
            from routes.platforms import _do_update_growth_task, GrowthTaskPayload
            from database import engine
            from sqlalchemy import text
            task_id = args.get("task_id", "")
            if not task_id:
                return {"error": "missing_task_id", "message": "请指定任务 ID"}
            # 先从 DB 取当前值，LLM 没传的字段保持不动
            with engine.connect() as conn:
                cur = conn.execute(
                    text("SELECT title, category, status FROM growth_tasks WHERE id = :tid AND user_id = :uid"),
                    {"tid": task_id, "uid": user_id},
                ).fetchone()
            if not cur:
                return {"error": "not_found", "message": "任务不存在"}
            title = (args.get("title") or "").strip() or cur.title
            category = args.get("category", "") or cur.category
            status = args.get("status", "") or cur.status
            if status not in ("pending", "in_progress", "done", "paused"):
                status = cur.status
            if status == "paused":
                status = "pending"
            try:
                result = _do_update_growth_task(
                    task_id,
                    GrowthTaskPayload(title=title, category=category, status=status),
                    user_id,
                    trigger_engine=True,
                )
                return result
            except Exception as e:
                logger.exception("update_my_task error")
                return {"error": str(e)}

        elif name == "delete_my_task":
            from routes.platforms import api_delete_growth_task
            task_id = args.get("task_id", "")
            if not task_id:
                return {"error": "missing_task_id", "message": "请指定任务 ID"}
            try:
                api_delete_growth_task(task_id, user={"id": user_id})
                return {"status": "ok", "message": "任务已删除"}
            except Exception as e:
                logger.exception("delete_my_task error")
                return {"error": str(e)}

        return {"error": "unknown_tool"}

    def _clean_resume_text(self, text: str, user_id: str) -> str:
        """把用户粘贴的原始文本格式化为规范的 Markdown 简历。"""
        resp = client.chat.completions.create(
            model=settings.llm_model,
            messages=[
                {"role": "system", "content": """你是一个简历格式整理助手。把用户粘贴的简历内容整理为规范的 Markdown 格式。

## 输出格式

```
# 姓名 — 目标岗位

个人总结（1-2句）

## 技能

- 熟练掌握: 技能1 / 技能2
- 熟悉: 技能3
- 了解: 技能4
- AI 辅助开发: 工具1 / 工具2

## 项目/工作经历

### 项目名 — 角色（时间段）

- 亮点1（量化结果）
- 亮点2

## 教育背景

- **学校名** — 学位 专业（年份）
```

## 规则
1. 只整理格式，不改变信息内容
2. 如果原文缺少某项（如没有总结），直接跳过
3. 只输出整理后的 Markdown，不要任何解释"""},
                {"role": "user", "content": text[:8000]},
            ],
            temperature=0.1,
        )
        return resp.choices[0].message.content or text

    def clear_session(self, user_id: str):
        self.sessions.pop(user_id, None)
        self._last_tailored.pop(user_id, None)
        self._langs.pop(user_id, None)
