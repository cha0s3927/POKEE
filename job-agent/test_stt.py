"""测试 STT 链路 — 直接调用阿里云 ASR，验证 token + 识别是否正常."""
import httpx
import json
import os
import sys

# 屏蔽系统代理（阿里云国内直连）
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)

# --- 复制 stt.py 的 token 获取逻辑（独立版本，绕代理）---
def get_token():
    from aliyunsdkcore.client import AcsClient
    from aliyunsdkcore.request import CommonRequest

    ak_id = os.environ.get("ALIYUN_AK_ID", "")
    ak_secret = os.environ.get("ALIYUN_AK_SECRET", "")
    if not ak_id or not ak_secret:
        from dotenv import load_dotenv
        load_dotenv()
        ak_id = os.environ.get("ALIYUN_AK_ID", "")
        ak_secret = os.environ.get("ALIYUN_AK_SECRET", "")

    client = AcsClient(ak_id, ak_secret, "cn-shanghai")
    req = CommonRequest()
    req.set_method("POST")
    req.set_domain("nls-meta.cn-shanghai.aliyuncs.com")
    req.set_version("2019-02-28")
    req.set_action_name("CreateToken")
    resp = client.do_action_with_exception(req)
    data = json.loads(resp)
    return data.get("Token", {}).get("Id", "")


def asr(token, appkey, audio_bytes, fmt="wav", rate=16000):
    """直接调阿里云一句话识别."""
    params = {
        "appkey": appkey,
        "format": fmt,
        "sample_rate": str(rate),
        "enable_punctuation_prediction": "true",
        "enable_inverse_text_normalization": "true",
    }
    client = httpx.Client(timeout=httpx.Timeout(15.0, connect=5.0), trust_env=False)
    try:
        resp = client.post(
            "https://nls-gateway.cn-shanghai.aliyuncs.com/stream/v1/asr",
            params=params,
            headers={"X-NLS-Token": token, "Content-Type": "application/octet-stream"},
            content=audio_bytes,
        )
        return resp.json()
    finally:
        client.close()


if __name__ == "__main__":
    # 1. Get token
    print("Getting token...")
    token = get_token()
    print(f"Token OK ({len(token)} chars)")

    appkey = os.environ.get("ALIYUN_APPKEY", "")
    if not appkey:
        from dotenv import load_dotenv
        load_dotenv()
        appkey = os.environ.get("ALIYUN_APPKEY", "")

    if len(sys.argv) > 1:
        # 传入 wav 文件
        with open(sys.argv[1], "rb") as f:
            audio = f.read()
        print(f"Audio loaded: {len(audio)} bytes from {sys.argv[1]}")
        result = asr(token, appkey, audio, "wav", 16000)
        print(f"ASR result: {json.dumps(result, ensure_ascii=False)}")
    else:
        # 生成一段 16kHz 16bit mono WAV 静音（1秒）→ 应该返回空
        import struct
        sample_rate = 16000
        duration_sec = 1
        n_samples = sample_rate * duration_sec
        samples = b'\x00\x00' * n_samples  # 全静音
        wav_header = struct.pack(
            '<4sI4s4sIHHIIHH4sI',
            b'RIFF', 36 + len(samples),
            b'WAVE',
            b'fmt ', 16, 1, 1, sample_rate,
            sample_rate * 2, 2, 16,
            b'data', len(samples)
        )
        wav_data = wav_header + samples
        print(f"Sending silent WAV: {len(wav_data)} bytes")
        result = asr(token, appkey, wav_data, "wav", 16000)
        print(f"Silent WAV result: {json.dumps(result, ensure_ascii=False)}")
        print("(empty text is expected for silence — this validates the API pipeline)")
