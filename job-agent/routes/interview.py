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
    difficulty: str = Field(default="mid", description="难度: junior/mid/senior")
    force: bool = Field(default=False, description="跳过匹配度检查，强制开始")
    question_count: int = Field(default=5, ge=1, le=10, description="题目数量，默认5，范围1-10")


class AnswerRequest(BaseModel):
    answer: str = Field(..., min_length=1, description="回答内容")


def _build_profile_bio(user_id: str, position: str, company: str) -> str:
    """从用户画像构建面试背景文本，无简历时替代 resume_md."""
    import json
    from database import engine
    from sqlalchemy import text

    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT email, profile FROM users WHERE id = :uid"),
            {"uid": user_id},
        ).fetchone()

    profile = json.loads(row.profile) if row and row.profile else {}
    email = row.email if row else ""

    lines = []

    name = profile.get("name", "").strip()
    if name:
        lines.append(f"# {name}")

    # 求职意向
    actual_role = position or profile.get("target_role", "").strip() or "通用岗位"
    lines.append("## 求职意向")
    lines.append(f"- 目标岗位: {actual_role}")
    if company:
        lines.append(f"- 目标公司: {company}")
    target_industry = profile.get("target_industry", "").strip()
    if target_industry:
        lines.append(f"- 目标行业: {target_industry}")

    # 工作经历
    experience = profile.get("experience_summary", "").strip()
    if experience:
        lines.append(f"## 工作经历\n{experience}")

    # 个人总结
    summary_text = profile.get("summary", "").strip()
    if summary_text:
        lines.append(f"## 个人总结\n{summary_text}")

    # 项目
    projects = profile.get("projects", [])
    if projects:
        lines.append("## 项目经验")
        for p in projects:
            pname = p.get("name", "") if isinstance(p, dict) else ""
            pdesc = p.get("description", "") if isinstance(p, dict) else ""
            if pname:
                lines.append(f"- **{pname}**")
                if pdesc:
                    lines.append(f"  {pdesc}")

    # 如果画像几乎为空，至少给点上下文
    if not experience and not summary_text and not projects:
        lines.append(f"## 备注\n候选人正在寻找 {actual_role} 相关岗位，邮箱 {email}。")

    return "\n".join(lines)


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
        markdown = _build_profile_bio(user["id"], req.position, req.company)

    # position 为空时回退到画像中的目标岗位
    if not req.position.strip():
        import json
        from database import engine
        from sqlalchemy import text
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT profile FROM users WHERE id = :uid"), {"uid": user["id"]}
            ).fetchone()
        profile = json.loads(row.profile) if row and row.profile else {}
        req.position = profile.get("target_role", "").strip()

    from database import spend_points
    try:
        spend_points(user["id"], 200, "interview")
    except ValueError as e:
        return {"error": "no_points", "message": str(e) or "积分不足"}

    session = get_session(user["id"])
    try:
        session.start(
            resume_md=markdown,
            jd_text=req.jd_text,
            position=req.position,
            company=req.company,
            difficulty=req.difficulty,
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

    from database import engine
    from sqlalchemy import text
    reply = session.handle_answer(req.answer)
    if reply is None:
        return {"error": "internal", "message": "处理失败"}

    if session.status == "done" and session._last_scores:
        _update_interview_stats(user["id"], session._last_scores)

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


def _update_interview_stats(user_id: str, scores: dict):
    """累计更新用户的面试统计（profile.interview_stats）"""
    import json
    from database import engine
    from sqlalchemy import text

    dims = scores.get("dimensions", {})
    total = scores.get("total_score", 0)
    if not dims or not total:
        return

    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT profile FROM users WHERE id = :uid"), {"uid": user_id}
        ).fetchone()
        profile = json.loads(row.profile) if row and row.profile else {}
        stats = profile.get("interview_stats")

    if not stats or not isinstance(stats, dict) or "total_score_sum" not in stats:
        stats = {"total_sessions": 0, "total_score_sum": 0, "dim_sums": {}, "dim_counts": {}}

    stats["total_sessions"] += 1
    stats["total_score_sum"] += total
    stats["avg_score"] = round(stats["total_score_sum"] / stats["total_sessions"])
    stats["latest_score"] = total
    stats["latest_improvements"] = scores.get("improvements", [])[:3]
    stats["latest_strengths"] = scores.get("strengths", [])[:3]

    for dim, detail in dims.items():
        s = detail.get("score", 0)
        if not isinstance(s, (int, float)):
            continue
        if dim not in stats["dim_sums"]:
            stats["dim_sums"][dim] = 0
            stats["dim_counts"][dim] = 0
        stats["dim_sums"][dim] += s
        stats["dim_counts"][dim] += 1

    dims_avg = {}
    for dim in stats["dim_sums"]:
        dims_avg[dim] = round(stats["dim_sums"][dim] / stats["dim_counts"][dim])

    stats["dimensions_avg"] = dims_avg
    if dims_avg:
        stats["weakest"] = min(dims_avg, key=dims_avg.get)
        stats["strongest"] = max(dims_avg, key=dims_avg.get)

    with engine.connect() as conn:
        current = json.loads(
            conn.execute(
                text("SELECT profile FROM users WHERE id = :uid"), {"uid": user_id}
            ).fetchone().profile
        )
        current["interview_stats"] = stats
        conn.execute(
            text("UPDATE users SET profile = :p WHERE id = :uid"),
            {"p": json.dumps(current, ensure_ascii=False), "uid": user_id},
        )
        conn.commit()


@router.post("/api/interview/reset", summary="重置面试")
def reset_interview(user: dict = Depends(auth_user)):
    clear_session(user["id"])
    return {"status": "ok"}
