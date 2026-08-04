"""
SSE route — /api/sse 实时通知推送
"""
import asyncio
import json

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import text
from starlette.responses import Response

from database import engine
from scheduler import sse_clients

router = APIRouter(tags=["sse"])


@router.get("/api/sse", summary="实时通知推送")
async def sse_stream(request: Request, token: str = ""):
    if token:
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT * FROM users WHERE token = :t"), {"t": token}
            ).fetchone()
        if not row:
            return Response(status_code=401)
        user = dict(row._mapping)
    else:
        return Response(status_code=401)

    uid = user["id"]
    queue: asyncio.Queue = asyncio.Queue(maxsize=50)
    old = sse_clients.pop(uid, None)
    sse_clients[uid] = queue
    print(f"[SSE] user={uid} connected, total={len(sse_clients)}")

    async def event_generator():
        try:
            yield ": ok\n\n"
            while True:
                try:
                    data = await asyncio.wait_for(queue.get(), timeout=15)
                    try:
                        print(f"[SSE] -> {uid}: {data.get('task', '')}")
                    except Exception:
                        pass
                    yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        except Exception:
            pass
        finally:
            if sse_clients.get(uid) is queue:
                sse_clients.pop(uid, None)
            print(f"[SSE] user={uid} disconnected, total={len(sse_clients)}")

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
