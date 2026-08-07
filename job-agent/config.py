"""
统一配置 — .env + config.yaml
"""
import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml
from dotenv import load_dotenv

# 自动加载 .env
for _env_dir in (Path(__file__).parent, Path(__file__).parent.parent):
    _env_path = _env_dir / ".env"
    if _env_path.exists():
        load_dotenv(_env_path)


def _load_config() -> dict:
    config_path = Path(__file__).parent / "config.yaml"
    with open(config_path, encoding="utf-8") as f:
        raw = f.read()
    raw = re.sub(r'\$\{(\w+):-([^}]*)\}', lambda m: os.environ.get(m.group(1), m.group(2)), raw)
    raw = re.sub(r'\$\{(\w+)\}', lambda m: os.environ.get(m.group(1), ''), raw)
    return yaml.safe_load(raw)


_cfg = _load_config()


@dataclass
class Settings:
    database_url: str = _cfg.get("database", {}).get("url", "sqlite:///data/job_agent.db")
    server_url: str = _cfg.get("server_url", "http://localhost:8001")

    llm_api_key: str = _cfg.get("llm", {}).get("api_key", "")
    llm_base_url: str = _cfg.get("llm", {}).get("base_url", "https://api.deepseek.com/v1")
    llm_model: str = _cfg.get("llm", {}).get("model", "deepseek-chat")

    jwt_secret: str = _cfg.get("auth", {}).get("jwt_secret", "job-agent-secret-change-me")

    http_proxy: str = _cfg.get("search", {}).get("http_proxy", "")
    serper_api_key: str = _cfg.get("search", {}).get("serper_api_key", "")
    stt_provider: str = _cfg.get("stt", {}).get("provider", "web")  # "web" | "whisper_api" | "aliyun"
    stt_api_key: str = _cfg.get("stt", {}).get("api_key", "")
    stt_base_url: str = _cfg.get("stt", {}).get("base_url", "https://api.openai.com/v1")
    stt_model: str = _cfg.get("stt", {}).get("model", "whisper-1")
    stt_aliyun_ak_id: str = _cfg.get("stt", {}).get("aliyun_ak_id", "")
    stt_aliyun_ak_secret: str = _cfg.get("stt", {}).get("aliyun_ak_secret", "")
    stt_aliyun_appkey: str = _cfg.get("stt", {}).get("aliyun_appkey", "")


settings = Settings()
