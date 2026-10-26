# MCP server（stdio）：暴露 3 个无状态工具（library_search / web_search / rewrite_query）
#   - 直接复用既有 ai.retriever / ai.rewrite 实现，不复制业务逻辑
#   - 不 import ai.tools（避免与 client 形成循环依赖）
#   - 入参为「编排层已合并的检索条件」+ user_id（服务端不做鉴权，维持客户端已校验口径）
#   - 入口：python -m ai.mcp_server（FastMCP 默认 stdio 传输）
import json

import anyio.to_thread
from mcp.server.fastmcp import FastMCP

from ai import retriever as retriever_mod
from ai import rewrite as rewrite_mod

mcp = FastMCP("litradar-tools")


def _struct(query: str, keywords, year_from, year_to, sources, cats) -> dict:
    """组装检索式：filters 四字段全传 + 保留 query（keywords 为空时检索层回退 query，避免行为漂移）"""
    return {
        "query": query or "",
        "keywords": [k for k in (keywords or []) if k],
        "filters": {
            "year_from": year_from or "",
            "year_to": year_to or "",
            "sources": [s for s in (sources or []) if s],
            "cats": [c for c in (cats or []) if c],
        },
    }


@mcp.tool()
async def library_search(keywords: list[str], query: str = "", year_from: str = "",
                         year_to: str = "", sources: list[str] | None = None,
                         cats: list[str] | None = None, user_id: int | None = None) -> str:
    """库内检索：词法路（FULLTEXT/LIKE）+ 向量路（bge）双路召回 → RRF 融合（复用 retriever.search_library）

    阻塞调用经线程池 offload：bge/torch 模型加载会长时间占用线程，若直接在事件循环内执行会卡死 stdio 读写。
    """
    struct = _struct(query, keywords, year_from, year_to, sources, cats)
    evidence = await anyio.to_thread.run_sync(
        lambda: retriever_mod.search_library(struct, user_id=user_id))
    return json.dumps(evidence, ensure_ascii=False)


@mcp.tool()
async def web_search(keywords: list[str], query: str = "", year_from: str = "",
                     year_to: str = "", sources: list[str] | None = None,
                     cats: list[str] | None = None) -> str:
    """实时网络学术检索（复用 retriever.search_web；阻塞的网络 IO 经线程池 offload）"""
    struct = _struct(query, keywords, year_from, year_to, sources, cats)
    evidence = await anyio.to_thread.run_sync(lambda: retriever_mod.search_web(struct))
    return json.dumps(evidence, ensure_ascii=False)


@mcp.tool()
async def rewrite_query(user_query: str, mode: str = "phrase", enabled: bool = True) -> str:
    """查询改写（复用 rewrite.rewrite_query），失败回退原始问句（LLM 调用经线程池 offload）"""
    struct = await anyio.to_thread.run_sync(
        lambda: rewrite_mod.rewrite_query(user_query, mode=mode, enabled=enabled))
    return json.dumps(struct, ensure_ascii=False)


def _warmup() -> None:
    """预热重依赖：`sentence_transformers`→sklearn/scipy 首次导入会加载大量 C 扩展 DLL

    若推迟到工具调用阶段执行，会与 stdio 事件循环的并发 import 争抢 Windows 加载器锁而整体卡死
    （实测：工具调用线程阻塞在 scipy C 扩展 create_module，永不返回）。
    故在启动（单线程、事件循环尚未运行）阶段先完成导入；导入失败不影响服务（检索层自带回落）。
    """
    try:
        import sentence_transformers  # noqa: F401
    except Exception:
        pass


def main() -> None:
    """stdio 传输启动（供 `python -m ai.mcp_server` 调用）"""
    _warmup()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
