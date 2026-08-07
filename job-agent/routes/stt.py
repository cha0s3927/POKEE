"""
语音转文字路由 — 支持 Whisper API / 阿里云 NLS
"""
import logging
import os
import subprocess
import tempfile

import httpx
from fastapi import APIRouter, Depends, UploadFile, File

from routes.auth import auth_user
from config import settings

logger = logging.getLogger(__name__)
router = APIRouter(tags=["stt"])

# 阿里云国内直连，不走系统代理。SDK 和 httpx 均可能读取 HTTP_PROXY 环境变量，
# 模块加载时清除，进程内后续调用均不受代理干扰。
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)

# 复用 httpx 连接池，trust_env=False 避免读 Windows 注册表代理
_httpx_client = httpx.Client(timeout=httpx.Timeout(15.0, connect=5.0), trust_env=False)


@router.get("/api/stt/config", summary="STT 配置状态")
def stt_config():
    available = False
    if settings.stt_provider == "aliyun":
        available = bool(settings.stt_aliyun_ak_id and settings.stt_aliyun_ak_secret)
    elif settings.stt_provider != "web":
        available = bool(settings.stt_api_key)
    return {"provider": settings.stt_provider, "available": available}


# ── Aliyun NLS helpers ──

def _aliyun_get_token() -> str:
    """使用官方 SDK CommonRequest 获取阿里云 NLS Token（有效期 24h）."""
    import json
    from aliyunsdkcore.client import AcsClient
    from aliyunsdkcore.request import CommonRequest

    client = AcsClient(
        settings.stt_aliyun_ak_id,
        settings.stt_aliyun_ak_secret,
        "cn-shanghai",
    )
    req = CommonRequest()
    req.set_method("POST")
    req.set_domain("nls-meta.cn-shanghai.aliyuncs.com")
    req.set_version("2019-02-28")
    req.set_action_name("CreateToken")
    resp = client.do_action_with_exception(req)
    data = json.loads(resp)
    token = data.get("Token", {}).get("Id", "")
    if not token:
        raise RuntimeError(f"获取阿里云 NLS Token 失败: {data}")
    return token


def _aliyun_asr(audio_bytes: bytes, audio_format: str, sample_rate: int) -> str:
    """调用阿里云 NLS 一句话识别 REST API."""
    token = _aliyun_get_token()
    params = {
        "appkey": settings.stt_aliyun_appkey,
        "format": audio_format,
        "sample_rate": str(sample_rate),
        "enable_punctuation_prediction": "true",
        "enable_inverse_text_normalization": "true",
    }

    resp = _httpx_client.post(
        "https://nls-gateway.cn-shanghai.aliyuncs.com/stream/v1/asr",
        params=params,
        headers={
            "X-NLS-Token": token,
            "Content-Type": "application/octet-stream",
        },
        content=audio_bytes,
    )
    data = resp.json()
    if data.get("status") == 20000000:
        return data.get("result", "")
    raise RuntimeError(f"阿里云 ASR 失败: status={data.get('status')} msg={data.get('status_text', data)}")


def _convert_to_wav(input_bytes: bytes, input_suffix: str) -> bytes:
    """用 ffmpeg 将音频转为 16kHz mono WAV."""
    with tempfile.NamedTemporaryFile(suffix=input_suffix, delete=False) as infile:
        infile.write(input_bytes)
        infile_path = infile.name
    outfile_path = infile_path + ".wav"
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", infile_path, "-ar", "16000", "-ac", "1", "-f", "wav", outfile_path],
            capture_output=True, check=True, timeout=10,
        )
        with open(outfile_path, "rb") as f:
            return f.read()
    finally:
        for p in (infile_path, outfile_path):
            try:
                os.unlink(p)
            except OSError:
                pass


# ── Route ──

@router.post("/api/stt", summary="语音转文字")
async def transcribe(audio: UploadFile = File(...), user: dict = Depends(auth_user)):
    """接收音频文件，调用对应 STT 服务转文字."""
    suffix = os.path.splitext(audio.filename or "audio.webm")[1] or ".webm"
    content = await audio.read()
    logger.info(f"STT: file={audio.filename} suffix={suffix} size={len(content)}")

    # 临时调试：保存上传的音频供分析
    _dbg_path = os.path.join(tempfile.gettempdir(), f"stt_debug{suffix}")
    with open(_dbg_path, "wb") as _f:
        _f.write(content)
    logger.info(f"STT debug: saved to {_dbg_path}")

    # ── Aliyun NLS ──
    if settings.stt_provider == "aliyun":
        if not settings.stt_aliyun_ak_id or not settings.stt_aliyun_ak_secret:
            return {"error": "no_key", "message": "未配置阿里云 AccessKey"}
        if not settings.stt_aliyun_appkey:
            return {"error": "no_appkey", "message": "未配置阿里云 NLS AppKey，请在 config.yaml 的 stt.aliyun_appkey 中设置"}

        try:
            if suffix == ".wav":
                # 前端已转好 16kHz PCM WAV，直接发送
                text = _aliyun_asr(content, "wav", 16000)
            else:
                # 旧版前端发的 webm/opus → 先试直接发，失败则 ffmpeg 转换
                try:
                    text = _aliyun_asr(content, "opus", 16000)
                except Exception:
                    wav_bytes = _convert_to_wav(content, suffix)
                    text = _aliyun_asr(wav_bytes, "wav", 16000)
            return {"text": text}
        except Exception as e:
            logger.exception("Aliyun STT failed")
            return {"error": "transcribe_failed", "message": f"语音识别失败: {e}"}

    # ── Whisper API ──
    if settings.stt_provider == "web":
        return {"error": "web", "message": "服务端 STT 未配置，请在浏览器中使用语音输入"}

    if not settings.stt_api_key:
        return {"error": "no_key", "message": "未配置 STT API Key"}

    suffix = os.path.splitext(audio.filename or "audio.webm")[1] or ".webm"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(content)
        tmp_path = tmp.name

    try:
        from openai import OpenAI
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
