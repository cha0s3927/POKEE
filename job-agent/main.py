"""
求职辅助 Agent — FastAPI 入口
"""
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response

from agent import Agent
from database import init_db

# 全局 Agent 实例
agent = Agent()

app = FastAPI(title="求职辅助 Agent", version="0.1.0")

# CORS — 允许同一局域网访问
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def startup():
    init_db()
    print("[STARTUP] Database initialized")

    from growth_engine import start_growth_engine
    start_growth_engine()
    print("[STARTUP] Growth engine started")


@app.on_event("shutdown")
def shutdown():
    from growth_engine import stop_growth_engine
    stop_growth_engine()


# ── 路由 ──
from routes.auth import router as auth_router
from routes.resume import router as resume_router
from routes.score import router as score_router
from routes.chat import router as chat_router
from routes.platforms import router as platforms_router
from routes.interview import router as interview_router
from routes.stt import router as stt_router

app.include_router(auth_router)
app.include_router(resume_router)
app.include_router(score_router)
app.include_router(chat_router)
app.include_router(platforms_router)
app.include_router(interview_router)
app.include_router(stt_router)


# ── 健康检查 ──
@app.get("/health")
def health():
    return {"status": "ok", "service": "job-agent"}


# ── 静态文件（放在最后，优先级低于 API） ──
class NoCacheStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope) -> Response:
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        for h in ("etag", "last-modified"):
            if h in response.headers:
                del response.headers[h]
        return response


static_dir = Path(__file__).parent / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/", NoCacheStaticFiles(directory=str(static_dir), html=True), name="static")
