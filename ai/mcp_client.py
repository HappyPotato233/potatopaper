# MCP 客户端（混合工具层的协议侧）：以 stdio 子进程方式连接本地 MCP server
#   enabled()   LITRADAR_MCP_ENABLED 默认开（!= "0" 即启用）；置 0 退回纯进程内实现
#   list_tools() 列出 server 暴露的工具名（不可用 → None）
#   call_tool()  同步调用工具：内部经专用 event loop 线程桥接 async 会话
# 失败处置：依赖缺失 / 子进程起不来 / 握手失败 / 调用超时 → 置不可用标记并返回 None，
#           由工具层回落进程内实现，链路不中断（不反复 spawn 子进程）
import asyncio
import concurrent.futures
import json
import os
import sys
import threading
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

_DEFAULT_TIMEOUT = 30.0

_lock = threading.Lock()
_loop: asyncio.AbstractEventLoop | None = None
_loop_thread: threading.Thread | None = None
_requests: asyncio.Queue | None = None
_ready: threading.Event | None = None
_startup: dict = {"error": None}
_unavailable = False


def enabled() -> bool:
    """MCP 工具开关：默认开启；置 LITRADAR_MCP_ENABLED=0 可退回纯进程内实现"""
    return os.getenv("LITRADAR_MCP_ENABLED", "1") != "0"


def _timeout() -> float:
    try:
        return float(os.getenv("LITRADAR_MCP_TIMEOUT", str(_DEFAULT_TIMEOUT)))
    except ValueError:
        return _DEFAULT_TIMEOUT


def list_tools() -> list[str] | None:
    """列出 MCP server 暴露的工具名；未启用/不可用 → None"""
    return _invoke("list")


def call_tool(name: str, arguments: dict):
    """同步调用 MCP 工具；返回解析后的结构化结果；未启用/不可用/失败 → None"""
    return _invoke("call", name, arguments)


# ---------- 同步桥接（专用 loop 线程 + 常驻服务协程） ----------

def _invoke(kind: str, name: str | None = None, arguments: dict | None = None):
    """向常驻服务协程投递请求并同步取回结果；任何异常 → 置不可用并返回 None"""
    global _unavailable
    if not enabled() or _unavailable:
        return None
    try:
        loop, requests = _ensure_ready()
    except Exception as e:      # 依赖缺失/握手失败/超时 → 统一判不可用
        _mark_unavailable(e)
        return None
    fut: concurrent.futures.Future = concurrent.futures.Future()
    loop.call_soon_threadsafe(requests.put_nowait, (kind, name, arguments, fut))
    try:
        return fut.result(timeout=_timeout())
    except Exception as e:      # 调用超时/协议错误 → 判不可用，后续直接回落
        _mark_unavailable(e)
        return None


def _ensure_ready():
    """惰性建立 loop 线程与常驻服务协程，并等待握手完成；返回 (loop, requests)"""
    global _loop, _loop_thread, _requests, _ready
    with _lock:
        if _loop is None:
            _ready = threading.Event()
            _requests = asyncio.Queue()
            _startup["error"] = None
            _loop = asyncio.new_event_loop()
            _loop_thread = threading.Thread(target=_run_loop, name="mcp-loop", daemon=True)
            _loop_thread.start()
            _loop.call_soon_threadsafe(_loop.create_task, _serve(_requests))
        loop, requests, ready = _loop, _requests, _ready
    # 握手等待放在锁外，避免阻塞其他调用方
    if not ready.wait(timeout=_timeout()):
        raise TimeoutError("MCP 握手超时")
    if _startup["error"] is not None:
        raise _startup["error"]
    return loop, requests


def _run_loop() -> None:
    asyncio.set_event_loop(_loop)
    _loop.run_forever()


async def _serve(requests: asyncio.Queue) -> None:
    """常驻服务协程：单一任务内完成 stdio 会话全生命周期（避免跨任务退出 anyio 任务组）"""
    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "ai.mcp_server"],
            cwd=str(PROJECT_ROOT),
            env={**os.environ, "PYTHONPATH": str(PROJECT_ROOT), "PYTHONIOENCODING": "utf-8"},
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                _ready.set()
                await _pump(session, requests)
    except Exception as e:      # 握手/会话异常 → 记录并唤醒等待方（不干等超时）
        _startup["error"] = e
        _ready.set()


async def _pump(session, requests: asyncio.Queue) -> None:
    """逐条处理请求：list → 工具名列表；call → 解析后的调用结果"""
    while True:
        kind, name, arguments, fut = await requests.get()
        if kind == "stop":
            fut.set_result(None)
            return
        try:
            if kind == "list":
                res = await session.list_tools()
                fut.set_result([t.name for t in res.tools])
            else:
                fut.set_result(_parse(await session.call_tool(name, arguments)))
        except Exception as e:
            fut.set_exception(e)


def _parse(res):
    """MCP 工具返回 → Python 对象

    FastMCP 对 str 返回值会包成 structuredContent={"result": "<json 文本>"}，
    故此处对 {"result": …} 单键情形再解析一次；其余回落解析首个 text 块 JSON。
    """
    sc = getattr(res, "structuredContent", None)
    if isinstance(sc, dict) and set(sc.keys()) == {"result"}:
        return _decode(sc["result"])
    if sc is not None:
        return sc
    for block in getattr(res, "content", None) or []:
        text = getattr(block, "text", None)
        if text:
            return _decode(text)
    return None


def _decode(value):
    """JSON 文本 → Python 对象；非 JSON 文本原样返回"""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _mark_unavailable(err: Exception) -> None:
    """标记不可用：本进程后续直接走进程内回落（打印一次告警）"""
    global _unavailable
    if not _unavailable:
        print(f"[mcp_client] MCP 通道不可用，回落进程内实现：{err}")
    _unavailable = True
