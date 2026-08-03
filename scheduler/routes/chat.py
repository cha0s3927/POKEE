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
    if not check_rate(user["id"], max_req=20):
        raise HTTPException(status_code=429, detail="请求太频繁，请稍后再试")
    reply = agent.chat(user_message=req.message, user_id=user["id"])
    execute_tool("ack_notifications", {"user_id": user["id"]})
    return {"reply": reply}


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
