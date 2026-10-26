# 用例：问答编排与 LLM 调用的并发闸门（超限排队、峰值不越界、环境变量解析）
import threading
import time
from types import SimpleNamespace

from ai import llm_provider, orchestrator


class _FakeGraph:
    """只记录并发峰值的假图：invoke 睡眠期间占住一个流水线槽位"""

    def __init__(self, delay: float = 0.05):
        self.delay = delay
        self.lock = threading.Lock()
        self.active = 0
        self.peak = 0
        self.checkpointer = self

    def invoke(self, state, cfg):
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        time.sleep(self.delay)
        with self.lock:
            self.active -= 1

    def update_state(self, cfg, values):
        pass

    def delete_thread(self, thread_id):
        pass


def _run_many(fn, n: int) -> None:
    threads = [threading.Thread(target=fn, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)


def test_pipeline_slots_cap_concurrency(monkeypatch):
    """流水线槽位为 2 时，6 条并发流水线最多同时跑 2 条，且都能正常收尾"""
    graph = _FakeGraph()
    monkeypatch.setattr(orchestrator, "_PIPELINE_GRAPH", graph)
    monkeypatch.setattr(orchestrator, "_PIPELINE_SLOTS", threading.BoundedSemaphore(2))
    sessions = [{"session_id": f"t{i}"} for i in range(6)]

    _run_many(lambda i: orchestrator._run_pipeline(sessions[i]), len(sessions))

    assert graph.peak <= 2
    assert all(s["finished"] is True for s in sessions)


def test_llm_slots_cap_upstream_calls(monkeypatch):
    """LLM 槽位为 3 时，8 个并发调用最多同时打到上游 3 个"""
    state = {"lock": threading.Lock(), "active": 0, "peak": 0}

    class _Completions:
        def create(self, **kwargs):
            with state["lock"]:
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
            time.sleep(0.05)
            with state["lock"]:
                state["active"] -= 1
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))], usage=None)

    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=_Completions()))
    monkeypatch.setattr(llm_provider, "get_agent_model",
                        lambda key: {"base_url": "", "api_key_enc": "", "model_id": "m",
                                     "extra": None})
    monkeypatch.setattr(llm_provider, "_get_client", lambda base, key: fake_client)
    monkeypatch.setattr(llm_provider, "_LLM_SLOTS", threading.BoundedSemaphore(3))

    results: list[str] = []
    _run_many(lambda i: results.append(llm_provider.chat("main", f"q{i}")), 8)

    assert state["peak"] <= 3
    assert results == ["ok"] * 8


def test_concurrency_env_parsing(monkeypatch):
    """环境变量可覆盖上限与排队时长；非法值回退默认"""
    monkeypatch.setenv("LITRADAR_QA_MAX_CONCURRENCY", "7")
    monkeypatch.setenv("LITRADAR_LLM_MAX_CONCURRENCY", "11")
    monkeypatch.setenv("LITRADAR_QA_QUEUE_TIMEOUT", "12.5")
    assert orchestrator._qa_max_concurrency() == 7
    assert llm_provider._llm_max_concurrency() == 11
    assert orchestrator._qa_queue_timeout() == 12.5

    monkeypatch.setenv("LITRADAR_QA_MAX_CONCURRENCY", "0")
    monkeypatch.setenv("LITRADAR_LLM_MAX_CONCURRENCY", "abc")
    monkeypatch.setenv("LITRADAR_QA_QUEUE_TIMEOUT", "0")
    assert orchestrator._qa_max_concurrency() == 1
    assert llm_provider._llm_max_concurrency() == llm_provider.LLM_MAX_CONCURRENCY
    assert orchestrator._qa_queue_timeout() == orchestrator.QA_QUEUE_TIMEOUT
