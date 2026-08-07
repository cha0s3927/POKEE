"""
面试路由 — 模拟面试 API
"""
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from routes.auth import auth_user
from interview_engine import get_session, clear_session
from resume import get_default_resume, get_resume_content

router = APIRouter(tags=["interview"])


class StartRequest(BaseModel):
    position: str = Field(default="", description="岗位名称")
    company: str = Field(default="", description="公司名")
    jd_text: str = Field(default="", description="岗位 JD（可选）")
    resume_id: str = Field(default="", description="简历 ID，不传则用默认")
    force: bool = Field(default=False, description="跳过匹配度检查，强制开始")
    question_count: int = Field(default=5, ge=1, le=10, description="题目数量，默认5，范围1-10")


class AnswerRequest(BaseModel):
    answer: str = Field(..., min_length=1, description="回答内容")


@router.post("/api/interview/start", summary="开始模拟面试")
def start_interview(req: StartRequest, user: dict = Depends(auth_user)):
    resume_id = req.resume_id
    markdown = None
    if resume_id:
        markdown = get_resume_content(resume_id)
    if not markdown:
        r = get_default_resume(user["id"])
        if r:
            markdown = r["content"]
            resume_id = r["id"]
    if not markdown:
        return {"error": "no_resume", "message": "请先上传简历再开始模拟面试"}

    session = get_session(user["id"])
    try:
        session.start(
            resume_md=markdown,
            jd_text=req.jd_text,
            position=req.position,
            company=req.company,
            force=req.force,
            question_count=req.question_count,
        )
    except Exception as e:
        return {"error": "plan_failed", "message": f"生成面试题失败: {e}"}

    intro = session.first_question()
    if intro is None:
        return {"error": "no_questions", "message": "生成面试题失败，请重试"}

    return {
        "status": "started",
        "total": len(session.questions),
        "match_score": session._match_score,
        "match_note": session._match_note,
        "message": intro,
    }


@router.post("/api/interview/answer", summary="提交面试回答")
def submit_answer(req: AnswerRequest, user: dict = Depends(auth_user)):
    session = get_session(user["id"])
    if session.status != "waiting":
        return {"error": "not_waiting", "message": "当前没有等待回答的面试题"}

    from database import spend_points, engine
    from sqlalchemy import text
    try:
        spend_points(user["id"], 10, "interview")
    except ValueError as e:
        return {"error": "no_points", "message": str(e) or "积分不足"}

    reply = session.handle_answer(req.answer)
    if reply is None:
        return {"error": "internal", "message": "处理失败"}

    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT points FROM users WHERE id = :uid"),
            {"uid": user["id"]},
        ).fetchone()

    return {
        "status": session.status,
        "current": session.current_idx + 1,
        "total": len(session.questions),
        "message": reply,
        "balance": round((row.points if row else 0) / 10, 1),
    }


@router.post("/api/interview/reset", summary="重置面试")
def reset_interview(user: dict = Depends(auth_user)):
    clear_session(user["id"])
    return {"status": "ok"}
