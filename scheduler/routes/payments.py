"""
支付路由 — 积分包 / 创建订单 / 查询状态
"""
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from routes.auth import auth_user
from payments import create_order, get_order_status
from vmq import PACKAGES, PAYMENT_METHODS

router = APIRouter(prefix="/api/payments", tags=["payments"])


class CreateOrderBody(BaseModel):
    method: str
    amount: float
    points: int


@router.get("/packages", summary="积分包列表")
def packages():
    return {"packages": PACKAGES, "methods": PAYMENT_METHODS}


@router.post("/create", summary="创建充值订单")
def api_create_order(body: CreateOrderBody, user: dict = Depends(auth_user)):
    result = create_order(user["id"], body.method, body.amount, body.points)
    return result


@router.get("/status/{trade_order_id}", summary="查询订单状态")
def api_order_status(trade_order_id: str, user: dict = Depends(auth_user)):
    order = get_order_status(trade_order_id)
    if not order:
        return {"error": "not found"}
    return {"trade_order_id": order["trade_order_id"],
            "status": order["status"],
            "amount_yuan": order["amount_yuan"],
            "points": order["points"]}
