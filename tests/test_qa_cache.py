# 用例：问答结果缓存（归一化、键隔离、TTL 过期、上限淘汰、按用户清空、开关关闭）+ 会话命中/排队超时
import threading
import time
from types import SimpleNamespace

import pytest

from ai import orchestrator, qa_cache


@pytest.fixture(autouse=True)
def _clean_cache(monkeypatch):
    """每个用例前清空缓存并还原开关，避免用例互相污染"""
    qa_cache.clear()
    monkeypatch.delenv("LITRADAR_QA_CACHE", raising=False)
    yield
    qa_cache.clear()


class _FakeGraph:
    """只存 state 的假图：满足 _seed_pipeline / _load_session / _cleanup_expired"""

    def __init__(self):
        self.states: dict[str, dict] = {}
        self.invoked = 0

    def update_state(self, cfg, values):
        self.states[cfg["configurable"]["thread_id"]] = values

    def get_state(self, cfg):
        return SimpleNamespace(values=self.states.get(cfg["configurable"]["thread_id"]))

    def delete_thread(self, thread_id):
        self.states.pop(thread_id, None)

    def invoke(self, state, cfg):
        self.invoked += 1


def test_normalize_and_key_equivalence():
    """空白与大小写差异视为同一条问句；用户/形态/开关任一不同则键不同"""
    a = qa_cache.make_key(1, "  RAG   是什么 ", "phrase", True, True)
    b = qa_cache.make_key(1, "rag 是什么", "phrase", True, True)
    assert a == b
    assert a != qa_cache.make_key(2, "rag 是什么", "phrase", True, True)
    assert a != qa_cache.make_key(1, "rag 是什么", "paragraph", True, True)
    assert a != qa_cache.make_key(1, "rag 是什么", "phrase", False, True)
    assert a != qa_cache.make_key(1, "rag 是什么", "phrase", True, False)


def test_put_get_expiry(monkeypatch):
    """TTL 内命中、过期后未命中"""
    now = [1000.0]
    monkeypatch.setattr(qa_cache, "time", SimpleNamespace(time=lambda: now[0]))
    monkeypatch.setenv("LITRADAR_QA_CACHE_TTL", "10")
    key = qa_cache.make_key(1, "q", "phrase", True, True)
    qa_cache.put(key, 1, {"result": {"answer": "a"}})

    now[0] = 1005.0
    assert qa_cache.get(key)["result"]["answer"] == "a"

    now[0] = 1011.0
    assert qa_cache.get(key) is None


def test_max_size_evicts_oldest(monkeypatch):
    """超过条目上限时淘汰最久未访问的一条"""
    monkeypatch.setenv("LITRADAR_QA_CACHE_MAX", "2")
    keys = [qa_cache.make_key(1, f"q{i}", "phrase", True, True) for i in range(3)]
    for key in keys:
        qa_cache.put(key, 1, {"result": {"answer": key}})

    assert qa_cache.get(keys[0]) is None
    assert qa_cache.get(keys[1]) is not None
    assert qa_cache.get(keys[2]) is not None


def test_clear_user_keeps_others():
    """worker 入库后只清该用户的缓存，别人的不受影响"""
    key_a = qa_cache.make_key(1, "q", "phrase", True, True)
    key_b = qa_cache.make_key(2, "q", "phrase", True, True)
    qa_cache.put(key_a, 1, {"result": {"answer": "a"}})
    qa_cache.put(key_b, 2, {"result": {"answer": "b"}})

    assert qa_cache.clear_user(1) == 1
    assert qa_cache.get(key_a) is None
    assert qa_cache.get(key_b)["result"]["answer"] == "b"


def test_disabled_by_env(monkeypatch):
    """LITRADAR_QA_CACHE=0 时不读不写（评测口径）"""
    monkeypatch.setenv("LITRADAR_QA_CACHE", "0")
    key = qa_cache.make_key(1, "q", "phrase", True, True)
    qa_cache.put(key, 1, {"result": {"answer": "a"}})
    assert qa_cache.get(key) is None


def test_ask_returns_cached_result_without_pipeline(monkeypatch):
    """缓存命中：直接返回上次终态，不派生流水线线程、不调用上游"""
    graph = _FakeGraph()
    monkeypatch.setattr(orchestrator, "_PIPELINE_GRAPH", graph)
    ran: list[str] = []
    monkeypatch.setattr(orchestrator, "_run_pipeline", lambda session: ran.append(session))
    snapshot = {"complexity": "normal",
                "query_struct": {"query": "rag", "rewritten": True},
                "routes": [],
                "steps": [{"key": "rewrite", "label": "查询改写", "status": "done",
                           "detail": "已改写"}],
                "tool_calls": [], "review_notes": None, "planner_notes": [],
                "iteration_count": 1,
                "result": {"answer": "上次的答案", "citations": [], "points": []}}
    key = qa_cache.make_key(1, "RAG 是什么", "phrase", True, True)
    qa_cache.put(key, 1, snapshot)

    t0 = time.perf_counter()
    session_id = orchestrator.ask(1, " rag  是什么 ", mode="phrase")
    elapsed = time.perf_counter() - t0

    result = orchestrator.get_result(session_id)
    assert result["finished"] is True
    assert result["answer"] == "上次的答案"
    assert result["iteration_count"] == 1
    steps = orchestrator.get_steps(session_id)["steps"]
    assert "缓存命中" in steps[0]["detail"]
    assert graph.invoked == 0
    assert ran == []          # 没有派生流水线
    assert elapsed < 1.0      # 也没有等待任何上游调用


def test_pipeline_queue_timeout_reports_busy(monkeypatch):
    """流水线槽位被占满且超过排队上限时：会话立即结束并给出「请求过多」提示"""
    graph = _FakeGraph()
    monkeypatch.setattr(orchestrator, "_PIPELINE_GRAPH", graph)
    monkeypatch.setattr(orchestrator, "_PIPELINE_SLOTS", threading.BoundedSemaphore(1))
    monkeypatch.setenv("LITRADAR_QA_QUEUE_TIMEOUT", "0.1")
    orchestrator._PIPELINE_SLOTS.acquire()   # 模拟已有一条流水线在跑
    try:
        session = {"session_id": "busy1", "user_id": 1, "query": "q", "mode": "phrase",
                   "steps": [], "finished": False, "error": None, "result": None}
        orchestrator._run_pipeline(session)
    finally:
        orchestrator._PIPELINE_SLOTS.release()

    assert session["finished"] is True
    assert session["error"] == orchestrator.QA_BUSY_MESSAGE
    assert graph.invoked == 0


def test_store_cache_skips_error_and_review(monkeypatch):
    """失败或空答案、以及综述会话都不写缓存"""
    monkeypatch.setattr(orchestrator, "_PIPELINE_GRAPH", _FakeGraph())
    base = {"user_id": 1, "query": "q", "mode": "phrase", "rewrite": True,
            "enable_planner": True}
    orchestrator._store_cache({**base, "error": "boom", "result": {"answer": "x"}})
    orchestrator._store_cache({**base, "error": None, "result": None})
    orchestrator._store_cache({**base, "mode": "review", "error": None,
                               "result": {"answer": "x"}})
    assert qa_cache.get(qa_cache.make_key(1, "q", "phrase", True, True)) is None

    orchestrator._store_cache({**base, "error": None, "result": {"answer": "x"}})
    assert qa_cache.get(qa_cache.make_key(1, "q", "phrase", True, True))["result"]["answer"] == "x"
