# 用例：MCP stdio 真实链路冒烟（list_tools 含 3 工具 + rewrite_query phrase 模式可调用）
# 真实子进程：依赖 mcp 包与本地解释器；不可用则 skip（不阻断 CI）
import pytest

from ai import mcp_client


def test_stdio_smoke(monkeypatch):
    """真实 stdio 子进程：工具清单正确，且无 LLM/无库依赖的 rewrite_query 可调用"""
    pytest.importorskip("mcp")
    monkeypatch.setenv("LITRADAR_MCP_ENABLED", "1")

    names = mcp_client.list_tools()
    if names is None:
        pytest.skip("MCP server 子进程不可用")
    assert set(names) == {"library_search", "web_search", "rewrite_query"}

    data = mcp_client.call_tool(
        "rewrite_query", {"user_query": "对比两种方法", "mode": "phrase", "enabled": False})
    assert isinstance(data, dict) and "keywords" in data


def test_main_warms_up_before_serving(monkeypatch):
    """回归防护：main() 必须先预热重依赖、再启动 stdio 服务

    根因：sentence_transformers 首次导入（连锁加载 sklearn/scipy 的 C 扩展 DLL）若发生在工具调用阶段，
    会与 stdio 事件循环的并发 import 争抢 Windows 加载器锁而整体卡死（工具线程阻塞在 C 扩展 create_module）。
    故须把首次导入前移到单线程启动阶段。
    """
    pytest.importorskip("mcp")
    from ai import mcp_server

    calls: list[str] = []
    monkeypatch.setattr(mcp_server, "_warmup", lambda: calls.append("warmup"))
    monkeypatch.setattr(mcp_server.mcp, "run", lambda **kw: calls.append("run"))

    mcp_server.main()
    assert calls == ["warmup", "run"]
