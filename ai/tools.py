# 工具层（编排方案 v4 §3 / 详细设计 §5.1.2）：Planner 可调用的 6 个工具 + 统一权限校验
#   library_search  本地文献库检索，user_id 隔离（越权返回空）
#   web_search      网络实时学术检索（限速退避由适配层负责）
#   memory_search   读取当前会话历史（session_id 校验）
#   rewrite_query   查询改写，失败回退原始 query
#   run_analysts    分析师集群按 enable_list 调用
#   run_reviewer    复审质检，输入非空校验
# 统一返回：{ok, brief, data, error}——brief 用于前端思考面板的工具调用记录
# 混合工具层（MCP 接入）：library_search / web_search / rewrite_query 三个无状态工具优先走
#   MCP 通道（ai.mcp_client），未启用/不可用/失败则回落下方进程内实现；其余三个工具恒进程内
#   （memory_search 吃 session_id + 会话历史、run_analysts/run_reviewer 吃证据对象引用，过协议不划算）
from ai import analysts as analysts_mod
from ai import retriever as retriever_mod
from ai import reviewer as reviewer_mod
from ai import rewrite as rewrite_mod

TOOL_NAMES = ("library_search", "web_search", "memory_search",
              "rewrite_query", "run_analysts", "run_reviewer")


def execute(name: str, args: dict | None, ctx: dict) -> dict:
    """工具执行入口（统一权限校验 + 异常降级 + MCP 优先/回落）

    3 个无状态工具（_MCP_TOOLS）先走 MCP 通道，成功则按进程内口径重建返回；
    未启用/不可用/失败 → 回落进程内实现；其余工具恒走进程内。

    :param ctx: 会话上下文（含 user_id / session_id / query / mode / rewrite / query_struct /
                evidence / analyst_outputs，以及 _memory_records 回调）
    :return: {ok: bool, brief: str, data: any, error: str}
    """
    args = args or {}
    if name in _MCP_TOOLS:
        res = _try_mcp(name, args, ctx)
        if res is not None:
            return res
    try:
        return _DISPATCH[name](args, ctx)
    except KeyError:
        return _fail(f"未知工具：{name}")
    except Exception as e:  # 单工具失败不阻断整体（v4 §5：单个分析师/工具失败其余照常）
        print(f"[tools] {name} 执行失败: {e}")
        return _fail(f"执行失败：{e}")


def _fail(reason: str) -> dict:
    return {"ok": False, "brief": reason, "data": None, "error": reason}


def _ok(brief: str, data=None) -> dict:
    return {"ok": True, "brief": brief, "data": data, "error": ""}


# ---------- MCP 通道（混合层：3 个无状态工具优先走 MCP，失败回落进程内） ----------

_MCP_TOOLS = ("library_search", "web_search", "rewrite_query")


def _try_mcp(name: str, args: dict, ctx: dict) -> dict | None:
    """MCP 通道优先：成功 → 按进程内口径重建 {ok,brief,data}；未启用/失败 → None（回落）"""
    try:
        from ai import mcp_client          # 惰性导入：mcp 依赖缺失也不阻断工具层加载
    except Exception as e:
        print(f"[tools] MCP 客户端不可用，{name} 回落进程内：{e}")
        return None
    if not mcp_client.enabled():
        return None
    params = _mcp_params(name, args, ctx)
    if params is None:                      # 前置校验不通过（如缺 user_id）→ 回落走进程内校验
        return None
    try:
        data = mcp_client.call_tool(name, params)
    except Exception as e:
        print(f"[tools] MCP 调用失败，{name} 回落进程内：{e}")
        return None
    if data is None:                        # 不可用/解析失败 → 回落
        return None
    return _mcp_result(name, ctx, data)


def _mcp_params(name: str, args: dict, ctx: dict) -> dict | None:
    """工具入参 → MCP 显式入参（复用 _merge_struct，保持「会话检索式 + 工具入参」合并口径）

    library_search 缺 user_id → 返回 None（client 侧前置拒绝，不发起 MCP，回落走进程内越权校验）
    """
    if name == "library_search":
        user_id = ctx.get("user_id")
        if not user_id:
            return None
        return {"user_id": user_id, **_merged_params(args, ctx)}
    if name == "web_search":
        return _merged_params(args, ctx)
    # rewrite_query
    return {"user_query": args.get("user_query") or ctx.get("query") or "",
            "mode": ctx.get("mode", "phrase"), "enabled": ctx.get("rewrite", True)}


def _merged_params(args: dict, ctx: dict) -> dict:
    """合并后的检索条件 → MCP 显式入参（filters 四字段 + query 全传，避免关键词为空时行为漂移）"""
    merged = _merge_struct(ctx.get("query_struct") or {}, args)
    filters = merged.get("filters") or {}
    return {"keywords": merged.get("keywords") or [],
            "query": merged.get("query") or "",
            "year_from": filters.get("year_from", ""),
            "year_to": filters.get("year_to", ""),
            "sources": filters.get("sources") or [],
            "cats": filters.get("cats") or []}


def _mcp_result(name: str, ctx: dict, data) -> dict:
    """MCP 结果 → 与进程内逐字一致的 {ok,brief,data}（brief 进前端面板与评测记录）"""
    if name == "library_search":
        return _ok(f"命中 {len(data)} 篇（user_id={ctx.get('user_id')} 隔离）", data)
    if name == "web_search":
        return _ok(f"网络命中 {len(data)} 条", data)
    struct = data if isinstance(data, dict) else {}
    rewritten = "已改写" if struct.get("rewritten") else "回退原始问句"
    return _ok(f"{rewritten}：{' '.join(struct.get('keywords') or [])[:60]}", struct)


# ---------- library_search：库内检索（user_id 隔离） ----------

def _library_search(args: dict, ctx: dict) -> dict:
    user_id = ctx.get("user_id")
    if not user_id:
        # 权限校验（§5.1.2）：必须携带 user_id，越权返回空
        return _fail("越权：缺少 user_id，拒绝检索")
    struct = _merge_struct(ctx.get("query_struct") or {}, args)
    evidence = retriever_mod.search_library(struct, user_id=user_id)
    return _ok(f"命中 {len(evidence)} 篇（user_id={user_id} 隔离）", evidence)


# ---------- web_search：网络实时检索 ----------

def _web_search(args: dict, ctx: dict) -> dict:
    struct = _merge_struct(ctx.get("query_struct") or {}, args)
    evidence = retriever_mod.search_web(struct)
    return _ok(f"网络命中 {len(evidence)} 条", evidence)


# ---------- memory_search：会话历史读取（session_id 校验） ----------

def _memory_search(args: dict, ctx: dict) -> dict:
    session_id = ctx.get("session_id")
    asked = args.get("session_id")
    if not session_id or (asked and asked != session_id):
        return _fail("越权：session_id 校验不通过")
    from ai import orchestrator as _orch        # 惰性导入：运行期解析，避免模块级循环依赖
    records = _orch._memory_records(ctx.get("user_id"), session_id)
    struct = ctx.get("query_struct") or {}
    evidence = retriever_mod.search_memory(
        records, ctx.get("query", ""), struct.get("keywords"))
    return _ok(f"历史会话命中 {len(evidence)} 条", evidence)


# ---------- rewrite_query：查询改写（失败回退原始问句） ----------

def _rewrite_query(args: dict, ctx: dict) -> dict:
    user_query = args.get("user_query") or ctx.get("query") or ""
    struct = rewrite_mod.rewrite_query(
        user_query, mode=ctx.get("mode", "phrase"), enabled=ctx.get("rewrite", True))
    rewritten = "已改写" if struct.get("rewritten") else "回退原始问句"
    return _ok(f"{rewritten}：{' '.join(struct.get('keywords') or [])[:60]}", struct)


# ---------- run_analysts：分析师集群（按 enable_list） ----------

def _run_analysts(args: dict, ctx: dict) -> dict:
    evidence = ctx.get("evidence") or []
    keys = [k for k in (args.get("enable_list") or []) if k in ("method", "critic", "compare")]
    outputs = analysts_mod.run_analysts(evidence, keys=keys or None)
    ok = [k for k, v in outputs.items() if v]
    return _ok(f"完成视角：{', '.join(ok) if ok else '全部降级跳过'}", outputs)


# ---------- run_reviewer：复审质检（输入非空校验） ----------

def _run_reviewer(args: dict, ctx: dict) -> dict:
    question = args.get("user_query") or ctx.get("query") or ""
    evidence = args.get("context_evidence") or ctx.get("evidence") or []
    analysts = args.get("analyst_outputs") or ctx.get("analyst_outputs") or {}
    # 待交付草稿（§33 汇总前移）：由编排层 summarize 节点先行产出，复审据此核验引用真实性
    draft = args.get("draft") or (ctx.get("result") or {}).get("answer", "")
    if not question.strip() or not evidence:
        # 权限/输入校验（§5.1.2）：输入校验非空（草稿允许为空——汇总可能降级为模板，不判失败）
        return _fail("复审输入为空，拒绝执行")
    note = reviewer_mod.review(question, evidence, analysts, draft=draft)
    if note is None:
        return _fail("复审不可用（跳过质检）")
    verdict = "通过" if note["passed"] else f"未通过（{len(note['defects'])} 项缺陷）"
    return _ok(f"复审{verdict}", note)


_DISPATCH = {
    "library_search": _library_search,
    "web_search": _web_search,
    "memory_search": _memory_search,
    "rewrite_query": _rewrite_query,
    "run_analysts": _run_analysts,
    "run_reviewer": _run_reviewer,
}


def _merge_struct(struct: dict, args: dict) -> dict:
    """会话检索式 + 工具入参 → 实际检索条件（工具显式入参优先，缺省沿用会话改写结果）"""
    merged = dict(struct)
    if args.get("keywords"):
        merged["keywords"] = [k for k in args["keywords"] if isinstance(k, str) and k.strip()]
    filters = dict(struct.get("filters") or {"year_from": "", "year_to": "", "sources": [], "cats": []})
    for key in ("year_from", "year_to"):
        if args.get(key):
            filters[key] = str(args[key])
    if args.get("sources"):
        filters["sources"] = [s for s in args["sources"] if s]
    merged["filters"] = filters
    return merged
