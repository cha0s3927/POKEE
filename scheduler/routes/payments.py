"""
Payment routes — 积分充值 API
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from routes.auth import auth_user

router = APIRouter(prefix="/api/payments", tags=["payments"])


class CreateOrderRequest(BaseModel):
    method: str = Field(description="支付方式: wechat | alipay")
    amount: float = Field(description="金额（元）")
    points: int = Field(description="积分数量")


@router.get("/packages", summary="获取积分包列表")
def list_packages():
    from payments import PACKAGES, PAYMENT_METHODS
    return {"packages": PACKAGES, "methods": PAYMENT_METHODS}


@router.post("/create", summary="创建充值订单")
def create(req: CreateOrderRequest, user: dict = Depends(auth_user)):
    from payments import create_order, PACKAGES

    if req.method not in ("wechat", "alipay"):
        raise HTTPException(status_code=400, detail="支付方式无效，可选: wechat, alipay")

    # 校验积分包
    valid = any(pkg["amount"] == req.amount and pkg["points"] == req.points for pkg in PACKAGES)
    if not valid:
        raise HTTPException(status_code=400, detail="积分包不匹配")

    from config import settings
    notify_base = settings.server_url.rstrip("/")
    notify_url = f"{notify_base}/api/payments/notify/{req.method}"

    result = create_order(
        user_id=user["id"],
        method=req.method,
        amount_yuan=req.amount,
        points=req.points,
        notify_url=notify_url,
    )
    if "error" in result:
        raise HTTPException(status_code=500, detail=result.get("message", "创建订单失败"))
    return result


@router.post("/notify/wx", summary="微信支付回调")
async def notify_wx(request: Request):
    from payments import process_callback
    try:
        data = await request.form()
        params = dict(data)
    except Exception:
        body = await request.body()
        import json
        params = json.loads(body) if body else {}
    result = process_callback("wechat", params)
    from starlette.responses import PlainTextResponse
    return PlainTextResponse(result)


@router.post("/notify/ali", summary="支付宝回调")
async def notify_ali(request: Request):
    from payments import process_callback
    try:
        data = await request.form()
        params = dict(data)
    except Exception:
        body = await request.body()
        import json
        params = json.loads(body) if body else {}
    result = process_callback("alipay", params)
    from starlette.responses import PlainTextResponse
    return PlainTextResponse(result)


@router.get("/status/{order_id}", summary="查询订单状态")
def order_status(order_id: str, user: dict = Depends(auth_user)):
    from payments import get_order_status
    order = get_order_status(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="订单不存在")
    return order
