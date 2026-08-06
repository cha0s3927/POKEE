"""
Chat routes — /api/chat, /api/reset, notifications
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from routes.auth import auth_user, check_rate

router = APIRouter(tags=["chat"])


class ChatRequest(BaseModel):
    message: str = Field(description="用户消息")


@router.post("/api/chat", summary="Agent 对话")
def chat(req: ChatRequest, user: dict = Depends(auth_user)):
    from main import agent, execute_tool
    from database import get_user_persona, try_daily_bonus
    if not check_rate(user["id"], max_req=20):
        raise HTTPException(status_code=429, detail="请求太频繁，请稍后再试")
    bonus = try_daily_bonus(user["id"])
    persona = get_user_persona(user["id"])
    reply = agent.chat(user_message=req.message, user_id=user["id"], persona=persona)
    execute_tool("ack_notifications", {"user_id": user["id"]})
    result = {"reply": reply}
    if bonus["credited"]:
        result["bonus"] = bonus
    return result


@router.post("/api/reset", summary="重置会话")
def reset_session(user: dict = Depends(auth_user)):
    from main import agent
    agent.clear_session(user["id"])
    return {"status": "ok"}


@router.get("/notifications", summary="获取未读通知")
def get_notifications(user: dict = Depends(auth_user)):
    from tools import execute_tool
    return execute_tool("get_notifications", {"user_id": user["id"]})


@router.post("/notifications/ack", summary="标记通知已读")
def ack_notifications(user: dict = Depends(auth_user)):
    from tools import execute_tool
    return execute_tool("ack_notifications", {"user_id": user["id"]})


@router.get("/now", summary="获取当前时间")
def get_current_time():
    from tools import execute_tool
    return execute_tool("get_current_time", {})


# ── 人设 ──

@router.get("/api/settings/persona", summary="获取当前人设")
def get_persona(user: dict = Depends(auth_user)):
    from agent import PERSONAS
    from database import get_user_persona
    key = get_user_persona(user["id"])
    info = PERSONAS.get(key, PERSONAS["default"])
    return {"key": key, "name": info["name"], "emoji": info["emoji"]}


@router.put("/api/settings/persona", summary="切换人设")
def set_persona(req: dict, user: dict = Depends(auth_user)):
    from database import engine
    from sqlalchemy import text
    key = req.get("persona", "default")
    if key not in ("default", "cute_girl", "reliable_guy"):
        raise HTTPException(status_code=400, detail="无效的人设")
    with engine.connect() as conn:
        conn.execute(
            text("UPDATE users SET persona = :p WHERE id = :uid"),
            {"p": key, "uid": user["id"]},
        )
        conn.commit()
    return {"status": "ok", "persona": key}


@router.get("/api/settings/personas", summary="列出所有人设")
def list_personas():
    from agent import PERSONAS
    return {k: {"name": v["name"], "emoji": v["emoji"]} for k, v in PERSONAS.items()}


# ── 语言 ──

@router.get("/api/settings/lang", summary="获取当前语言偏好")
def get_lang(user: dict = Depends(auth_user)):
    from database import get_user_lang
    lang = get_user_lang(user["id"])
    return {"lang": lang}


@router.put("/api/settings/lang", summary="切换语言偏好")
def set_lang(req: dict, user: dict = Depends(auth_user)):
    from database import engine
    from sqlalchemy import text
    l = req.get("lang", "zh")
    if l not in ("zh", "en"):
        raise HTTPException(status_code=400, detail="不支持的语言")
    with engine.connect() as conn:
        conn.execute(
            text("UPDATE users SET lang = :l WHERE id = :uid"),
            {"l": l, "uid": user["id"]},
        )
        conn.commit()
    return {"status": "ok", "lang": l}


# ── 积分 ──

@router.get("/api/me/points", summary="查询积分余额")
def get_points(user: dict = Depends(auth_user)):
    from database import get_user_points
    return get_user_points(user["id"])
