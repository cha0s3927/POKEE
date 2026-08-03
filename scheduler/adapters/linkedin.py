"""
LinkedIn Adapter — 进程内 LinkedInBot 管理（Voyager API 轮询）
"""
from adapters.base import BaseIMAdapter
from channels.linkedin import LinkedInBot
from config import settings


class LinkedInAdapter(BaseIMAdapter):
    platform = "linkedin"

    def __init__(self, agent, execute_tool):
        super().__init__(agent, execute_tool)
        self._bot: LinkedInBot | None = None

    def start(self):
        if settings.linkedin_email and settings.linkedin_password:
            cookies = None
            if settings.linkedin_li_at:
                cookies = {"li_at": settings.linkedin_li_at, "JSESSIONID": settings.linkedin_jsessionid}
            self._bot = LinkedInBot(
                settings.linkedin_email, settings.linkedin_password,
                self.agent, self.execute_tool,
                cookies=cookies,
            )
            self._bot.start()
            print("[LINKEDIN] bot started")

    def stop(self):
        if self._bot:
            self._bot.stop()

    def get_status(self, user_id: str) -> dict:
        if not settings.linkedin_email:
            return {"connected": False, "qr_available": False}
        return {"connected": self._bot is not None and self._bot._running, "qr_available": False}

    def get_qr(self, user_id: str) -> dict:
        return {"connected": False, "qr_available": False, "note": "LinkedIn uses cookie auth, no QR"}

    def send_notification(self, im_user_id: str, task: str, run_at: str, web_user_id: str = "") -> bool:
        if self._bot:
            self._bot.send_notification(im_user_id, task, run_at)
            return True
        return False

    @property
    def api(self):
        """暴露底层 LinkedIn API client，供 chat endpoint 使用"""
        return self._bot.api if self._bot else None
