# 用例：混合工具层（MCP 优先 + 失败回落进程内）——开关语义 / MCP 成功 / 回落 / 前置拒绝
# 纯单元测试：monkeypatch ai.mcp_client 与检索原语，不依赖数据库与真实 MCP 子进程
import pytest

from ai import mcp_client
from ai import retriever as retriever_mod
from ai import tools


def _ctx(**kw):
    """构造会话上下文（含 user_id / query_struct，缺省值可逐项覆盖）"""
    ctx = {"user_id": 7, "session_id": "s1", "query": "问题", "mode": "phrase",
           "rewrite": True, "query_struct": {"keywords": ["a"], "filters": {}}}
    ctx.update(kw)
    return ctx


def _capture_call(monkeypatch, payload):
    """替换 mcp_client.call_tool：记录调用次数与入参，返回固定结果"""
    box = {"calls": []}

    def fake(name, arguments):
        box["calls"].append((name, arguments))
        return payload

    monkeypatch.setattr(mcp_client, "call_tool", fake)
    return box


def test_enabled_default_on_and_off(monkeypatch):
    """开关语义：缺省开启；LITRADAR_MCP_ENABLED=0 关闭"""
    monkeypatch.delenv("LITRADAR_MCP_ENABLED", raising=False)
    assert mcp_client.enabled() is True
    monkeypatch.setenv("LITRADAR_MCP_ENABLED", "0")
    assert mcp_client.enabled() is False


def test_library_search_via_mcp(monkeypatch):
    """library_search 走 MCP：user_id / 过滤条件显式入参，brief 与进程内口径逐字一致"""
    box = _capture_call(monkeypatch, [{"id": 1, "title": "t"}])
    ctx = _ctx(query_struct={"keywords": ["a"], "filters": {"year_from": "2025"}})

    res = tools.execute("library_search", {}, ctx)

    assert res["ok"] is True
    assert res["brief"] == "命中 1 篇（user_id=7 隔离）"
    assert len(res["data"]) == 1
    name, args = box["calls"][0]
    assert name == "library_search"
    assert args["user_id"] == 7
    assert args["year_from"] == "2025"


def test_web_search_via_mcp(monkeypatch):
    """web_search 走 MCP：零命中文案与进程内一致"""
    _capture_call(monkeypatch, [])

    res = tools.execute("web_search", {}, _ctx())

    assert res["ok"] is True and res["brief"] == "网络命中 0 条"


def test_rewrite_query_via_mcp(monkeypatch):
    """rewrite_query 走 MCP：brief 沿用「已改写 / 回退原始问句」口径"""
    _capture_call(monkeypatch, {"rewritten": False, "keywords": ["x"]})

    res = tools.execute("rewrite_query", {}, _ctx())

    assert res["ok"] is True and res["brief"].startswith("回退原始问句")


def test_mcp_failure_falls_back(monkeypatch):
    """MCP 返回 None（不可用/失败）→ 回落进程内实现，链路不中断"""
    _capture_call(monkeypatch, None)
    monkeypatch.setattr(retriever_mod, "search_library",
                        lambda struct, user_id=None: [{"id": 9}])

    res = tools.execute("library_search", {}, _ctx(user_id=3))

    assert res["ok"] is True
    assert res["brief"] == "命中 1 篇（user_id=3 隔离）"


def test_disabled_uses_inprocess(monkeypatch):
    """开关关闭 → 完全不碰 MCP，直接进程内"""
    monkeypatch.setenv("LITRADAR_MCP_ENABLED", "0")
    box = _capture_call(monkeypatch, [{"id": 1}])
    monkeypatch.setattr(retriever_mod, "search_library",
                        lambda struct, user_id=None: [{"id": 2}])

    res = tools.execute("library_search", {}, _ctx())

    assert res["ok"] is True
    assert box["calls"] == []          # 未启用 → 不发起 MCP 调用


def test_library_search_without_user_id_rejects_before_mcp(monkeypatch):
    """缺 user_id → client 侧前置拒绝（越权），且不发起 MCP 调用"""
    box = _capture_call(monkeypatch, [{"id": 1}])

    res = tools.execute("library_search", {}, {"query_struct": {}})

    assert res["ok"] is False and "越权" in res["brief"]
    assert box["calls"] == []


def test_inprocess_only_tools_never_mcp(monkeypatch):
    """仅 3 个无状态工具走 MCP；memory_search 等永不走 MCP"""
    assert set(tools._MCP_TOOLS) == {"library_search", "web_search", "rewrite_query"}
    assert "memory_search" not in tools._MCP_TOOLS
