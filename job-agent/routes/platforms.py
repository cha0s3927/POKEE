"""
平台相关路由 — 岗位搜索 + 岗位收藏 + STAR 故事
"""
import json
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text

from database import engine
from routes.auth import auth_user

router = APIRouter(tags=["platforms"])


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Saved Jobs helpers ──

def save_job_for_user(user_id: str, data: dict) -> str:
    """保存岗位到数据库，返回 job ID"""
    jid = str(uuid.uuid4())
    now = now_iso()
    with engine.connect() as conn:
        conn.execute(
            text("""INSERT INTO saved_jobs (id, user_id, resume_id, title, company, platform, url, jd_text, score_total, score_details, verdict, created_at)
                 VALUES (:id, :uid, :rid, :title, :company, :platform, :url, :jd, :score, :details, :verdict, :now)"""),
            {
                "id": jid, "uid": user_id,
                "rid": data.get("resume_id", "") or None,
                "title": data.get("title", ""),
                "company": data.get("company", ""),
                "platform": data.get("platform", "other"),
                "url": data.get("url", ""),
                "jd": data.get("jd_text", ""),
                "score": data.get("score_total"),
                "details": json.dumps(data.get("score_details", {}), ensure_ascii=False),
                "verdict": data.get("verdict", ""),
                "now": now,
            },
        )
        conn.commit()
    return jid


def list_saved_jobs_for_user(user_id: str) -> List[Dict]:
    """列出用户收藏的岗位"""
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT * FROM saved_jobs WHERE user_id = :uid ORDER BY created_at DESC"),
            {"uid": user_id},
        ).fetchall()

    return [
        {
            "id": r.id, "title": r.title, "company": r.company,
            "platform": r.platform or "", "url": r.url or "",
            "jd_text": r.jd_text or "", "score_total": r.score_total,
            "score_details": json.loads(r.score_details) if r.score_details else {},
            "verdict": r.verdict or "", "created_at": r.created_at,
            "resume_id": r.resume_id or "",
        }
        for r in rows
    ]


# ── Pydantic models ──

class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1, description="搜索关键词")
    site: str = Field(default="all", description="限定平台: zhipin/shixiseng/zhilian/51job/lagou/all")


class SaveJobRequest(BaseModel):
    title: str = Field(..., description="岗位名称")
    company: str = Field(..., description="公司名")
    platform: str = Field(default="other")
    url: str = Field(default="")
    jd_text: str = Field(default="")
    score_total: Optional[float] = None
    score_details: Optional[dict] = None
    verdict: str = Field(default="")
    resume_id: Optional[str] = None


# ── Endpoints ──

@router.post("/api/search", summary="搜索岗位")
def api_search_jobs(req: SearchRequest, user: dict = Depends(auth_user)):
    try:
        from search import search_jobs
        results = search_jobs(req.query, req.site, user["id"])
        return results
    except Exception as e:
        raise HTTPException(500, f"搜索失败: {e}")


@router.get("/api/saved-jobs", summary="获取收藏岗位列表")
def api_list_saved_jobs(user: dict = Depends(auth_user)):
    return {"jobs": list_saved_jobs_for_user(user["id"])}


@router.post("/api/saved-jobs", summary="收藏岗位")
def api_save_job(req: SaveJobRequest, user: dict = Depends(auth_user)):
    jid = save_job_for_user(user["id"], req.model_dump())
    return {"status": "ok", "job_id": jid}


@router.delete("/api/saved-jobs/{job_id}", summary="删除收藏岗位")
def api_delete_saved_job(job_id: str, user: dict = Depends(auth_user)):
    with engine.connect() as conn:
        conn.execute(
            text("DELETE FROM saved_jobs WHERE id = :jid AND user_id = :uid"),
            {"jid": job_id, "uid": user["id"]},
        )
        conn.commit()
    return {"status": "ok"}


# ── STAR Stories ──

@router.get("/api/star-stories", summary="获取 STAR 故事列表")
def api_list_stories(resume_id: Optional[str] = None, user: dict = Depends(auth_user)):
    from interview import list_star_stories
    stories = list_star_stories(user["id"], resume_id)
    return {"stories": stories}


@router.post("/api/star-stories/generate", summary="从简历生成 STAR 故事")
def api_generate_stories(resume_id: Optional[str] = None, user: dict = Depends(auth_user)):
    from resume import get_resume_content, get_default_resume
    from interview import generate_star_stories

    content = None
    if resume_id:
        content = get_resume_content(resume_id)
    if not content:
        r = get_default_resume(user["id"])
        if r:
            content = r["content"]
            resume_id = r["id"]
    if not content:
        raise HTTPException(400, "请先上传简历")

    from database import spend_points
    try:
        spend_points(user["id"], 20, "star_stories")
    except ValueError as e:
        raise HTTPException(402, str(e))

    stories = generate_star_stories(user["id"], content, resume_id)
    return {"stories": stories, "total": len(stories)}


@router.delete("/api/star-stories/{story_id}", summary="删除 STAR 故事")
def api_delete_story(story_id: str, user: dict = Depends(auth_user)):
    from interview import delete_star_story
    delete_star_story(story_id, user["id"])
    return {"status": "ok"}


# ── 积分（与 POKEE 共享 points_ledger）──

@router.get("/api/me/points", summary="查询积分余额")
def api_get_points(user: dict = Depends(auth_user)):
    """返回积分余额 + 今日收入 + 最近一条变动"""
    from datetime import datetime

    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT points FROM users WHERE id = :uid"), {"uid": user["id"]}
        ).fetchone()
        internal = row.points if row else 0

        today = datetime.utcnow().strftime("%Y-%m-%d")
        row2 = conn.execute(
            text("SELECT COALESCE(SUM(amount), 0) AS earned FROM points_ledger "
                 "WHERE user_id = :uid AND amount > 0 AND date(created_at) = :today"),
            {"uid": user["id"], "today": today},
        ).fetchone()
        internal_earned = row2.earned if row2 else 0

        latest = conn.execute(
            text("SELECT amount, reason, created_at FROM points_ledger "
                 "WHERE user_id = :uid ORDER BY created_at DESC LIMIT 1"),
            {"uid": user["id"]},
        ).fetchone()

    latest_change = None
    if latest:
        latest_change = {
            "amount": round(latest.amount / 10, 1),
            "reason": latest.reason,
            "created_at": latest.created_at,
        }

    return {
        "balance": round(internal / 10, 1),
        "today_earned": round(internal_earned / 10, 1),
        "latest_change": latest_change,
    }


@router.post("/api/me/daily-bonus", summary="每日签到领积分")
def api_daily_bonus(user: dict = Depends(auth_user)):
    """每日首次签到 +5.0 积分"""
    from datetime import datetime

    today = datetime.utcnow().strftime("%Y-%m-%d")

    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT id FROM points_ledger "
                 "WHERE user_id = :uid AND reason = 'daily_login' AND date(created_at) = :today "
                 "LIMIT 1"),
            {"uid": user["id"], "today": today},
        ).fetchone()
        if row:
            pts = conn.execute(
                text("SELECT points FROM users WHERE id = :uid"), {"uid": user["id"]}
            ).fetchone()
            pts_earned = conn.execute(
                text("SELECT COALESCE(SUM(amount), 0) AS earned FROM points_ledger "
                     "WHERE user_id = :uid AND amount > 0 AND date(created_at) = :today"),
                {"uid": user["id"], "today": today},
            ).fetchone()
            internal = pts.points if pts else 0
            internal_earned = pts_earned.earned if pts_earned else 0
            return {"credited": False, "balance": round(internal / 10, 1),
                    "today_earned": round(internal_earned / 10, 1)}

    # 未签到，加 50 内部单位 (= 5.0 显示积分)
    now = datetime.utcnow().isoformat()
    with engine.connect() as conn:
        conn.execute(
            text("UPDATE users SET points = points + 50 WHERE id = :uid"),
            {"uid": user["id"]},
        )
        conn.execute(
            text("INSERT INTO points_ledger (user_id, amount, reason, created_at) "
                 "VALUES (:uid, 50, 'daily_login', :now)"),
            {"uid": user["id"], "now": now},
        )
        conn.commit()
        row = conn.execute(
            text("SELECT points FROM users WHERE id = :uid"), {"uid": user["id"]}
        ).fetchone()
        internal = row.points if row else 50

    return {"credited": True, "balance": round(internal / 10, 1), "today_earned": 5.0}


@router.get("/api/me/points-ledger", summary="积分流水")
def api_points_ledger(user: dict = Depends(auth_user)):
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT amount, reason, created_at FROM points_ledger "
                 "WHERE user_id = :uid ORDER BY created_at DESC LIMIT 30"),
            {"uid": user["id"]},
        ).fetchall()

    return {
        "ledger": [
            {"amount": r.amount, "display": round(r.amount / 10, 1),
             "reason": r.reason, "created_at": r.created_at}
            for r in rows
        ]
    }


# ── 用户画像 ──

class ProfilePayload(BaseModel):
    name: Optional[str] = Field(default=None, description="用户姓名")
    education: Optional[dict] = Field(default=None, description="{school, major, degree}")
    skills: Optional[list] = Field(default=None, description="技能/能力列表")
    experience_summary: Optional[str] = Field(default=None, description="工作经历摘要")
    summary: Optional[str] = Field(default=None, description="个人总结")
    projects: Optional[list] = Field(default=None, description="项目/作品集 [{name, description, url}]")
    target_role: Optional[str] = Field(default=None, description="目标岗位")
    target_industry: Optional[str] = Field(default=None, description="目标行业")
    target_locations: Optional[list] = Field(default=None, description="意向城市列表")
    salary_min: Optional[int] = Field(default=None, description="最低薪资(K)")
    salary_max: Optional[int] = Field(default=None, description="最高薪资(K)")
    salary_range: Optional[str] = Field(default=None, description="薪资范围显示")
    preferred_cities: Optional[list] = Field(default=None, description="意向城市列表(兼容)")
    years_of_experience: Optional[str] = Field(default=None, description="工作年限: 0-1/1-3/3-5/5-10/10+")
    job_search_status: Optional[str] = Field(default=None, description="求职状态: actively-looking/casually-browsing/preparing")
    current_status: Optional[str] = Field(default=None, description="在职状态: employed/unemployed/student")
    personality_notes: Optional[str] = Field(default=None, description="性格/偏好备注，AI 被动收集，不在对话中主动询问")


@router.get("/api/me/profile", summary="获取用户画像")
def api_get_profile(user: dict = Depends(auth_user)):
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT profile FROM users WHERE id = :uid"), {"uid": user["id"]}
        ).fetchone()
    profile = json.loads(row.profile) if row and row.profile else {}
    # Merge defaults with saved values
    defaults = {
        "name": "",
        "education": {"school": "", "major": "", "degree": ""},
        "skills": [],
        "experience_summary": "",
        "summary": "",
        "projects": [],
        "target_role": "",
        "target_industry": "",
        "target_locations": [],
        "salary_min": None,
        "salary_max": None,
        "salary_range": "",
        "preferred_cities": [],
        "years_of_experience": "",
        "job_search_status": "exploring",
        "current_status": "",
        "personality_notes": "",
    }
    for k, v in defaults.items():
        if k not in profile:
            profile[k] = v
    return {"profile": profile}


@router.put("/api/me/profile", summary="更新用户画像")
def api_update_profile(req: ProfilePayload, user: dict = Depends(auth_user)):
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT profile FROM users WHERE id = :uid"), {"uid": user["id"]}
        ).fetchone()
        current = json.loads(row.profile) if row and row.profile else {}

    for key, val in req.model_dump(exclude_none=True).items():
        current[key] = val

    with engine.connect() as conn:
        conn.execute(
            text("UPDATE users SET profile = :p WHERE id = :uid"),
            {"p": json.dumps(current, ensure_ascii=False), "uid": user["id"]},
        )
        conn.commit()
    return {"profile": current}


# ── 成长计划 ──

class GrowthTaskPayload(BaseModel):
    title: str = Field(..., min_length=1, description="事项内容")
    category: str = Field(default="skill", description="类型: skill/project/action")
    status: str = Field(default="pending", description="状态: pending/in_progress/done")
    sort_order: int = Field(default=0)


@router.get("/api/me/growth-tasks", summary="获取成长计划列表")
def api_list_growth_tasks(user: dict = Depends(auth_user)):
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT id, title, category, status, sort_order, created_at "
                 "FROM growth_tasks WHERE user_id = :uid ORDER BY sort_order, created_at DESC"),
            {"uid": user["id"]},
        ).fetchall()
    return {
        "tasks": [
            {"id": r.id, "title": r.title, "category": r.category,
             "status": r.status, "sort_order": r.sort_order, "created_at": r.created_at}
            for r in rows
        ]
    }


@router.post("/api/me/growth-tasks", summary="创建成长任务")
def api_create_growth_task(req: GrowthTaskPayload, user: dict = Depends(auth_user)):
    tid = str(uuid.uuid4())
    now = now_iso()
    with engine.connect() as conn:
        conn.execute(
            text("INSERT INTO growth_tasks (id, user_id, title, category, status, sort_order, created_at) "
                 "VALUES (:id, :uid, :title, :cat, :status, :sort, :now)"),
            {"id": tid, "uid": user["id"], "title": req.title, "cat": req.category,
             "status": req.status, "sort": req.sort_order, "now": now},
        )
        # 如果直接创建为 in_progress，初始化督促时间（首次督促在 20-28h 后）
        if req.status == "in_progress":
            conn.execute(
                text("UPDATE growth_tasks SET last_checkin_at = :now WHERE id = :tid"),
                {"now": now, "tid": tid},
            )
        conn.commit()
    return {"id": tid, "status": "ok"}


@router.put("/api/me/growth-tasks/{task_id}", summary="更新成长任务")
def api_update_growth_task(task_id: str, req: GrowthTaskPayload, user: dict = Depends(auth_user)):
    return _do_update_growth_task(task_id, req, user["id"])


def _do_update_growth_task(task_id: str, req: GrowthTaskPayload, user_id: str, trigger_engine: bool = False) -> dict:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT * FROM growth_tasks WHERE id = :tid AND user_id = :uid"),
            {"tid": task_id, "uid": user_id},
        ).fetchone()
        if not row:
            raise HTTPException(404, "任务不存在")

        old_status = row.status
        # Only update fields that are explicitly provided
        set_clauses = []
        params = {"tid": task_id}
        if req.title:
            set_clauses.append("title = :title")
            params["title"] = req.title
        if req.category:
            set_clauses.append("category = :cat")
            params["cat"] = req.category
        set_clauses.append("status = :status")
        params["status"] = req.status
        set_clauses.append("sort_order = :sort")
        params["sort"] = req.sort_order

        conn.execute(
            text(f"UPDATE growth_tasks SET {', '.join(set_clauses)} WHERE id = :tid"),
            params,
        )

        # 开始任务 → 重置督促状态（last_checkin_at=now，首次督促在 20-28h 后）
        if req.status == "in_progress" and old_status != "in_progress":
            now = now_iso()
            conn.execute(
                text("UPDATE growth_tasks SET last_checkin_at = :now, checkin_count = 0, "
                     "user_responded_at = NULL, silence_days = 0, last_tone = '' "
                     "WHERE id = :tid"),
                {"tid": task_id, "now": now},
            )

        # 完成任务 → 记录完成时间
        if req.status == "done":
            conn.execute(
                text("UPDATE growth_tasks SET completed_at = :now WHERE id = :tid"),
                {"now": now_iso(), "tid": task_id},
            )

        conn.commit()

    # 触发成长引擎（在 commit 之后，确保引擎读到最新状态）
    if trigger_engine and req.status == "in_progress":
        try:
            from growth_engine import process_due_tasks
            process_due_tasks()
        except Exception:
            pass

    return {"status": "ok"}


# ── 成长督促 ──

@router.get("/api/me/growth-checkins", summary="获取未读督促消息")
def api_list_checkins(user: dict = Depends(auth_user)):
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT gc.*, gt.title as task_title, gt.category as task_category "
                 "FROM growth_checkins gc JOIN growth_tasks gt ON gc.task_id = gt.id "
                 "WHERE gc.user_id = :uid AND gc.direction = 'out' "
                 "ORDER BY gc.created_at DESC LIMIT 20"),
            {"uid": user["id"]},
        ).fetchall()
    return {
        "checkins": [
            {"id": r.id, "task_id": r.task_id, "task_title": r.task_title,
             "task_category": r.task_category, "message": r.message,
             "tone": r.tone, "created_at": r.created_at}
            for r in rows
        ]
    }


class RespondPayload(BaseModel):
    reply: str = Field(default="", description="用户回复内容")


@router.post("/api/me/growth-tasks/{task_id}/respond", summary="回应督促消息")
def api_respond_checkin(task_id: str, req: RespondPayload, user: dict = Depends(auth_user)):
    """用户回应督促。req: {reply: str} """
    reply = req.reply or ""
    cid = str(uuid.uuid4())
    now = now_iso()
    with engine.connect() as conn:
        conn.execute(
            text("INSERT INTO growth_checkins (id, task_id, user_id, direction, message, tone, created_at) "
                 "VALUES (:id, :tid, :uid, 'in', :msg, '', :now)"),
            {"id": cid, "tid": task_id, "uid": user["id"], "msg": reply, "now": now},
        )
        conn.execute(
            text("UPDATE growth_tasks SET user_responded_at = :now, silence_days = 0 "
                 "WHERE id = :tid"),
            {"now": now, "tid": task_id},
        )
        conn.commit()
    return {"status": "ok", "checkin_id": cid}


@router.post("/api/me/growth-tasks/{task_id}/followup", summary="生成督促后续回复")
def api_growth_followup(task_id: str, req: RespondPayload, user: dict = Depends(auth_user)):
    """用户回应后，LLM 生成一句结尾回复。req: {reply: str} """
    reply = req.reply or ""

    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT title, last_tone FROM growth_tasks WHERE id = :tid AND user_id = :uid"),
            {"tid": task_id, "uid": user["id"]},
        ).fetchone()

    if not row:
        raise HTTPException(404, "任务不存在")

    task_title = row.title
    last_tone = row.last_tone or ""
    tone_labels = {
        "curious": "好奇型", "supportive": "支持型", "challenging": "挑战型",
        "humorous": "幽默型", "structured": "结构化型",
    }
    tone_desc = tone_labels.get(last_tone, "支持型")

    prompt = f"""你是孙悟空，花果山求职道场的掌门。你刚问师弟/师妹「{task_title}」的进度，师弟/师妹回复了。

师弟/师妹的回复：{reply}
上次督促的语气：{tone_desc}

请以猴哥的口吻回一句 30-60 字的结尾。规则：
1. 自称"俺老孙"或"猴哥"，称用户"师弟/师妹"
2. 如果师弟说完成了 → 狠狠夸，别敷衍，像猴哥看到师弟练成七十二变那样高兴
3. 如果师弟说还在做 → 鼓励一句，给点小建议，别啰嗦
4. 如果师弟说暂停 → 表示理解，说随时可以再开始
5. 保持和上次督促一致的语感
6. 只输出消息文本，不要加引号、前缀或解释"""

    try:
        from openai import OpenAI
        import httpx
        client = OpenAI(
            api_key=__import__("config").settings.llm_api_key,
            base_url=__import__("config").settings.llm_base_url,
            http_client=httpx.Client(transport=httpx.HTTPTransport(retries=0), timeout=httpx.Timeout(20.0, connect=5.0)),
        )
        resp = client.chat.completions.create(
            model=__import__("config").settings.llm_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.85,
            max_tokens=200,
        )
        message = resp.choices[0].message.content.strip()
    except Exception as e:
        logger = __import__("logging").getLogger(__name__)
        logger.warning(f"Followup LLM failed: {e}")
        if "完成" in reply:
            message = "师弟好样的！俺老孙就知道你能行，这一关过了，离拿下 offer 又近了一步！"
        elif "暂停" in reply:
            message = "没事师弟，歇一歇也好。啥时候想继续了，猴哥随时在这儿。"
        else:
            message = "好的师弟，稳步前进就是好事。有啥难题随时来找猴哥！"

    cid = str(uuid.uuid4())
    now = now_iso()
    with engine.connect() as conn:
        conn.execute(
            text("INSERT INTO growth_checkins (id, task_id, user_id, direction, message, tone, created_at) "
                 "VALUES (:id, :tid, :uid, 'out', :msg, :tone, :now)"),
            {"id": cid, "tid": task_id, "uid": user["id"], "msg": message, "tone": last_tone, "now": now},
        )
        conn.commit()

    return {"status": "ok", "checkin_id": cid, "message": message}


@router.delete("/api/me/growth-tasks/{task_id}", summary="删除成长任务")
def api_delete_growth_task(task_id: str, user: dict = Depends(auth_user)):
    with engine.connect() as conn:
        conn.execute(
            text("DELETE FROM growth_tasks WHERE id = :tid AND user_id = :uid"),
            {"tid": task_id, "uid": user["id"]},
        )
        conn.commit()
    return {"status": "ok"}


# ── 调试：手动触发督促（模拟时间流逝）──

from pydantic import BaseModel as DebugBaseModel


class TriggerCheckinRequest(DebugBaseModel):
    task_id: str = Field(default="", description="指定任务 ID，不传则处理所有 in_progress 任务")
    hours_ago: int = Field(default=25, ge=1, le=168, description="模拟多少小时前最后一次督促，默认 25h")


@router.post("/api/debug/trigger-checkin", summary="[调试] 手动触发督促")
def api_trigger_checkin(req: TriggerCheckinRequest, user: dict = Depends(auth_user)):
    """回退 last_checkin_at 时间，然后立即触发督促引擎。用于测试。"""
    from datetime import datetime, timedelta, timezone

    fake_past = (datetime.now(timezone.utc) - timedelta(hours=req.hours_ago)).isoformat()

    with engine.connect() as conn:
        if req.task_id:
            conn.execute(
                text("UPDATE growth_tasks SET last_checkin_at = :t WHERE id = :tid AND user_id = :uid"),
                {"t": fake_past, "tid": req.task_id, "uid": user["id"]},
            )
        else:
            conn.execute(
                text("UPDATE growth_tasks SET last_checkin_at = :t WHERE status = 'in_progress' AND user_id = :uid"),
                {"t": fake_past, "uid": user["id"]},
            )
        conn.commit()

    from growth_engine import process_due_tasks
    process_due_tasks()

    # 读取生成的 checkin
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT gc.*, gt.title as task_title FROM growth_checkins gc "
                 "JOIN growth_tasks gt ON gc.task_id = gt.id "
                 "WHERE gc.user_id = :uid AND gc.direction = 'out' "
                 "ORDER BY gc.created_at DESC LIMIT 10"),
            {"uid": user["id"]},
        ).fetchall()

    return {
        "triggered": True,
        "hours_ago": req.hours_ago,
        "checkins": [
            {"id": r.id, "task_id": r.task_id, "task_title": r.task_title,
             "message": r.message, "tone": r.tone, "created_at": r.created_at}
            for r in rows
        ],
    }


# ── 语言偏好 ──

@router.get("/api/settings/lang", summary="获取当前语言偏好")
def api_get_lang(user: dict = Depends(auth_user)):
    from database import get_user_lang
    lang = get_user_lang(user["id"])
    return {"lang": lang}


@router.put("/api/settings/lang", summary="切换语言偏好")
def api_set_lang(req: dict, user: dict = Depends(auth_user)):
    l = req.get("lang", "zh")
    if l not in ("zh", "en"):
        l = "zh"
    with engine.connect() as conn:
        conn.execute(
            text("UPDATE users SET lang = :l WHERE id = :uid"),
            {"l": l, "uid": user["id"]},
        )
        conn.commit()
    return {"status": "ok", "lang": l}
