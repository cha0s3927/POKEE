"""
Growth Engine — 主动成长计划督促
后台线程定期检查 in_progress 任务，LLM 生成变体督促消息
"""
import logging
import random
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import text

from database import engine

logger = logging.getLogger(__name__)

LLM_CONCURRENCY = 4       # 单次 tick 最多同时调几个 LLM
LLM_TIMEOUT = 25          # 单个 LLM 调用超时（秒）

TONE_ROTATION = ["curious", "supportive", "challenging", "humorous", "structured"]
TONE_LABELS = {
    "curious": "好奇型——像师兄路过随口一问，轻松自然",
    "supportive": "支持型——表达关心，主动提出帮师弟拆解难题",
    "challenging": "挑战型——激将法，用猴哥的方式刺激一下",
    "humorous": "幽默型——用西游记梗调侃，让师弟笑着继续",
    "structured": "结构化型——帮师弟盘点进度，列出还剩几步",
}

CHECK_INTERVAL = 120  # 后台轮询间隔（秒）

_timer: Optional[threading.Timer] = None
_process_lock = threading.Lock()


def pick_tone(last_tone: str) -> str:
    available = [t for t in TONE_ROTATION if t != last_tone]
    return random.choice(available)


def _silence_context(silence_days: int) -> str:
    if silence_days <= 0:
        return "师弟刚开始这项任务，像朋友一样随口问问进展。"
    elif silence_days <= 2:
        return "师弟 1-2 天没动静了，带点调侃的关心，问是不是被妖怪抓走了。"
    elif silence_days <= 5:
        return "师弟 3-5 天没回应了，用激将法稍微刺激一下，但别真的生气。"
    else:
        return "师弟 6-7 天没动静了，这是最后一次。用猴哥的口气表达失望但不放弃，说俺先回花果山了，师弟想练了随时叫俺。再不回复就暂时不打扰了。"


def _next_checkin_hours(silence_days: int) -> int:
    if silence_days <= 0:
        return random.randint(20, 28)
    elif silence_days <= 2:
        return random.randint(20, 28)
    elif silence_days <= 5:
        return random.randint(20, 28)
    elif silence_days <= 7:
        return random.randint(44, 52)
    return 0


def generate_checkin_message(task: dict, user_id: str) -> str:
    from openai import OpenAI
    import httpx

    client = OpenAI(
        api_key=__import__("config").settings.llm_api_key,
        base_url=__import__("config").settings.llm_base_url,
        http_client=httpx.Client(transport=httpx.HTTPTransport(retries=0), timeout=httpx.Timeout(30.0, connect=5.0)),
    )

    silence_days = task.get("silence_days", 0) or 0
    tone = pick_tone(task.get("last_tone", ""))
    tone_desc = TONE_LABELS.get(tone, "")

    prompt = f"""你是孙悟空，花果山求职道场的掌门。你在跟进师弟的学习/求职任务进展。

任务标题：{task['title']}
任务类别：{task.get('category', 'skill')}
已经来问过 {task.get('checkin_count', 0)} 次了。
师弟静默天数：{silence_days} 天

语气要求：{tone_desc}
背景：{_silence_context(silence_days)}

请以猴哥的口吻生成一段 60-120 字的消息。规则：
1. 自称"俺老孙"或"猴哥"，称用户"师弟/师妹"
2. 不提"督促""提醒"这些词，像师兄串门聊天一样自然
3. 根据静默阶段调整紧迫感——越久越毒舌，但心是软的
4. 可以夹杂西游记梗（别太频繁），让师弟会心一笑
5. 结尾留一个开放式问题，引导师弟回复
6. 只输出消息文本，不要加引号、前缀或解释"""

    try:
        resp = client.chat.completions.create(
            model=__import__("config").settings.llm_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.9,
            max_tokens=300,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        logger.warning(f"LLM checkin generation failed: {e}")
        fallbacks = [
            f"师弟，你的「{task['title']}」进度如何了？有没有遇到什么难打的妖怪？",
            f"最近「{task['title']}」上有新进展吗？猴哥想听听你的近况～",
            f"想起来问问，「{task['title']}」练得怎么样了？",
        ]
        return random.choice(fallbacks)


def create_checkin(task_id: str, user_id: str, message: str, tone: str, silence_days: int) -> str:
    cid = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    thirty_sec_ago = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
    with engine.connect() as conn:
        # 防重：同一任务 30 秒内已有 out 方向消息则跳过（防止定时器+调试并发）
        dup = conn.execute(
            text("SELECT id FROM growth_checkins WHERE task_id = :tid AND direction = 'out' AND created_at > :since LIMIT 1"),
            {"tid": task_id, "since": thirty_sec_ago},
        ).fetchone()
        if dup:
            logger.info(f"Skip duplicate checkin for {task_id[:8]} (recent checkin exists)")
            return ""
        conn.execute(
            text("INSERT INTO growth_checkins (id, task_id, user_id, direction, message, tone, created_at) "
                 "VALUES (:id, :tid, :uid, 'out', :msg, :tone, :now)"),
            {"id": cid, "tid": task_id, "uid": user_id, "msg": message, "tone": tone, "now": now},
        )
        conn.execute(
            text("UPDATE growth_tasks SET last_checkin_at = :now, checkin_count = checkin_count + 1, "
                 "last_tone = :tone, silence_days = :sd WHERE id = :tid"),
            {"now": now, "tone": tone, "sd": silence_days, "tid": task_id},
        )
        conn.commit()
    return cid


def process_due_tasks():
    if not _process_lock.acquire(blocking=False):
        return  # 上一轮还没跑完，跳过
    try:
        _process_due_tasks_locked()
    finally:
        _process_lock.release()


def _process_due_tasks_locked():
    now = datetime.now(timezone.utc)

    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT * FROM growth_tasks WHERE status = 'in_progress' AND user_id IS NOT NULL"),
        ).fetchall()

    due: list[dict] = []
    for row in rows:
        task = dict(row._mapping)

        # 计算实际静默天数
        responded_at = task.get("user_responded_at")
        if responded_at:
            last_response = datetime.fromisoformat(responded_at)
            silence_days = (now - last_response).days
        else:
            since = task.get("last_checkin_at") or task.get("created_at")
            if since:
                silence_days = (now - datetime.fromisoformat(since)).days
            else:
                silence_days = 0

        if silence_days > 7:
            continue

        last_checkin = task.get("last_checkin_at")
        if last_checkin:
            last_dt = datetime.fromisoformat(last_checkin)
            hours = _next_checkin_hours(silence_days)
            if hours == 0:
                continue
            if now < last_dt + timedelta(hours=hours):
                continue
        else:
            # 兜底：没有 last_checkin_at 记录，跳过（等 update 逻辑补上时间戳）
            continue

        task["_silence_days"] = silence_days
        due.append(task)

    if not due:
        return

    # 并发调 LLM 生成督促消息
    with ThreadPoolExecutor(max_workers=LLM_CONCURRENCY) as pool:
        futures = {
            pool.submit(_generate_one, task): task
            for task in due
        }
        for fut in as_completed(futures, timeout=LLM_TIMEOUT * 2):
            try:
                fut.result(timeout=LLM_TIMEOUT)
            except Exception as e:
                task = futures[fut]
                logger.warning(f"Checkin timeout/fail for {task['id'][:8]}: {e}")


def _generate_one(task: dict):
    """单个任务的督促生成 + 写入（线程安全，每个任务独立 DB 连接）"""
    silence_days = task.pop("_silence_days", 0)
    tone = pick_tone(task.get("last_tone", ""))
    message = generate_checkin_message({**task, "silence_days": silence_days}, task["user_id"])
    create_checkin(task["id"], task["user_id"], message, tone, silence_days)
    logger.info(f"Checkin: task={task['id'][:8]} tone={tone} silence={silence_days}d")


def _tick():
    try:
        process_due_tasks()
    except Exception as e:
        logger.exception(f"Growth engine tick error: {e}")
    schedule_next_tick()


def schedule_next_tick():
    global _timer
    _timer = threading.Timer(CHECK_INTERVAL, _tick)
    _timer.daemon = True
    _timer.start()


def start_growth_engine():
    global _timer
    if _timer is not None:
        return
    logger.info("[GROWTH] Engine started")
    schedule_next_tick()


def stop_growth_engine():
    global _timer
    if _timer is not None:
        _timer.cancel()
        _timer = None
