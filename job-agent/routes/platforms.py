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
