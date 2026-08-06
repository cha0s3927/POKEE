"""
聊天路由 — Agent 对话
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from routes.auth import auth_user

router = APIRouter(tags=["chat"])


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, description="用户消息")


@router.post("/api/chat", summary="Agent 对话")
def chat(req: ChatRequest, user: dict = Depends(auth_user)):
    from main import agent
    reply = agent.chat(user_id=user["id"], user_message=req.message)
    return {"reply": reply, "user_id": user["id"]}


@router.post("/api/reset", summary="重置会话")
def reset_session(user: dict = Depends(auth_user)):
    from main import agent
    agent.clear_session(user["id"])
    return {"status": "ok"}
