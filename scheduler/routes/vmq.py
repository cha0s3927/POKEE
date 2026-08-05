"""
V免签 HTTP 端点 — appPush / 支付页面 / 订单创建
"""
import hashlib

from fastapi import APIRouter, Request, Query
from fastapi.responses import HTMLResponse, PlainTextResponse, JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from database import engine
from vmq import (verify_app_push, process_app_push, get_order_status,
                 get_qr_image, _now_iso)

router = APIRouter(tags=["vmq"])


# ── 根路径端点（APK 实际调用的路径，不在 /index/index 下面）──

@router.get("/appHeart", include_in_schema=False)
@router.post("/appHeart", include_in_schema=False)
async def app_heart_root(request: Request):
    """心跳 — APK 直接调用 /appHeart，不是 /index/index/appHeart"""
    return JSONResponse({"code": 1, "msg": "success"})


@router.get("/appPush", include_in_schema=False)
@router.post("/appPush", include_in_schema=False)
async def app_push_root(request: Request):
    return await _app_push_impl(request)


# ── 原版 V免签 PHP 兼容路径（APK 内置的端点）──

@router.get("/index/index/jk", include_in_schema=False)
def monitor_page():
    """模拟 V免签 PHP 监控端设置页面 — 显示配置 QR 码和参数"""
    from vmq import VMQ_KEY, HOST_PORT
    config_text = f"{HOST_PORT}/{VMQ_KEY}"
    return HTMLResponse(f"""
    <html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
    <title>监控端设置</title>
    <style>
      body{{font-family:-apple-system,sans-serif;background:#f5f5f5;margin:0;padding:20px}}
      .card{{max-width:420px;margin:0 auto;background:#fff;border-radius:12px;padding:24px;box-shadow:0 2px 8px rgba(0,0,0,.08)}}
      h2{{margin:0 0 16px;font-size:18px;text-align:center}}
      .row{{display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid #eee;font-size:14px}}
      .row .label{{color:#666}} .row .val{{color:#333;font-weight:500;word-break:break-all}}
      .qr-box{{text-align:center;margin:20px 0}}
      .qr-box img{{width:200px;height:200px}}
      .hint{{font-size:12px;color:#999;text-align:center;margin-top:8px}}
    </style></head><body>
    <div class="card">
      <h2>POKEE 支付监控端</h2>
      <div class="row"><span class="label">配置数据</span><span class="val" style="font-weight:700;color:#f59e0b">{config_text}</span></div>
      <div class="row"><span class="label">服务器地址</span><span class="val">{HOST_PORT}</span></div>
      <div class="row"><span class="label">通讯密钥</span><span class="val">{VMQ_KEY}</span></div>
      <div class="qr-box">
        <p style="font-size:13px;color:#333;margin-bottom:12px">APK 扫码配置</p>
        <img src="/static/vmq-config.png" alt="配置二维码">
      </div>
      <p class="hint">用监控端 App 扫描上方二维码即可自动配置</p>
      <p class="hint">手动配置请输入（复制上面黄色配置数据）:</p>
      <p class="hint" style="color:#f59e0b;font-size:14px;font-weight:600">{config_text}</p>
    </div></body></html>""")



@router.get("/index/index/appPush", include_in_schema=False)
@router.post("/index/index/appPush", include_in_schema=False)
async def app_push_legacy(request: Request):
    return await _app_push_impl(request)


@router.get("/index/index/appHeart", include_in_schema=False)
@router.post("/index/index/appHeart", include_in_schema=False)
async def app_heart(request: Request):
    """心跳 — APK 每 60 秒发一次，返回 success 即可"""
    return JSONResponse({"code": 1, "msg": "success"})


# ── 我们的路径 ──

@router.get("/vmq/appPush", summary="APK 收款推送 (GET)")
@router.post("/vmq/appPush", summary="APK 收款推送 (POST)")
async def app_push(request: Request):
    return await _app_push_impl(request)


async def _app_push_impl(request: Request):
    """接收 Android 监控 APK 推送的收款通知"""
    if request.method == "POST":
        try:
            data = await request.json()
        except Exception:
            data = {}
        type_ = data.get("type", request.query_params.get("type", ""))
        price = data.get("price", request.query_params.get("price", ""))
        t = data.get("t", request.query_params.get("t", ""))
        sign = data.get("sign", request.query_params.get("sign", ""))
    else:
        type_ = request.query_params.get("type", "")
        price = request.query_params.get("price", "")
        t = request.query_params.get("t", "")
        sign = request.query_params.get("sign", "")

    try:
        type_ = int(type_)
        price = float(price)
    except (ValueError, TypeError):
        return JSONResponse({"code": -1, "msg": "invalid params"})

    # 验签
    if not verify_app_push(type_, price, str(t), str(sign)):
        return JSONResponse({"code": -1, "msg": "sign error"})

    # 匹配订单
    result = process_app_push(type_, price)
    if result["matched"]:
        return JSONResponse({"code": 1, "msg": "success",
                             "data": {"payId": result["trade_order_id"]}})
    return JSONResponse({"code": 0, "msg": result.get("message", "no match")})


@router.get("/vmq/pay/{trade_order_id}", response_class=HTMLResponse,
            summary="支付页面")
def pay_page(trade_order_id: str):
    """用户扫码支付页面 — 显示收款码 + 金额"""
    order = get_order_status(trade_order_id)
    if not order:
        return HTMLResponse("<h3>订单不存在</h3>", status_code=404)

    if order["status"] == "paid":
        return HTMLResponse("""
        <html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
        <title>支付成功</title><style>body{font-family:-apple-system,sans-serif;display:flex;
        justify-content:center;align-items:center;height:100vh;margin:0;background:#0f1117;color:#fff}
        .box{text-align:center}.check{font-size:48px;color:#4ade80}.title{font-size:20px;margin:16px 0}
        .sub{color:#9ca0b0;font-size:14px}</style></head><body><div class="box">
        <div class="check">OK</div><div class="title">支付成功</div>
        <div class="sub">积分已到账，请返回页面</div></div></body></html>""")

    method = order["payment_method"]
    qr_path = get_qr_image(method)
    qr_img = f'<img src="/qr/{method}.jpg" style="max-width:240px;border-radius:12px">' if qr_path else '<div style="padding:40px;background:#222;border-radius:12px;color:#9ca0b0">请先上传收款码</div>'

    method_label = "微信" if method == "wechat" else "支付宝"
    return HTMLResponse(f"""
    <html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
    <title>扫码支付</title>
    <style>
      *{{box-sizing:border-box}} body{{font-family:-apple-system,sans-serif;display:flex;
      justify-content:center;align-items:center;min-height:100vh;margin:0;background:#0f1117;color:#fff}}
      .card{{text-align:center;padding:32px 24px;max-width:360px;width:100%}}
      .title{{font-size:20px;font-weight:700;margin-bottom:4px}}
      .sub{{font-size:13px;color:#9ca0b0;margin-bottom:20px}}
      .amount{{font-size:36px;font-weight:800;color:#f59e0b;margin-bottom:20px}}
      .qr{{margin-bottom:12px}} .qr img{{max-width:220px;border-radius:12px}}
      .hint{{font-size:13px;color:#9ca0b0;line-height:1.6}}
      .steps{{text-align:left;background:#1a1d27;border-radius:10px;padding:16px 20px;margin-top:16px}}
      .steps li{{font-size:13px;color:#a8b0c0;margin-bottom:8px;line-height:1.5}}
    </style></head><body><div class="card">
      <div class="title">使用{method_label}扫码支付</div>
      <div class="amount">¥{order["amount_yuan"]:.2f}</div>
      <div class="qr">{qr_img}</div>
      <div class="steps"><ol>
        <li>打开{method_label}扫一扫</li>
        <li>扫描上方收款码</li>
        <li>输入金额 <b>¥{order["amount_yuan"]:.2f}</b> 完成支付</li>
        <li>支付成功后积分自动到账</li>
      </ol></div>
    </div></body></html>""")


@router.post("/vmq/createOrder", summary="V免签创建订单 API")
async def vmq_create_order(request: Request):
    """供外部调用（兼容 V免签协议），也供我们的 payments.py 内部调用"""
    from vmq import create_vmq_order, _make_sign

    try:
        data = await request.json()
    except Exception:
        data = {}

    price = float(data.get("price", 0))
    pay_id = data.get("payId", "")
    payment_type = int(data.get("type", 1))
    sign = data.get("sign", "")
    param = data.get("param", "")
    notify_url = data.get("notifyUrl", "")
    return_url = data.get("returnUrl", "")

    expected = _make_sign([pay_id, param, payment_type, price])
    if sign != expected:
        return JSONResponse({"code": -1, "msg": "sign error"})

    method = "wechat" if payment_type == 1 else "alipay"
    # This is a generic V免签 order - map amount to nearest package
    from vmq import PACKAGES
    pkg = min(PACKAGES, key=lambda p: abs(p["amount"] - price))
    result = create_vmq_order("vmq-generic", method, pkg["amount"], pkg["points"])

    return JSONResponse({"code": 1, "msg": "success", "data": {
        "payId": pay_id,
        "orderId": result["trade_order_id"],
        "payurl": result["pay_url"],
    }})


@router.get("/vmq/status/{trade_order_id}", summary="查询 V免签订单状态")
def vmq_status(trade_order_id: str):
    order = get_order_status(trade_order_id)
    if not order:
        return JSONResponse({"code": -1, "msg": "not found"}, status_code=404)
    return {"code": 1, "trade_order_id": order["trade_order_id"],
            "status": order["status"],
            "amount_yuan": order["amount_yuan"],
            "points": order["points"]}
