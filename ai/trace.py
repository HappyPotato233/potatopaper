# 运行追踪器（可选启用，零副作用）：记录 LLM 调用 / 工具调用 / 步骤流转，供评测与调试
# 设计：默认未启用（active() 返回 None），所有 record_* 为 no-op，不改变任何业务行为；
#       评测 runner 调 set_active(recorder) 后，llm_provider / orchestrator 自动上报事件。
# 线程说明：流水线在后台线程执行，contextvar 不会跨线程继承，故此处用进程级单例（评测按顺序单跑）。
import threading
import time
from datetime import datetime

_lock = threading.Lock()
_ACTIVE: "Recorder | None" = None


def now_iso() -> str:
    """当前时刻字符串（毫秒精度），用于事件时间戳"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


class Recorder:
    """事件收集器：按先后顺序记录事件，runner 用 case_start 事件切分单题区间"""

    def __init__(self) -> None:
        self.events: list[dict] = []

    # ---- 通用记录 ----
    def add(self, kind: str, session_id: str = "", **fields) -> None:
        """追加一条事件（kind：case_start / llm / tool / step / note）"""
        with _lock:
            self.events.append({"kind": kind, "ts": now_iso(),
                                "session_id": session_id, **fields})

    # ---- 分类型便捷方法 ----
    def llm(self, agent_key: str, model_id: str, prompt: str, response: str = "",
            usage: dict | None = None, error: str = "",
            duration_ms: int | None = None, session_id: str = "") -> None:
        """记录一次 LLM 调用（prompt/response 原文 + token 用量 + 耗时）"""
        self.add("llm", session_id, agent_key=agent_key, model_id=model_id,
                 prompt=prompt, response=response, usage=usage or {},
                 error=error, duration_ms=duration_ms)

    def tool(self, name: str, args: dict, ok: bool, brief: str,
             duration_ms: int | None = None, session_id: str = "") -> None:
        """记录一次工具调用（工具名/入参/是否成功/结果摘要 + 耗时）"""
        self.add("tool", session_id, name=name, args=args, ok=ok, brief=brief,
                 duration_ms=duration_ms)

    def step(self, key: str, status: str, detail: str = "", session_id: str = "") -> None:
        """记录一次步骤状态流转（8 节点步骤条）"""
        self.add("step", session_id, key=key, status=status, detail=detail)


def set_active(recorder: "Recorder | None") -> None:
    """启用/切换当前追踪器（评测 runner 调用）"""
    global _ACTIVE
    _ACTIVE = recorder


def active() -> "Recorder | None":
    """当前追踪器；未启用返回 None（调用方据此跳过记录）"""
    return _ACTIVE


def clear() -> None:
    """关闭追踪"""
    set_active(None)


class Stopwatch:
    """上下文管理器：测量代码段耗时（毫秒），事件记录用"""

    def __enter__(self):
        self._t0 = time.perf_counter()
        return self

    def __exit__(self, *exc):
        self.ms = int((time.perf_counter() - self._t0) * 1000)
        return False
