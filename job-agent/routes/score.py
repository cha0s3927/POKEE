"""
JD 评分路由
"""
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from routes.auth import auth_user
from resume import get_resume_content, get_default_resume
from engine import score_job
from database import spend_points

router = APIRouter(tags=["score"])


class ScoreRequest(BaseModel):
    jd_text: str = Field(..., min_length=20, description="岗位 JD 全文")
    resume_id: Optional[str] = Field(default=None, description="简历 ID，不传则使用默认简历")


class ScoreResponse(BaseModel):
    total: float
    dimensions: Dict[str, float]
    strengths: List[str]
    weaknesses: List[str]
    verdict: str
    verdict_reason: str
    resume_id: Optional[str] = None


@router.post("/api/score", summary="JD 评分")
def api_score(req: ScoreRequest, user: dict = Depends(auth_user)):
    content = None
    resume_id = req.resume_id
    if resume_id:
        content = get_resume_content(resume_id)
        if not content:
            raise HTTPException(404, "简历不存在")

    if not content:
        r = get_default_resume(user["id"])
        if r:
            content = r["content"]
            resume_id = r["id"]

    if not content:
        raise HTTPException(400, "请先在「简历管理」中上传简历")

    try:
        spend_points(user["id"], 30, "jd_score")
    except ValueError as e:
        raise HTTPException(402, str(e))

    result = score_job(content, req.jd_text)
    return {
        "total": result.total,
        "dimensions": result.dimensions,
        "strengths": result.strengths,
        "weaknesses": result.weaknesses,
        "verdict": result.verdict,
        "verdict_reason": result.verdict_reason,
        "resume_id": resume_id,
    }
