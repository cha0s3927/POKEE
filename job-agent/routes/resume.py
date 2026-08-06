"""
简历管理路由 — 多简历 CRUD + 定制生成（纯 Markdown）
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from pydantic import BaseModel, Field

from routes.auth import auth_user
from resume import (
    list_resumes, get_resume, save_resume, delete_resume,
    set_default_resume, get_default_resume, tailor_resume,
    get_resume_content, resume_to_markdown,
)
from cover_letter import generate_pitch, generate_cover

router = APIRouter(tags=["resume"])


class ResumePayload(BaseModel):
    name: str = Field(default="未命名简历", description="简历名称")
    content: str = Field(..., min_length=20, description="Markdown 简历内容")


class ResumeUpdatePayload(BaseModel):
    name: Optional[str] = Field(default=None, description="简历名称")
    content: Optional[str] = Field(default=None, min_length=20, description="Markdown 简历内容")


class TailorRequest(BaseModel):
    jd_text: str = Field(..., min_length=20, description="岗位 JD 全文")
    resume_id: Optional[str] = Field(default=None, description="简历 ID，不传则使用默认简历")


class CoverRequest(BaseModel):
    jd_text: str = Field(..., min_length=20, description="岗位 JD 全文")
    style: str = Field(default="pitch", description="pitch=招呼语, cover=正式 Cover Letter")
    resume_id: Optional[str] = Field(default=None, description="简历 ID，不传则使用默认简历")


# ── Multi-resume CRUD ──

@router.get("/api/resumes", summary="列出所有简历")
def api_list_resumes(user: dict = Depends(auth_user)):
    resumes = list_resumes(user["id"])
    return {"resumes": resumes}


@router.get("/api/resume", summary="获取默认简历内容")
def api_get_default_resume(user: dict = Depends(auth_user)):
    r = get_default_resume(user["id"])
    if not r:
        return {"exists": False, "content": None}
    return {"exists": True, "content": r["content"], "resume_id": r["id"], "name": r["name"]}


@router.get("/api/resume/{resume_id}", summary="获取指定简历")
def api_get_resume(resume_id: str, user: dict = Depends(auth_user)):
    r = get_resume(resume_id)
    if not r or r["user_id"] != user["id"]:
        raise HTTPException(404, "简历不存在")
    return {"exists": True, "content": r["content"], "name": r["name"], "resume_id": r["id"], "is_default": r["is_default"]}


@router.get("/api/resume/{resume_id}/markdown", summary="获取简历 Markdown")
def api_get_resume_markdown(resume_id: str, user: dict = Depends(auth_user)):
    r = get_resume(resume_id)
    if not r or r["user_id"] != user["id"]:
        raise HTTPException(404, "简历不存在")
    return {"markdown": r["content"], "name": r["name"], "resume_id": r["id"]}


@router.post("/api/resumes", summary="创建简历")
def api_create_resume(req: ResumePayload, user: dict = Depends(auth_user)):
    result = save_resume(user["id"], req.name, req.content)
    return result


@router.put("/api/resume", summary="更新默认简历（向后兼容）")
def api_save_default_resume(req: ResumePayload, user: dict = Depends(auth_user)):
    r = get_default_resume(user["id"])
    if r:
        result = save_resume(user["id"], req.name, req.content, r["id"])
    else:
        result = save_resume(user["id"], req.name, req.content)
    return result


@router.put("/api/resume/{resume_id}", summary="更新指定简历")
def api_update_resume(resume_id: str, req: ResumeUpdatePayload, user: dict = Depends(auth_user)):
    r = get_resume(resume_id)
    if not r or r["user_id"] != user["id"]:
        raise HTTPException(404, "简历不存在")
    name = req.name or r["name"]
    content = req.content or r["content"]
    result = save_resume(user["id"], name, content, resume_id)
    return result


@router.delete("/api/resume/{resume_id}", summary="删除简历")
def api_delete_resume(resume_id: str, user: dict = Depends(auth_user)):
    try:
        delete_resume(resume_id, user["id"])
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"status": "ok"}


@router.post("/api/resume/{resume_id}/default", summary="设为默认简历")
def api_set_default_resume(resume_id: str, user: dict = Depends(auth_user)):
    r = get_resume(resume_id)
    if not r or r["user_id"] != user["id"]:
        raise HTTPException(404, "简历不存在")
    set_default_resume(resume_id, user["id"])
    return {"status": "ok"}


# ── 文件上传解析 ──

@router.post("/api/resume/parse", summary="上传简历文件并解析为 Markdown")
async def api_parse_resume(file: UploadFile = File(...), user: dict = Depends(auth_user)):
    """上传 PDF/Word/Markdown/TXT 简历文件，AI 自动解析为 Markdown"""
    from resume_parser import parse_to_markdown, ALLOWED_EXTENSIONS, MAX_FILE_SIZE

    ext = "." + file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in ALLOWED_EXTENSIONS:
        allowed = ", ".join(ALLOWED_EXTENSIONS)
        raise HTTPException(400, f"不支持的格式，支持: {allowed}")

    content = await file.read()
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(400, f"文件过大，上限 {MAX_FILE_SIZE // 1024 // 1024}MB")

    try:
        markdown = parse_to_markdown(file.filename, content)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"解析失败: {e}")

    # 从 Markdown 第一行提取默认简历名
    first_line = markdown.strip().split("\n")[0].lstrip("# ").strip()
    name = first_line or "未命名"
    return {"name": name, "content": markdown, "raw_name": file.filename}


# ── 定制生成 ──

@router.post("/api/tailor-resume", summary="生成定制简历")
def api_tailor_resume(req: TailorRequest, user: dict = Depends(auth_user)):
    resume_id = req.resume_id
    if not resume_id:
        r = get_default_resume(user["id"])
        if r:
            resume_id = r["id"]
    if not resume_id:
        raise HTTPException(400, "请先上传简历")
    try:
        markdown = tailor_resume(resume_id, req.jd_text)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"markdown": markdown, "resume_id": resume_id}


@router.post("/api/cover-letter", summary="生成招呼语/Cover Letter")
def api_cover_letter(req: CoverRequest, user: dict = Depends(auth_user)):
    try:
        if req.style == "cover":
            text = generate_cover(user["id"], req.jd_text, req.resume_id)
        else:
            text = generate_pitch(user["id"], req.jd_text, req.resume_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"text": text}
