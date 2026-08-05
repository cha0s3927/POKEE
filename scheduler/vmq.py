"""
V免签精简服务端 — 订单创建 + APK 收款推送 + 金额匹配 + 回调
"""
import hashlib
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from config import settings
from database import engine, add_points

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo
TZ = ZoneInfo("Asia/Shanghai")

# ── 通讯密钥（APK 与服务器之间的签名 key）──
VMQ_KEY = settings.vmq_key
HOST_PORT = settings.vmq_host_port

# ── QR 码存放目录 ──
QR_DIR = Path(__file__).parent / "static" / "qr"
QR_DIR.mkdir(parents=True, exist_ok=True)

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


def _make_sign(params: list, key: str = None) -> str:
    """V免签签名：md5(参数值拼接 + key)"""
    if key is None:
        key = VMQ_KEY
    raw = "".join(str(p) for p in params) + key
    return hashlib.md5(raw.encode()).hexdigest()


def verify_app_push(type_: int, price: float, t: str, sign: str) -> bool:
    """验证 APK 推送的签名"""
    expected = _make_sign([type_, price, t])
    return sign == expected


def create_vmq_order(user_id: str, method: str, amount_yuan: float,
                     points: int) -> dict:
    """创建支付订单，返回 {trade_order_id, pay_url}"""
    trade_order_id = datetime.now(TZ).strftime("%Y%m%d%H%M%S") + str(uuid.uuid4())[:8]

    rid = str(uuid.uuid4())[:12]
    with Session(engine) as session:
        # 确保表存在
        session.execute(text("""
            CREATE TABLE IF NOT EXISTS payment_orders (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                trade_order_id TEXT NOT NULL UNIQUE,
                payment_method TEXT NOT NULL,
                amount_yuan REAL NOT NULL,
                points INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                paid_at TEXT
            )
        """))
        session.commit()

        session.execute(
            text("INSERT INTO payment_orders (id, user_id, trade_order_id, "
                 "payment_method, amount_yuan, points, status, created_at) "
                 "VALUES (:id, :uid, :tid, :method, :amount, :points, 'pending', :now)"),
            {"id": rid, "uid": user_id, "tid": trade_order_id,
             "method": method, "amount": amount_yuan, "points": points,
             "now": _now_iso()},
        )
        session.commit()

    return {
        "trade_order_id": trade_order_id,
        "pay_url": f"/vmq/pay/{trade_order_id}",
    }


def process_app_push(type_: int, price: float) -> dict:
    """处理 APK 收款推送：匹配订单 → 标记支付 → 加积分"""
    method_map = {1: "wechat", 2: "alipay"}
    method = method_map.get(type_, "unknown")

    # 匹配金额最接近的待支付订单（±0.02 容差）
    with Session(engine) as session:
        rows = session.execute(
            text("SELECT * FROM payment_orders WHERE status='pending' "
                 "AND payment_method=:method "
                 "AND ABS(amount_yuan - :price) <= 0.02 "
                 "ORDER BY created_at ASC LIMIT 1"),
            {"method": method, "price": price},
        ).fetchall()

        if not rows:
            return {"matched": False, "message": "no pending order found"}

        order = dict(rows[0]._mapping)

        # 更新订单状态
        session.execute(
            text("UPDATE payment_orders SET status='paid', paid_at=:now "
                 "WHERE trade_order_id=:tid"),
            {"tid": order["trade_order_id"], "now": _now_iso()},
        )
        session.commit()

    # 加积分
    add_points(order["user_id"], order["points"] * 10, "recharge",
               order["trade_order_id"])

    return {"matched": True, "trade_order_id": order["trade_order_id"],
            "user_id": order["user_id"], "amount": order["amount_yuan"],
            "points": order["points"]}


def get_order_status(trade_order_id: str) -> Optional[dict]:
    """查询订单状态"""
    with Session(engine) as session:
        row = session.execute(
            text("SELECT trade_order_id, payment_method, amount_yuan, points, "
                 "status, created_at, paid_at "
                 "FROM payment_orders WHERE trade_order_id = :tid"),
            {"tid": trade_order_id},
        ).fetchone()
    return dict(row._mapping) if row else None


def get_qr_image(method: str) -> Optional[str]:
    """获取收款码图片路径"""
    ext_map = {"wechat": "wechat.jpg", "alipay": "alipay.jpg"}
    filename = ext_map.get(method)
    if not filename:
        return None
    path = QR_DIR / filename
    return str(path) if path.exists() else None
