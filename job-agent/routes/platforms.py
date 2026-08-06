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
    """返回积分余额 + 今日收入（显示单位）"""
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

    return {"balance": round(internal / 10, 1), "today_earned": round(internal_earned / 10, 1)}


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
    education: Optional[dict] = Field(default=None, description="{school, major, degree}")
    skills: Optional[list] = Field(default=None, description="技能/能力列表")
    experience_summary: Optional[str] = Field(default=None, description="工作经历摘要")
    projects: Optional[list] = Field(default=None, description="项目/作品集 [{name, description, url}]")
    target_role: Optional[str] = Field(default=None, description="目标岗位")
    target_industry: Optional[str] = Field(default=None, description="目标行业")
    salary_min: Optional[int] = Field(default=None, description="最低薪资(K)")
    salary_max: Optional[int] = Field(default=None, description="最高薪资(K)")
    preferred_cities: Optional[list] = Field(default=None, description="意向城市列表")


@router.get("/api/me/profile", summary="获取用户画像")
def api_get_profile(user: dict = Depends(auth_user)):
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT profile FROM users WHERE id = :uid"), {"uid": user["id"]}
        ).fetchone()
    profile = json.loads(row.profile) if row and row.profile else {}
    # Merge defaults with saved values
    defaults = {
        "education": {"school": "", "major": "", "degree": ""},
        "skills": [],
        "experience_summary": "",
        "projects": [],
        "target_role": "",
        "target_industry": "",
        "salary_min": None,
        "salary_max": None,
        "preferred_cities": [],
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
        conn.commit()
    return {"id": tid, "status": "ok"}


@router.put("/api/me/growth-tasks/{task_id}", summary="更新成长任务")
def api_update_growth_task(task_id: str, req: GrowthTaskPayload, user: dict = Depends(auth_user)):
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT id FROM growth_tasks WHERE id = :tid AND user_id = :uid"),
            {"tid": task_id, "uid": user["id"]},
        ).fetchone()
        if not row:
            raise HTTPException(404, "任务不存在")
        conn.execute(
            text("UPDATE growth_tasks SET title = :title, category = :cat, status = :status, "
                 "sort_order = :sort WHERE id = :tid"),
            {"title": req.title, "cat": req.category, "status": req.status,
             "sort": req.sort_order, "tid": task_id},
        )
        conn.commit()
    return {"status": "ok"}


@router.delete("/api/me/growth-tasks/{task_id}", summary="删除成长任务")
def api_delete_growth_task(task_id: str, user: dict = Depends(auth_user)):
    with engine.connect() as conn:
        conn.execute(
            text("DELETE FROM growth_tasks WHERE id = :tid AND user_id = :uid"),
            {"tid": task_id, "uid": user["id"]},
        )
        conn.commit()
    return {"status": "ok"}
