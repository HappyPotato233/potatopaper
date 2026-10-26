# 用例：LLM 429 指数退避重试（只重试限流、退避间隔递增、其他异常不重试、重试耗尽原样抛出）
import threading
from types import SimpleNamespace

import pytest

from ai import llm_provider


class _LimitError(Exception):
    """模拟 429：只带 status_code，验证 _is_rate_limited 的通用判定分支"""

    status_code = 429


def _model() -> dict:
    return {"base_url": "", "api_key_enc": "", "model_id": "m", "extra": None}


def _fake_client(create):
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def _setup(monkeypatch, create, slots: int = 2):
    """统一替换模型解析、客户端与槽位，并把 sleep 换成记录器"""
    delays: list[float] = []
    monkeypatch.setattr(llm_provider, "get_agent_model", lambda key: _model())
    monkeypatch.setattr(llm_provider, "_get_client", lambda base, key: _fake_client(create))
    monkeypatch.setattr(llm_provider, "_LLM_SLOTS", threading.BoundedSemaphore(slots))
    monkeypatch.setattr(llm_provider.time, "sleep", lambda seconds: delays.append(seconds))
    return delays


def _ok_response():
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
                           usage=None)


def test_429_retries_then_succeeds(monkeypatch):
    """连续两次 429 后成功：共调用 3 次，退避间隔按 1s / 2s 递增（含 0~0.3s 抖动）"""
    calls: list[int] = []

    def create(**kwargs):
        calls.append(1)
        if len(calls) <= 2:
            raise _LimitError("429 Too Many Requests")
        return _ok_response()

    delays = _setup(monkeypatch, create)
    assert llm_provider.chat("main", "q") == "ok"
    assert len(calls) == 3
    assert len(delays) == 2
    assert 1.0 <= delays[0] <= 1.3
    assert 2.0 <= delays[1] <= 2.3


def test_429_exhausted_raises(monkeypatch):
    """4 次全 429（首次 + 3 次重试）后原样抛出，交给上层降级"""
    calls: list[int] = []

    def create(**kwargs):
        calls.append(1)
        raise _LimitError("429")

    delays = _setup(monkeypatch, create)
    with pytest.raises(_LimitError):
        llm_provider.chat("main", "q")
    assert len(calls) == llm_provider.LLM_RETRY_MAX + 1
    assert len(delays) == llm_provider.LLM_RETRY_MAX


def test_non_429_is_not_retried(monkeypatch):
    """非 429（如超时/5xx）不重试：调用一次即抛出，避免无谓等待"""
    calls: list[int] = []

    def create(**kwargs):
        calls.append(1)
        raise ValueError("timeout")

    delays = _setup(monkeypatch, create)
    with pytest.raises(ValueError):
        llm_provider.chat("main", "q")
    assert len(calls) == 1
    assert delays == []
