"""
Feishu Adapter — 进程内 FeishuBot 管理
支持两种模式：
  1. 全局共享 bot（环境变量 FEISHU_APP_ID / FEISHU_APP_SECRET）
  2. Per-user device-code bot（扫码创建）
"""
from __future__ import annotations

import http.cookiejar
import json
import random
import string
import time
import urllib.parse
import urllib.request
import uuid

from sqlalchemy import text
from sqlalchemy.orm import Session

from adapters.base import BaseIMAdapter
from channels.feishu import FeishuBot
from config import settings
from database import engine


class FeishuAdapter(BaseIMAdapter):
    platform = "feishu"

    def __init__(self, agent, execute_tool):
        super().__init__(agent, execute_tool)
        self._global_bot: FeishuBot | None = None
        self._user_bots: dict[str, FeishuBot] = {}     # {user_id: FeishuBot}
        self._pairing_codes: dict[str, dict] = {}       # {code: {user_id, expires}}
        self._registrations: dict[str, dict] = {}       # {device_code: {user_id, expires, ...}}

    # ── 生命周期 ──

    def start(self):
        # 全局 bot（环境变量配置）
        if settings.feishu_app_id and settings.feishu_app_secret:
            def handle_pairing(msg_text: str, open_id: str) -> bool:
                return self._consume_pairing_code(msg_text, open_id)

            def resolve_user(open_id: str) -> str:
                with Session(engine) as session:
                    row = session.execute(
                        text("SELECT user_id FROM user_im_bindings WHERE platform='feishu' AND im_user_id=:imuid"),
                        {"imuid": open_id},
                    ).fetchone()
                return row.user_id if row else f"feishu:{open_id}"

            self._global_bot = FeishuBot(
                settings.feishu_app_id, settings.feishu_app_secret,
                self.agent, self.execute_tool,
                pairing_handler=handle_pairing,
                resolve_user=resolve_user,
            )
            self._global_bot.start()
            print("[FEISHU] global bot started")

        # 恢复 per-user bots（device-code 创建的）
        try:
            with Session(engine) as session:
                rows = session.execute(
                    text("SELECT user_id, app_id, app_secret, open_id FROM feishu_credentials")
                ).fetchall()
            for row in rows:
                try:
                    self._start_user_bot(row.user_id, row.app_id, row.app_secret, row.open_id)
                except Exception as e:
                    print(f"[FEISHU] failed to restore bot for {row.user_id}: {e}")
            if rows:
                print(f"[FEISHU] restored {len(rows)} per-user bot(s)")
        except Exception:
            pass

    def stop(self):
        if self._global_bot:
            try:
                self._global_bot.stop()
            except Exception:
                pass
        for bot in self._user_bots.values():
            try:
                bot.stop()
            except Exception:
                pass
        self._user_bots.clear()

    # ── 状态查询 ──

    def get_status(self, user_id: str) -> dict:
        return {"connected": True, "qr_available": True}

    def get_qr(self, user_id: str) -> dict:
        with Session(engine) as session:
            row = session.execute(
                text("SELECT im_user_id FROM user_im_bindings WHERE user_id=:uid AND platform='feishu'"),
                {"uid": user_id},
            ).fetchone()
        if row:
            return {"connected": True}

        # 检查进行中的注册
        now = time.time()
        for dc, reg in list(self._registrations.items()):
            if reg.get("user_id") == user_id and reg.get("expires", 0) > now and reg.get("status") == "pending":
                poll_result = self._poll_registration(dc)
                if poll_result.get("status") == "success":
                    return {"connected": True}
                return {
                    "connected": False,
                    "qr_url": reg["qr_url"],
                    "device_code": dc,
                    "interval": reg["interval"],
                }

        # 启动新注册
        result = self._init_registration(user_id)
        if "error" in result:
            return {"error": result["error"]}
        return {
            "connected": False,
            "qr_url": result["qr_url"],
            "device_code": result["device_code"],
            "interval": result["interval"],
        }

    def poll_registration(self, user_id: str, device_code: str) -> dict:
        reg = self._registrations.get(device_code)
        if not reg or reg.get("user_id") != user_id:
            return {"status": "not_found"}
        return self._poll_registration(device_code)

    # ── 配对码 ──

    def generate_pairing_code(self, user_id: str) -> str:
        now = time.time()
        expired = [c for c, v in self._pairing_codes.items() if v["expires"] < now]
        for c in expired:
            del self._pairing_codes[c]
        code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
        self._pairing_codes[code] = {"user_id": user_id, "expires": now + 300}
        return code

    def _consume_pairing_code(self, code: str, open_id: str) -> bool:
        now = time.time()
        entry = self._pairing_codes.get(code)
        if not entry or entry["expires"] < now:
            if entry:
                del self._pairing_codes[code]
            return False
        user_id = entry["user_id"]
        del self._pairing_codes[code]
        # 写入绑定
        with Session(engine) as session:
            session.execute(
                text("INSERT OR IGNORE INTO user_im_bindings (id, user_id, platform, im_user_id, created_at) "
                     "VALUES (:id, :uid, 'feishu', :imuid, :now)"),
                {"id": str(uuid.uuid4())[:12], "uid": user_id, "imuid": open_id,
                 "now": self._now_iso()},
            )
            session.commit()
        print(f"[FEISHU] bound open_id={open_id} to user_id={user_id}")
        return True

    # ── 通知推送 ──

    def send_notification(self, im_user_id: str, task: str, run_at: str, web_user_id: str = "") -> bool:
        bot = self._user_bots.get(web_user_id)
        if bot:
            bot.send_notification(im_user_id, task, run_at)
            return True
        if self._global_bot:
            self._global_bot.send_notification(im_user_id, task, run_at)
            return True
        print(f"[FEISHU] no bot available for push to {web_user_id}")
        return False

    # ── Device-code 注册 ──

    _REG_BASE = "https://accounts.feishu.cn/oauth/v1/app/registration"

    def _init_registration(self, user_id: str) -> dict:
        cj = http.cookiejar.CookieJar()

        # Step 1: init
        init_res = self._reg_fetch(f"{self._REG_BASE}", {"action": "init"}, cj)

        # Step 2: begin
        result = self._reg_fetch(f"{self._REG_BASE}", {
            "action": "begin",
            "archetype": "PersonalAgent",
            "auth_method": "client_secret",
            "request_user_info": "open_id",
        }, cj)

        device_code = result.get("device_code", "")
        qr_url = result.get("verification_uri_complete", "")
        interval = result.get("interval", 5)
        expires_in = result.get("expires_in", 600)

        if not device_code:
            return {"error": f"feishu registration failed: {result}"}

        if qr_url:
            parsed = list(urllib.parse.urlparse(qr_url))
            query = urllib.parse.parse_qs(parsed[4])
            query["from"] = ["oc_onboard"]
            query["tp"] = ["ob_cli_app"]
            parsed[4] = urllib.parse.urlencode(query, doseq=True)
            qr_url = urllib.parse.urlunparse(parsed)

        self._registrations[device_code] = {
            "user_id": user_id,
            "status": "pending",
            "expires": time.time() + expires_in,
            "qr_url": qr_url,
            "interval": interval,
            "cj": cj,
        }
        return {"qr_url": qr_url, "device_code": device_code, "interval": interval}

    def _poll_registration(self, device_code: str) -> dict:
        reg = self._registrations.get(device_code)
        if not reg:
            return {"status": "not_found"}
        if time.time() > reg["expires"]:
            del self._registrations[device_code]
            return {"status": "timeout"}

        result = self._reg_fetch(f"{self._REG_BASE}", {
            "action": "poll", "device_code": device_code,
        }, reg["cj"])

        if result.get("error") in ("authorization_pending", "slow_down"):
            return {"status": "pending"}

        app_id = result.get("client_id") or result.get("app_id") or ""
        app_secret = result.get("client_secret") or result.get("app_secret") or ""
        open_id = (result.get("user_info") or {}).get("open_id") or result.get("open_id") or ""

        if app_id and app_secret:
            self._configure_ws(app_id, app_secret)

            with Session(engine) as session:
                session.execute(
                    text("INSERT OR REPLACE INTO feishu_credentials (user_id, app_id, app_secret, open_id, created_at) "
                         "VALUES (:uid, :aid, :asec, :oid, :now)"),
                    {"uid": reg["user_id"], "aid": app_id, "asec": app_secret,
                     "oid": open_id, "now": self._now_iso()},
                )
                session.execute(
                    text("INSERT OR IGNORE INTO user_im_bindings (id, user_id, platform, im_user_id, created_at) "
                         "VALUES (:id, :uid, 'feishu', :imuid, :now)"),
                    {"id": str(uuid.uuid4())[:12], "uid": reg["user_id"],
                     "imuid": open_id, "now": self._now_iso()},
                )
                session.commit()
            reg["status"] = "success"
            self._registrations[device_code] = reg
            self._start_user_bot(reg["user_id"], app_id, app_secret, open_id)
            return {"status": "success", "app_id": app_id, "open_id": open_id}

        return {"status": "pending"}

    def _configure_ws(self, app_id: str, app_secret: str) -> bool:
        """配置 per-user 飞书应用的 WebSocket 事件订阅"""
        try:
            token_req = urllib.request.Request(
                "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                data=json.dumps({"app_id": app_id, "app_secret": app_secret}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            resp = urllib.request.urlopen(token_req, timeout=10)
            token_data = json.loads(resp.read().decode())
            token = token_data.get("tenant_access_token", "")
            if not token:
                return False

            patch_body = json.dumps({"event": {"subscription_type": ""}}).encode()
            patch_req = urllib.request.Request(
                f"https://open.feishu.cn/open-apis/application/v6/applications/{app_id}",
                data=patch_body,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"},
                method="PATCH",
            )
            resp = urllib.request.urlopen(patch_req, timeout=10)
            patch_result = json.loads(resp.read().decode())
            return patch_result.get("code") == 0
        except Exception as e:
            print(f"[FEISHU-CONFIG] error: {e}")
            return False

    def _start_user_bot(self, user_id: str, app_id: str, app_secret: str, open_id: str):
        old = self._user_bots.pop(user_id, None)
        if old:
            try:
                old.stop()
            except Exception:
                pass

        def resolve_user(oid: str) -> str:
            return user_id

        def on_open_id(oid: str):
            if not oid:
                return
            with Session(engine) as session:
                row = session.execute(
                    text("SELECT im_user_id FROM user_im_bindings WHERE user_id=:uid AND platform='feishu'"),
                    {"uid": user_id},
                ).fetchone()
                if not row or not row.im_user_id:
                    session.execute(
                        text("INSERT OR REPLACE INTO user_im_bindings (id, user_id, platform, im_user_id, created_at) "
                             "VALUES (:id, :uid, 'feishu', :imuid, :now)"),
                        {"id": str(uuid.uuid4())[:12], "uid": user_id, "imuid": oid,
                         "now": self._now_iso()},
                    )
                    session.execute(
                        text("UPDATE feishu_credentials SET open_id=:oid WHERE user_id=:uid"),
                        {"oid": oid, "uid": user_id},
                    )
                    session.commit()

        bot = FeishuBot(
            app_id, app_secret, self.agent, self.execute_tool,
            pairing_handler=None,
            resolve_user=resolve_user,
            on_open_id=on_open_id,
        )
        bot.start()
        self._user_bots[user_id] = bot
        print(f"[FEISHU] started user bot for {user_id}")

    # ── 工具方法 ──

    @staticmethod
    def _reg_fetch(url: str, data: dict | None = None, cookie_jar=None) -> dict:
        headers = {"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"}
        body = urllib.parse.urlencode(data).encode("utf-8") if data else None
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        if cookie_jar:
            cookie_jar.add_cookie_header(req)
        try:
            resp = urllib.request.urlopen(req, timeout=15)
            result = json.loads(resp.read().decode("utf-8"))
            if cookie_jar:
                cookie_jar.extract_cookies(resp, req)
            return result
        except urllib.error.HTTPError as e:
            body_bytes = e.read().decode("utf-8", errors="replace")
            try:
                return json.loads(body_bytes)
            except json.JSONDecodeError:
                return {"error": body_bytes, "http_status": e.code}
        except Exception as e:
            return {"error": str(e)}

    @staticmethod
    def _now_iso() -> str:
        from datetime import datetime
        try:
            from zoneinfo import ZoneInfo
        except ImportError:
            from backports.zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(settings.tz_name)).isoformat()
