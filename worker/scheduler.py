# 调度器：周期 tick + 错过窗口 coalesce 合并（对齐详细设计 §4.1）
# 自研轻量调度（文档允许"定时器 或 APScheduler"），单进程线程模型
import threading
import time
from datetime import datetime

from sqlalchemy import func, select

from app.models import Setting, Subscription
from app.session import SessionLocal
from worker.queue import task_queue

# 频率档位 → 秒（对齐设置页四档：每小时 / 每 6 小时 / 每日 / 每周）
FREQ_SECONDS = {"hourly": 3600, "6h": 21600, "daily": 86400, "weekly": 604800}
TICK_INTERVAL = 60  # 每分钟检查一次到期订阅

# 在途保护：已下发但未完成的订阅，防止调度重复下发
_inflight: set[int] = set()
_inflight_lock = threading.Lock()


def release(sub_id: int) -> None:
    """管道结束后释放在途标记（由 pipeline 调用）"""
    with _inflight_lock:
        _inflight.discard(sub_id)


class Scheduler:
    """后台守护线程：周期遍历启用订阅，游标到期即下发跟进单（跨窗口合并）"""

    def __init__(self):
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="radar-scheduler", daemon=True)
        self._thread.start()
        print(f"[scheduler] 调度器已启动（tick={TICK_INTERVAL}s）")

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(TICK_INTERVAL):
            try:
                self.tick()
            except Exception as e:
                print(f"[scheduler] tick 异常: {e}")

    def tick(self) -> int:
        """单次调度：返回本次下发的跟进单数"""
        now = datetime.now()
        # 启用订阅 + 所属用户的跟进频率（用户未配置则默认 daily）
        subs = _due_subs()
        enqueued = sum(_enqueue_if_due(sub, now, kind="schedule") for sub in subs)
        return enqueued


def enqueue_due_for_user(user_id: int, kind: str = "login") -> int:
    """登录惰性触发：对该用户所有到期订阅补发跟进单（不阻塞登录）"""
    now = datetime.now()
    subs = _due_subs(user_id)
    return sum(_enqueue_if_due(sub, now, kind=kind) for sub in subs)


def _due_subs(user_id: int | None = None) -> list:
    """查询启用订阅及其用户跟进频率（user_id 非空时仅限该用户）"""
    conditions = [Subscription.active.is_(True)]
    if user_id is not None:
        conditions.append(Subscription.user_id == user_id)
    with SessionLocal() as session:
        return session.execute(
            select(Subscription.id, Subscription.last_run_at, Subscription.created_at,
                   func.coalesce(Setting.frequency, "daily").label("frequency"))
            .outerjoin(Setting, Setting.user_id == Subscription.user_id)
            .where(*conditions)
        ).mappings().all()


def _enqueue_if_due(sub: dict, now: datetime, kind: str) -> int:
    """单订阅到期判定 + 下发（在途保护 + coalesce 合并），返回是否下发 0/1"""
    interval = FREQ_SECONDS.get(sub["frequency"], FREQ_SECONDS["daily"])
    cursor = sub["last_run_at"] or sub["created_at"]
    if isinstance(cursor, str):
        cursor = datetime.fromisoformat(cursor)
    elapsed = (now - cursor).total_seconds()
    if elapsed < interval:
        return 0
    with _inflight_lock:
        if sub["id"] in _inflight:
            return 0  # 在途任务未完成，跳过
        _inflight.add(sub["id"])
    # coalesce：距游标跨越多个调度周期 → 合并为一次执行（不重复不遗漏）
    missed_windows = max(1, int(elapsed // interval))
    task_queue.enqueue(
        "follow_up",
        {
            "subscription_id": sub["id"],
            "kind": kind,
            "merge": missed_windows > 1,
            "merged_windows": missed_windows,
        },
    )
    return 1


scheduler = Scheduler()
