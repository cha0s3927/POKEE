"""
虎皮椒支付 — 创建订单 + 验签 + 回调处理
"""
import hashlib
import json
import urllib.request
import uuid
from datetime import datetime
from typing import Optional, Tuple

from sqlalchemy import text
from sqlalchemy.orm import Session

from config import settings
from database import engine, add_points

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo
TZ = ZoneInfo(settings.tz_name)

# ── 积分包 ──
PACKAGES = [
    {"amount": 1, "points": 10, "label": "入门"},
    {"amount": 5, "points": 60, "label": "划算"},
    {"amount": 10, "points": 130, "label": "超值"},
    {"amount": 20, "points": 300, "label": "至尊"},
]

PAYMENT_METHODS = [
    {"key": "wechat", "label": "微信支付"},
    {"key": "alipay", "label": "支付宝"},
]


def _now_iso() -> str:
    return datetime.now(TZ).isoformat()


def _app_creds(method: str) -> Tuple[str, str, str]:
    """根据支付方式返回 (gateway, appid, appsecret)"""
    gw = settings.xunhupay_gateway
    if method == "wechat":
        return gw, settings.xunhupay_wx_appid, settings.xunhupay_wx_appsecret
    return gw, settings.xunhupay_ali_appid, settings.xunhupay_ali_appsecret


def make_sign(params: dict, appsecret: str) -> str:
    """虎皮椒签名：字典序排序 → key=value&… → 末尾追加 appsecret → MD5 32位小写"""
    sorted_keys = sorted(params.keys())
    raw = "&".join(f"{k}={params[k]}" for k in sorted_keys if params[k] != "")
    raw += appsecret
    return hashlib.md5(raw.encode()).hexdigest()


def create_order(user_id: str, method: str, amount_yuan: float, points: int,
                 notify_url: str, return_url: str = "") -> dict:
    """创建支付订单，返回 {trade_order_id, url, url_qrcode}"""
    gateway, appid, appsecret = _app_creds(method)
    trade_order_id = datetime.now(TZ).strftime("%Y%m%d%H%M%S") + str(uuid.uuid4())[:8]
    nonce_str = str(uuid.uuid4()).replace("-", "")[:32]

    params = {
        "appid": appid,
        "trade_order_id": trade_order_id,
        "total_fee": f"{amount_yuan:.2f}",
        "title": f"POKEE 积分充值 - {points}积分",
        "time": str(int(datetime.now(TZ).timestamp())),
        "nonce_str": nonce_str,
        "notify_url": notify_url,
        "return_url": return_url,
    }
    params["hash"] = make_sign(params, appsecret)

    body = json.dumps(params).encode()
    req = urllib.request.Request(
        f"{gateway}/payment/do.html",
        data=body,
        headers={"Content-Type": "application/json;charset=UTF-8"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        result = json.loads(resp.read().decode())

    if result.get("errcode") != 0:
        return {"error": result.get("errcode"), "message": result.get("errmsg", "创建订单失败")}

    # 入库
    rid = str(uuid.uuid4())[:12]
    with Session(engine) as session:
        session.execute(
            text("INSERT INTO payment_orders (id, user_id, trade_order_id, open_order_id, "
                 "payment_method, amount_yuan, points, status, created_at) "
                 "VALUES (:id, :uid, :tid, :oid, :method, :amount, :points, 'pending', :now)"),
            {"id": rid, "uid": user_id, "tid": trade_order_id,
             "oid": result.get("open_order_id", ""), "method": method,
             "amount": amount_yuan, "points": points, "now": _now_iso()},
        )
        session.commit()

    return {
        "trade_order_id": trade_order_id,
        "url": result.get("url", ""),
        "url_qrcode": result.get("url_qrcode", ""),
    }


def verify_sign(params: dict, method: str) -> bool:
    """验证回调签名"""
    _, _, appsecret = _app_creds(method)
    received_hash = params.get("hash", "")
    expected = make_sign({k: v for k, v in params.items() if k != "hash"}, appsecret)
    return received_hash == expected


def process_callback(method: str, data: dict) -> str:
    """处理支付回调。返回 "success" 给虎皮椒，或错误信息"""
    if not verify_sign(data, method):
        return "sign error"

    trade_order_id = data.get("trade_order_id", "")
    amount_yuan = float(data.get("total_fee", "0"))
    status = data.get("status", "")

    if status != "OD":
        return "success"  # 非支付成功状态，不处理

    with Session(engine) as session:
        row = session.execute(
            text("SELECT * FROM payment_orders WHERE trade_order_id = :tid"),
            {"tid": trade_order_id},
        ).fetchone()

        if not row:
            return f"order not found: {trade_order_id}"

        order = dict(row._mapping)
        if order["status"] == "paid":
            return "success"  # 幂等：已处理过

        # 校验金额
        if abs(order["amount_yuan"] - amount_yuan) > 0.01:
            return f"amount mismatch: expected {order['amount_yuan']}, got {amount_yuan}"

        # 更新订单状态
        session.execute(
            text("UPDATE payment_orders SET status='paid', paid_at=:now WHERE trade_order_id=:tid"),
            {"tid": trade_order_id, "now": _now_iso()},
        )
        session.commit()

    # 加积分（内部单位 ×10）
    add_points(order["user_id"], order["points"] * 10, "recharge", trade_order_id)
    return "success"


def get_order_status(trade_order_id: str) -> Optional[dict]:
    """查询订单"""
    with Session(engine) as session:
        row = session.execute(
            text("SELECT trade_order_id, payment_method, amount_yuan, points, status, created_at, paid_at "
                 "FROM payment_orders WHERE trade_order_id = :tid"),
            {"tid": trade_order_id},
        ).fetchone()
    return dict(row._mapping) if row else None
