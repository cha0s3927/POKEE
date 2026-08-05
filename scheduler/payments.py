"""
积分支付 — V免签版（个人收款码 + Android 监控 APK）
"""
from vmq import create_vmq_order, get_order_status, PACKAGES, PAYMENT_METHODS


def create_order(user_id: str, method: str, amount_yuan: float,
                 points: int) -> dict:
    """创建支付订单，返回 {trade_order_id, pay_url}"""
    return create_vmq_order(user_id, method, amount_yuan, points)
