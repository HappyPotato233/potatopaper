# 内存任务队列：task_id 幂等 + 指数退避重试（≤3 次）+ 线程 worker 循环
# 队列形态对齐详细设计 §5.1：抽象接口，后续可替换 Redis + RQ 而业务代码不变
import queue
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

MAX_RETRY = 3


class RetryableError(Exception):
    """非致命错误（来源 429/超时/瞬时）：队列级重试退避"""


class FatalError(Exception):
    """致命错误（配置/表结构异常）：落 failed 不重试"""


@dataclass
class Task:
    task_id: str
    type: str                 # follow_up / qa
    payload: dict = field(default_factory=dict)
    retry: int = 0


class TaskQueue:
    """线程安全内存队列；worker 线程消费并分发到注册的 handler"""

    def __init__(self, num_workers: int = 2):
        self._q: queue.Queue[Task] = queue.Queue()
        self._handlers: dict[str, Callable[[Task], None]] = {}
        self._processed: set[str] = set()          # 幂等键（已处理 task_id）
        self._lock = threading.Lock()
        self._num_workers = num_workers
        self._threads: list[threading.Thread] = []

    # ---------------- 对外接口 ----------------

    def register_handler(self, task_type: str, handler: Callable[[Task], None]) -> None:
        self._handlers[task_type] = handler

    def enqueue(self, task_type: str, payload: dict, task_id: str | None = None) -> str:
        """下发任务，返回 task_id（幂等：同 id 重复下发直接忽略）"""
        task_id = task_id or uuid.uuid4().hex[:16]
        with self._lock:
            if task_id in self._processed:
                return task_id
        self._q.put(Task(task_id=task_id, type=task_type, payload=payload))
        return task_id

    def start(self) -> None:
        """启动 worker 守护线程"""
        for i in range(self._num_workers):
            t = threading.Thread(target=self._worker_loop, name=f"radar-worker-{i}", daemon=True)
            t.start()
            self._threads.append(t)
        print(f"[worker] 任务队列已启动（{self._num_workers} 个 worker 线程）")

    # ---------------- 内部消费循环 ----------------

    def _worker_loop(self) -> None:
        while True:
            task = self._q.get()
            if task is None:
                break
            try:
                self._run_task(task)
            finally:
                self._q.task_done()

    def _run_task(self, task: Task) -> None:
        # 幂等检查：已处理过的任务 id 忽略（防重复消费）
        with self._lock:
            if task.task_id in self._processed:
                return
        handler = self._handlers.get(task.type)
        if handler is None:
            print(f"[worker] 未知任务类型: {task.type}，丢弃")
            self._mark_processed(task.task_id)
            return
        try:
            handler(task)
            self._mark_processed(task.task_id)
        except RetryableError as e:
            if task.retry < MAX_RETRY:
                delay = min(60.0, 2 ** task.retry)  # 指数退避
                print(f"[worker] 任务 {task.task_id} 可重试失败（{e}），{delay}s 后重试 "
                      f"（第 {task.retry + 1}/{MAX_RETRY} 次）")
                time.sleep(delay)
                task.retry += 1
                self._q.put(task)
            else:
                print(f"[worker] 任务 {task.task_id} 重试耗尽，标记失败: {e}")
                self._mark_processed(task.task_id)
        except FatalError as e:
            print(f"[worker] 任务 {task.task_id} 致命错误，落 failed: {e}")
            self._mark_processed(task.task_id)
        except Exception as e:
            # 未预期异常：打印堆栈并终止该任务（handler 内部应尽量自处理状态）
            traceback.print_exc()
            print(f"[worker] 任务 {task.task_id} 未预期异常: {e}")
            self._mark_processed(task.task_id)

    def _mark_processed(self, task_id: str) -> None:
        with self._lock:
            self._processed.add(task_id)
            # 防止集合无限增长：粗略上限后重建
            if len(self._processed) > 10_000:
                self._processed = set(list(self._processed)[-5_000:])


# 进程级单例（FastAPI 与调度器共用）
task_queue = TaskQueue()
