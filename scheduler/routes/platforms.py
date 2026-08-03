"""
Platform routes — IM 平台连接状态、QR、Chat/Reset 端点
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from adapters import adapter_registry, get as get_adapter, set_pending_binding, try_auto_bind
from database import engine
from routes.auth import auth_user

router = APIRouter(tags=["platforms"])


# ── 平台连接状态 ──

@router.get("/api/platforms/status", summary="IM 平台连接状态（per-user）")
def platforms_status(user: dict = Depends(auth_user)):
    web_user_id = user["id"]
    with Session(engine) as session:
        rows = session.execute(
            text("SELECT platform FROM user_im_bindings WHERE user_id = :uid"),
            {"uid": web_user_id},
        ).fetchall()
    bindings = {r.platform for r in rows}
    return {
        "web": {"connected": True, "label": "Web", "has_qr": False},
        "wechat": {
            "connected": "wechat" in bindings,
            "label": "微信",
            "how": "在微信中给 Bot 发消息即可使用",
            "has_qr": True,
        },
        "feishu": {
            "connected": "feishu" in bindings,
            "label": "飞书",
            "how": "在飞书中与 Bot 对话即可使用",
            "has_qr": True,
        },
        "whatsapp": {
            "connected": "whatsapp" in bindings,
            "label": "WhatsApp",
            "how": "在 WhatsApp 中给自己发消息即可使用",
            "has_qr": True,
        },
    }


# ── 平台 QR ──

@router.get("/api/platforms/qr/wechat", summary="微信登录二维码")
def qr_wechat(user: dict = Depends(auth_user)):
    web_user_id = user["id"]
    with Session(engine) as session:
        row = session.execute(
            text("SELECT im_user_id FROM user_im_bindings WHERE user_id=:uid AND platform='wechat'"),
            {"uid": web_user_id},
        ).fetchone()
    if row:
        return {"connected": True, "qr_image": ""}
    set_pending_binding(web_user_id, "wechat")
    adapter = get_adapter("wechat")
    if adapter:
        return adapter.get_qr(web_user_id)
    return {"connected": False, "error": "adapter not available"}


@router.get("/api/platforms/qr/whatsapp", summary="WhatsApp 登录二维码")
def qr_whatsapp(user: dict = Depends(auth_user)):
    web_user_id = user["id"]
    with Session(engine) as session:
        row = session.execute(
            text("SELECT im_user_id FROM user_im_bindings WHERE user_id=:uid AND platform='whatsapp'"),
            {"uid": web_user_id},
        ).fetchone()
    if row:
        return {"connected": True, "qr_image": ""}
    set_pending_binding(web_user_id, "whatsapp")
    adapter = get_adapter("whatsapp")
    if adapter:
        return adapter.get_qr(web_user_id)
    return {"connected": False, "error": "adapter not available"}


@router.post("/api/platforms/qr/whatsapp/pairing", summary="请求 WhatsApp 配对码")
def whatsapp_pairing(user: dict = Depends(auth_user), body: dict = None):
    web_user_id = user["id"]
    adapter = get_adapter("whatsapp")
    if adapter and hasattr(adapter, "request_pairing_code"):
        phone = (body or {}).get("phone", "")
        return adapter.request_pairing_code(phone, web_user_id)
    return {"error": "adapter not available"}


@router.get("/api/platforms/qr/feishu", summary="飞书 Bot 连接 — device-code 扫码创建")
def qr_feishu(user: dict = Depends(auth_user)):
    web_user_id = user["id"]
    adapter = get_adapter("feishu")
    if not adapter:
        return {"error": "adapter not available"}
    return adapter.get_qr(web_user_id)


@router.post("/api/platforms/qr/feishu/poll", summary="轮询飞书 device-code 注册状态")
def poll_feishu(user: dict = Depends(auth_user), body: dict = None):
    device_code = (body or {}).get("device_code", "")
    if not device_code:
        return {"status": "not_found"}
    adapter = get_adapter("feishu")
    if not adapter:
        return {"status": "not_found"}
    result = adapter.poll_registration(user["id"], device_code)
    if result.get("status") == "success":
        from scheduler import push_sse
        push_sse({"user_id": user["id"], "type": "platform_connected", "platform": "feishu"})
    return result


# ── IM Chat 端点（各平台消息入口）──

class IMChatRequest(BaseModel):
    message: str = Field(description="用户消息")
    conversation_id: str = Field(description="IM 会话 ID")
    secret: str = Field(description="共享密钥")


class IMResetRequest(BaseModel):
    conversation_id: str = Field(description="IM 会话 ID")
    secret: str = Field(description="共享密钥")


def _resolve_and_chat(platform: str, secret: str, expected_secret: str,
                       im_id: str, message: str) -> dict:
    """通用 IM 消息处理：验证密钥 → 解析用户 → agent.chat → 返回"""
    from main import agent, execute_tool
    if secret != expected_secret:
        raise HTTPException(status_code=403, detail="密钥错误")

    # 查绑定
    with Session(engine) as session:
        row = session.execute(
            text("SELECT user_id FROM user_im_bindings WHERE platform=:p AND im_user_id=:imuid"),
            {"p": platform, "imuid": im_id},
        ).fetchone()
    if row:
        user_id = row.user_id
    else:
        bound = try_auto_bind(platform, im_id)
        user_id = bound or f"{platform}:{im_id}"

    reply = agent.chat(user_message=message, user_id=user_id)
    execute_tool("ack_notifications", {"user_id": user_id})
    return {"reply": reply}


def _resolve_and_reset(platform: str, secret: str, expected_secret: str,
                        im_id: str) -> dict:
    """通用 IM 会话重置"""
    from main import agent
    if secret != expected_secret:
        raise HTTPException(status_code=403, detail="密钥错误")

    with Session(engine) as session:
        row = session.execute(
            text("SELECT user_id FROM user_im_bindings WHERE platform=:p AND im_user_id=:imuid"),
            {"p": platform, "imuid": im_id},
        ).fetchone()
    user_id = row.user_id if row else f"{platform}:{im_id}"
    agent.clear_session(user_id)
    return {"status": "ok"}


# ── WeChat ──

@router.post("/api/wechat/chat", summary="微信 Agent 对话")
def wechat_chat(req: IMChatRequest):
    from config import settings
    return _resolve_and_chat("wechat", req.secret, settings.wechat_secret,
                              req.conversation_id, req.message)


@router.post("/api/wechat/reset", summary="重置微信会话")
def wechat_reset(req: IMResetRequest):
    from config import settings
    return _resolve_and_reset("wechat", req.secret, settings.wechat_secret,
                               req.conversation_id)


# ── WhatsApp ──

@router.post("/api/whatsapp/chat", summary="WhatsApp Agent 对话")
def whatsapp_chat(req: IMChatRequest):
    from config import settings
    return _resolve_and_chat("whatsapp", req.secret, settings.whatsapp_secret,
                              req.conversation_id, req.message)


@router.post("/api/whatsapp/reset", summary="重置 WhatsApp 会话")
def whatsapp_reset(req: IMResetRequest):
    from config import settings
    return _resolve_and_reset("whatsapp", req.secret, settings.whatsapp_secret,
                               req.conversation_id)


# ── LinkedIn ──

@router.post("/api/linkedin/chat", summary="LinkedIn Agent 对话")
def linkedin_chat(req: IMChatRequest):
    from config import settings
    result = _resolve_and_chat("linkedin", req.secret, settings.linkedin_secret,
                                req.conversation_id, req.message)
    # 额外：通过 linkedin_bot 发送回复
    adapter = get_adapter("linkedin")
    if adapter and adapter.api:
        adapter.api.send_message(result["reply"], conversation_urn_id=req.conversation_id)
    return result


@router.post("/api/linkedin/reset", summary="重置 LinkedIn 会话")
def linkedin_reset(req: IMResetRequest):
    from config import settings
    return _resolve_and_reset("linkedin", req.secret, settings.linkedin_secret,
                               req.conversation_id)
