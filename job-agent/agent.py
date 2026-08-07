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
from workflow import classify_intent, is_workflow_intent, execute_workflow

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

## 你能做什么

1. **管理多份简历**：师弟可以为不同方向打造多根"金箍棒"（简历），每根独立管理。
2. **分析岗位匹配度**：贴个 JD（招兵榜）过来，猴哥从技能、薪资、地点、成长空间等 7 个维度打分（0-100），指出匹配亮点和硬伤，给结论。嘴毒但真实。
3. **定制简历**：根据 JD，从师弟的简历中挑最相关的经历，重排顺序，打出一根针对性的金箍棒。只挑和排，绝不编造经历。
4. **生成招呼语**：一段 80-150 字的短话术，用于招聘平台打招呼。
5. **生成 Cover Letter**：一封正式求职信（250-400 字），适合邮件投递。
6. **搜索岗位**：帮师弟找招聘网站上的岗位。
7. **收藏岗位**：高评分岗位存下来，方便回头比较。
8. **STAR 故事库**：从师弟的简历里提炼面试故事，用于过招（面试）准备。
9. **简历诊断**：看师弟的简历写得怎么样，哪里要改。猴哥的眼睛可是火眼金睛。
10. **求职画像**：查看和更新师弟的求职画像（教育、技能、目标岗位、薪资等）。
11. **成长计划**：帮师弟管理学习任务，开始任务后猴哥会主动来问进展——就像当年菩提祖师盯着俺老孙练功一样。

## 工作流程

- 师弟第一次来 → 先问有没有简历。没有的话引导他去简历管理页面上传，或者直接在聊天框里粘贴。
- 师弟问"看看我的简历""有什么问题" → 先调 list_my_resumes 让师弟选，然后调 get_my_resume 读内容，逐段诊断给建议。
- 师弟粘贴 JD → 先让师弟选用哪份简历（多份的话），然后调 score_job 分析。评分 60 以上，主动问"要不要猴哥帮你量身打造一份定制简历和招呼语？"
- 师弟要定制简历 → 调 tailor_resume → 展示结果。师弟说「保存」时，用 save_last_resume。
- 师弟要招呼语 → 调 generate_pitch
- 师弟要 Cover Letter → 调 generate_cover
- 师弟要搜索岗位 → 调 search_jobs，展示结果，问要不要收藏
- 师弟要 STAR 故事 → 调 generate_star_stories
- 师弟说"收藏""保存"某个岗位 → 调 save_job
- 师弟问/改求职画像 → 先调 get_my_profile 看看现在有什么，再调 update_my_profile 更新
- 师弟说「用简历完善画像」→ 先调 get_my_resume 读简历，分析后必须调 update_my_profile 把提取到的字段写入，再调 add_my_task 把行动清单逐条加入成长计划。别只说话不动手。
- 师弟说想学什么/做什么 → 调 add_my_task 加入成长计划
- 师弟说「开始做」「完成了」「暂停」→ 调 update_my_task 更新状态

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
    {
        "type": "function",
        "function": {
            "name": "list_my_resumes",
            "description": "列出用户的所有简历。当用户询问「我有几份简历」或需要选择简历时调用。",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_my_resume",
            "description": "读取一份简历的完整内容（结构化 JSON）。用于查看简历具体写了什么、诊断简历问题、提出优化建议。用户问「看看我的简历」「简历有什么问题」「帮我改简历」时调用。",
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
            "description": "用户在聊天中粘贴了自由格式的简历文本（非 Markdown），调用此工具用 AI 整理为规范格式并自动保存。支持纯文本、PDF/Word 解析后的文本。",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "用户粘贴的简历全文"},
                    "name": {"type": "string", "description": "为这份简历起个名字，如「后端开发」「产品经理」"},
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
            "description": "保存或更新简历。传递你在对话中展示给用户的 Markdown 简历全文作为 markdown 参数。新建时不传 resume_id，修改时传入。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "简历名称"},
                    "markdown": {"type": "string", "description": "你在对话中展示给用户的 Markdown 简历全文（必传，后端自动解析为结构化 JSON）"},
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
            "description": "保存最近一次生成的定制简历。调用 tailor_resume 后，简历内容已自动暂存，此工具直接用暂存内容保存，无需传 markdown。用户说「保存」「存下来」时优先用这个。",
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
    {
        "type": "function",
        "function": {
            "name": "score_job",
            "description": "根据用户的简历对一份 JD 进行 7 维评分（0-100）。返回总分、各维度得分、匹配亮点、风险点、投递建议。如果用户有多份简历，先让用户选择用哪份。",
            "parameters": {
                "type": "object",
                "properties": {
                    "jd_text": {"type": "string", "description": "岗位 JD 全文"},
                    "resume_id": {"type": "string", "description": "简历 ID（来自 list_my_resumes），不传则使用默认简历"},
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
            "description": "根据 JD 从用户的完整简历中选取最相关的经历，生成一份针对该岗位的定制版 Markdown 简历。只选取和重排已有内容，不编造。",
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
            "description": "生成一段 80-150 字的招呼语，用于招聘平台（BOSS直聘等）的'立即沟通'场景。突出核心匹配点，简短有力。",
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
            "description": "生成一封 250-400 字的正式 Cover Letter / 求职信，适合邮件投递或海外岗位申请。",
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
            "description": "搜索岗位。用户描述他想找什么样的岗位（如「上海 Python 后端 3年经验」），通过搜索引擎查找招聘网站上的岗位列表。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词，如「Python 后端 上海」"},
                    "site": {"type": "string", "description": "限定招聘网站：zhipin（BOSS直聘）/shixiseng（实习僧）/zhilian（智联）/51job/lagou/all。默认 all。"},
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
            "description": "收藏/保存一个岗位，供后续查看和比较。",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "岗位名称"},
                    "company": {"type": "string", "description": "公司名"},
                    "platform": {"type": "string", "description": "来源平台（boss/zhilian/shixiseng/51job/lagou/other）"},
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
            "description": "列出用户已收藏/保存的岗位列表。",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_star_stories",
            "description": "从用户的简历中自动提取经历，扩展为 STAR（情境-任务-行动-结果）格式的面试故事。",
            "parameters": {
                "type": "object",
                "properties": {
                    "resume_id": {"type": "string", "description": "简历 ID，不传则使用默认简历"},
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_my_profile",
            "description": "获取用户的求职画像，包括教育背景、技能、经历摘要、项目、目标岗位/行业、薪资范围、意向城市。",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_my_profile",
            "description": "更新用户求职画像中的字段。只传需要更新的字段，不传的保持不变。",
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
    {
        "type": "function",
        "function": {
            "name": "list_my_tasks",
            "description": "获取用户的成长计划任务列表，包括待学习技能、待做事项等。",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_my_task",
            "description": "向成长计划中添加一个任务。",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "任务标题"},
                    "category": {"type": "string", "description": "类型: skill/project/action，默认 skill"},
                    "status": {"type": "string", "description": "状态: pending/in_progress/done，默认 pending"},
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
            "description": "更新成长计划任务的状态。用户说「开始学」「完成了」「暂停一下」时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "任务 ID（来自 list_my_tasks）"},
                    "status": {"type": "string", "description": "新状态: pending/in_progress/done/paused"},
                },
                "required": ["task_id", "status"],
                "additionalProperties": False,
            },
        },
    },
]

MAX_TOOL_ITERATIONS = 5
MAX_MESSAGES = 101


class Agent:
    def __init__(self):
        self.sessions: dict[str, list[dict]] = {}
        self._last_tailored: dict[str, str] = {}  # user_id → markdown（工作流约束：缓存最新生成的简历）
        self._langs: dict[str, str] = {}  # user_id → lang

    def _build_prompt(self, has_resume: bool, lang: str) -> str:
        prompt = SYSTEM_PROMPT if has_resume else ONBOARDING_PROMPT
        if lang == "en":
            prompt += "\n\n[IMPORTANT] You MUST respond in English only. Do NOT reply in Chinese. All tool call arguments (like title, name, content) must also be in English."
        else:
            prompt += "\n\n请始终用中文回复。"
        return prompt

    def chat(self, user_id: str, user_message: str) -> tuple[str, list[str]]:
        from database import engine, get_user_lang
        from sqlalchemy import text

        lang = get_user_lang(user_id)

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
            # Language changed → rebuild system prompt
            has_resume = len(self.sessions[user_id]) > 0
            prompt = self._build_prompt(has_resume, lang)
            self.sessions[user_id][0] = {"role": "system", "content": prompt}
            self._langs[user_id] = lang

        messages = self.sessions[user_id]

        # ── 工作流路由 ──
        intent = classify_intent(user_message)
        if is_workflow_intent(intent):
            reply, tool_calls_made = execute_workflow(
                intent, user_id, user_message, messages, TOOLS, self._execute,
            )
            # 将对话记录写入 session，保持上下文
            messages.append({"role": "user", "content": user_message})
            messages.append({"role": "assistant", "content": reply})
            if len(messages) > MAX_MESSAGES:
                self.sessions[user_id] = [messages[0]] + messages[-(MAX_MESSAGES - 1):]
            return reply, tool_calls_made

        messages.append({"role": "user", "content": user_message})

        tool_calls_made: list[str] = []

        for _ in range(MAX_TOOL_ITERATIONS):
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
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps(result, ensure_ascii=False, default=str),
                    })
            else:
                messages.append({"role": "assistant", "content": msg.content})

                if len(messages) > MAX_MESSAGES:
                    keep_from = len(messages) - 100
                    while keep_from > 1 and messages[keep_from].get("role") == "tool":
                        keep_from -= 1
                    self.sessions[user_id] = [messages[0]] + messages[keep_from:]

                return msg.content, tool_calls_made

        return "抱歉，处理超时，请重试。", tool_calls_made

    def _execute(self, name: str, args: dict, user_id: str) -> dict:
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
                markdown = self._clean_resume_text(text, user_id)
                result = save_resume(user_id, name_val, markdown)
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
                result = save_resume(user_id, name_val, markdown, resume_id)
                action = "已更新" if resume_id else "已保存"
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
            task_id = args.get("task_id", "")
            status = args.get("status", "")
            if not task_id:
                return {"error": "missing_task_id", "message": "请指定任务 ID"}
            if status not in ("pending", "in_progress", "done", "paused"):
                return {"error": "invalid_status", "message": f"无效状态: {status}"}
            # paused → 映射到 pending（暂不支持独立 paused 状态）
            db_status = "pending" if status == "paused" else status
            try:
                result = _do_update_growth_task(
                    task_id,
                    GrowthTaskPayload(title="", category="skill", status=db_status),
                    user_id,
                    trigger_engine=True,
                )
                return result
            except Exception as e:
                logger.exception("update_my_task error")
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
