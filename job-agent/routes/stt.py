"""
语音转文字路由 — Whisper API 兼容
"""
import logging
import tempfile
import os

from fastapi import APIRouter, Depends, UploadFile, File
from openai import OpenAI

from routes.auth import auth_user
from config import settings

logger = logging.getLogger(__name__)
router = APIRouter(tags=["stt"])


@router.get("/api/stt/config", summary="STT 配置状态")
def stt_config():
    return {"provider": settings.stt_provider, "available": settings.stt_provider != "web" and bool(settings.stt_api_key)}


@router.post("/api/stt", summary="语音转文字")
async def transcribe(audio: UploadFile = File(...), user: dict = Depends(auth_user)):
    """接收音频文件，调用 Whisper API 转文字."""
    if settings.stt_provider == "web":
        return {"error": "web", "message": "服务端 STT 未配置，请在浏览器中使用语音输入"}

    if not settings.stt_api_key:
        return {"error": "no_key", "message": "未配置 STT API Key"}

    # Save uploaded audio to temp file
    suffix = os.path.splitext(audio.filename or "audio.webm")[1] or ".webm"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        content = await audio.read()
        tmp.write(content)
        tmp_path = tmp.name

    try:
        client = OpenAI(
            api_key=settings.stt_api_key,
            base_url=settings.stt_base_url,
        )
        with open(tmp_path, "rb") as f:
            result = client.audio.transcriptions.create(
                model=settings.stt_model,
                file=f,
                response_format="json",
            )
        return {"text": result.text}
    except Exception as e:
        logger.exception("STT transcription failed")
        return {"error": "transcribe_failed", "message": f"语音识别失败: {e}"}
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
