"""
Reminder Agent — 定时提醒助手
FastAPI + APScheduler + LLM Agent + 多 IM 平台
"""
import asyncio
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response

from agent import Agent
from config import settings
from database import init_db
from tools import execute_tool

# ── 全局 Agent ──
agent = Agent(
    api_key=settings.llm_api_key,
    base_url=settings.llm_base_url,
    model=settings.llm_model,
    tool_executor=execute_tool,
)

# ── FastAPI app ──
app = FastAPI(
    title="POKEE",
    version="4.0.0",
    description="Your Personal Reminder",
)


@app.on_event("startup")
def startup():
    from scheduler import scheduler, init_sse, restore_jobs
    from scheduler import push_sse as _push_sse_ref

    # 数据库
    init_db()

    # SSE
    loop = asyncio.get_running_loop()
    init_sse(loop)

    # 调度器
    scheduler.start()
    n = restore_jobs()
    print(f"[STARTUP] {n} pending reminders restored")

    # 注册并启动所有 IM 适配器
    from adapters import register as reg_adapter
    from adapters.wechat import WeChatAdapter
    from adapters.whatsapp import WhatsAppAdapter
    from adapters.feishu import FeishuAdapter
    from adapters.linkedin import LinkedInAdapter

    adapters = [
        WeChatAdapter(agent, execute_tool),
        WhatsAppAdapter(agent, execute_tool),
        FeishuAdapter(agent, execute_tool),
        LinkedInAdapter(agent, execute_tool),
    ]
    for a in adapters:
        reg_adapter(a)
        a.start()

    print(f"[STARTUP] {len(adapters)} adapters registered")


@app.on_event("shutdown")
def shutdown():
    from scheduler import scheduler
    from adapters import adapter_registry

    scheduler.shutdown()
    for a in adapter_registry.values():
        try:
            a.stop()
        except Exception:
            pass


# ── 挂载路由 ──
from routes.auth import router as auth_router
from routes.chat import router as chat_router
from routes.reminders import router as reminders_router
from routes.platforms import router as platforms_router
from routes.sse import router as sse_router
from routes.payments import router as payments_router

app.include_router(auth_router)
app.include_router(chat_router)
app.include_router(reminders_router)
app.include_router(platforms_router)
app.include_router(sse_router)
app.include_router(payments_router)


# ── 健康检查 ──
@app.get("/health", summary="健康检查")
def health():
    from scheduler import scheduler
    return {"status": "ok", "scheduler_running": scheduler.running}


# ── 静态文件 ──
class NoCacheStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope) -> Response:
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        for _h in ("etag", "last-modified"):
            if _h in response.headers:
                del response.headers[_h]
        return response


static_dir = Path(__file__).parent / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/", NoCacheStaticFiles(directory=str(static_dir), html=True), name="static")
