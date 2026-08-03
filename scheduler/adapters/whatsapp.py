"""
WhatsApp Adapter — HTTP 桥接到 Node.js whatsapp-adapter 进程
"""
import json
import urllib.parse
import urllib.request

from adapters.base import BaseIMAdapter
from config import settings


class WhatsAppAdapter(BaseIMAdapter):
    platform = "whatsapp"

    def __init__(self, agent, execute_tool):
        super().__init__(agent, execute_tool)

    def start(self):
        pass  # Node.js 进程由 supervisor 管理

    def stop(self):
        pass

    def get_status(self, user_id: str) -> dict:
        try:
            url = f"http://127.0.0.1:8767/status?user_id={urllib.parse.quote(user_id)}"
            req = urllib.request.Request(url)
            resp = urllib.request.urlopen(req, timeout=2)
            data = json.loads(resp.read())
            return {"connected": data.get("connected", False), "qr_available": True}
        except Exception:
            return {"connected": False, "qr_available": False, "error": "adapter not reachable"}

    def get_qr(self, user_id: str) -> dict:
        try:
            url = f"http://127.0.0.1:8767/qr?user_id={urllib.parse.quote(user_id)}"
            req = urllib.request.Request(url)
            resp = urllib.request.urlopen(req, timeout=3)
            data = json.loads(resp.read())
            qr_text = data.get("qr", "")
            pairing_code = data.get("pairing_code", "")
            result = {"connected": data.get("connected", False), "qr": qr_text}
            if pairing_code:
                result["pairing_code"] = pairing_code
            elif qr_text:
                result["qr_image"] = self._qr_image(qr_text)
            return result
        except Exception:
            return {"connected": False, "error": "adapter not reachable"}

    def request_pairing_code(self, phone: str, user_id: str) -> dict:
        """请求 WhatsApp 配对码"""
        try:
            payload = json.dumps({"phone": phone, "user_id": user_id}).encode()
            req = urllib.request.Request(
                "http://127.0.0.1:8767/pairing",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            resp = urllib.request.urlopen(req, timeout=10)
            return json.loads(resp.read())
        except Exception as e:
            return {"error": str(e)}

    def send_notification(self, im_user_id: str, task: str, run_at: str, web_user_id: str = "") -> bool:
        payload = {"task": task, "run_at": run_at, "user_id": im_user_id, "web_user_id": web_user_id}
        try:
            req = urllib.request.Request(
                settings.whatsapp_push_url,
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=5)
            return True
        except Exception as e:
            print(f"[WHATSAPP] push failed: {e}")
            return False

    @staticmethod
    def _qr_image(text: str) -> str:
        import base64, io
        import qrcode
        img = qrcode.make(text, border=2)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
