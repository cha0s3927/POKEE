"""
统一配置 — .env + config.yaml 合并为单一 Settings 对象
"""
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

# 自动加载 .env（优先当前目录，再找上级目录）
for _env_dir in (Path(__file__).parent, Path(__file__).parent.parent):
    _env_path = _env_dir / ".env"
    if _env_path.exists():
        load_dotenv(_env_path)


def _load_config() -> dict:
    """读取 config.yaml，替换 ${VAR:-default} 和 ${VAR} 占位符"""
    config_path = Path(__file__).parent / "config.yaml"
    with open(config_path, encoding="utf-8") as f:
        raw = f.read()
    raw = re.sub(r'\$\{(\w+):-([^}]*)\}', lambda m: os.environ.get(m.group(1), m.group(2)), raw)
    raw = re.sub(r'\$\{(\w+)\}', lambda m: os.environ.get(m.group(1), ''), raw)
    return yaml.safe_load(raw)


_cfg = _load_config()


@dataclass
class Settings:
    # 数据库
    database_url: str = _cfg.get("database", {}).get("url", "sqlite:///data/reminders.db")
    server_url: str = _cfg.get("server_url", "http://localhost:8000")

    # LLM
    llm_api_key: str = _cfg.get("llm", {}).get("api_key", "")
    llm_base_url: str = _cfg.get("llm", {}).get("base_url", "https://api.deepseek.com/v1")
    llm_model: str = _cfg.get("llm", {}).get("model", "deepseek-chat")

    # WeChat adapter
    wechat_secret: str = _cfg.get("wechat", {}).get("secret", os.environ.get("WECHAT_SECRET", "wechat-secret-change-me"))
    wechat_push_url: str = _cfg.get("wechat", {}).get("push_url", os.environ.get("WECHAT_PUSH_URL", "http://localhost:8765/push"))

    # Feishu
    feishu_app_id: str = _cfg.get("feishu", {}).get("app_id", os.environ.get("FEISHU_APP_ID", ""))
    feishu_app_secret: str = _cfg.get("feishu", {}).get("app_secret", os.environ.get("FEISHU_APP_SECRET", ""))

    # WhatsApp adapter
    whatsapp_secret: str = _cfg.get("whatsapp", {}).get("secret", os.environ.get("WHATSAPP_SECRET", "whatsapp-secret-change-me"))
    whatsapp_push_url: str = _cfg.get("whatsapp", {}).get("push_url", os.environ.get("WHATSAPP_PUSH_URL", "http://localhost:8767/push"))

    # LinkedIn
    linkedin_email: str = _cfg.get("linkedin", {}).get("email", os.environ.get("LINKEDIN_EMAIL", ""))
    linkedin_password: str = _cfg.get("linkedin", {}).get("password", os.environ.get("LINKEDIN_PASSWORD", ""))
    linkedin_secret: str = _cfg.get("linkedin", {}).get("secret", os.environ.get("LINKEDIN_SECRET", "reminder-agent-linkedin-2026"))
    linkedin_li_at: str = _cfg.get("linkedin", {}).get("li_at", os.environ.get("LINKEDIN_LI_AT", ""))
    linkedin_jsessionid: str = _cfg.get("linkedin", {}).get("jsessionid", os.environ.get("LINKEDIN_JSESSIONID", ""))

    # V免签
    vmq_key: str = _cfg.get("vmq", {}).get("key", os.environ.get("VMQ_KEY", "vmq-key-change-me"))
    vmq_host_port: str = _cfg.get("vmq", {}).get("host_port", os.environ.get("VMQ_HOST_PORT", "8.161.228.6:8000"))

    # 辅助
    tz_name: str = "Asia/Shanghai"


settings = Settings()
