# QA 编排状态机：会话生命周期 + v4 主-子 Agent 编排调度
# 编排分档（对齐《多agent编排方案v4.md》§4 / 详细设计 §5.1）：
#   quick  快速通道：查询改写 → 检索 → 汇总（跳过 Planner / Reviewer，§4.3）
#   normal/deep 且 enable_planner：Planner 主-子闭环（§4.1）——
#                                normal 单轮调度 + 一次质检；deep 多轮迭代（≤ MAX_ITERATION）+ 强制复审
#   enable_planner=false 或 Planner 异常：回退 v3 固定流水线 route-retrieve-analyze-summarize（§5）
# 会话为内存态（文档未设会话表）；后台线程执行全流程，前端经 /steps /result 轮询
from typing import TypedDict

from langgraph.graph import END, StateGraph
from langgraph.checkpoint.sqlite import SqliteSaver
import json
import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from ai import analysts as analysts_mod
from ai import planner as planner_mod
from ai import qa_cache
from ai import retriever as retriever_mod
from ai import rewrite as rewrite_mod
from ai import router_agent, summarizer
from ai import tools as tools_mod
from ai import trace
from sqlalchemy import select

from app.config import PROJECT_ROOT
from app.models import Paper, Subscription, SubscriptionPaper
from app.session import SessionLocal

# 8 节点（key, label）——对齐详细设计 §3.5 / 前端设计文档 v2 §6.3
STEPS = [
    ("rewrite", "查询改写"),
    ("plan", "规划调度"),
    ("decompose", "拆解问题"),
    ("library", "检索文献库"),
    ("web", "检索网络"),
    ("memory", "引用历史"),
    ("review", "复审校验"),
    ("summarize", "汇总答案"),
]

# 综述会话步骤（独立 4 节点；前端综述仅依 finished 判定，不渲染步骤条）
REVIEW_STEPS = [
    ("route", "综述方向"),
    ("library", "取方向文献"),
    ("analyze", "多视角分析"),
    ("summarize", "综述生成"),
]

SESSION_TTL = timedelta(hours=2)   # 会话内存保留时长
MEMORY_LOOKBACK = 20               # 记忆检索回看的历史会话数
MAX_ITERATION = 3                  # 全局迭代上限（v4 §4.2 MAX_LOOP=3 / §5.1.3 MAX_ITERATION=3）

# 流水线并发闸门：同时最多 N 条问答/综述在跑，超出部分在后台线程里排队。
# 实测（2026-10-24，10 并发）：限到 5 只会把墙钟从 63s 拉到 180s，Reviewer 失败率不变，
# 说明本档并发下瓶颈不是流水线数量；闸门真正的作用是拦住"几十个请求同时派生
# 几十个 analyst 线程"的极端情况，故缺省放到 10，正常并发不受影响。
# 限流后 /ask 仍立即返回 202，排队中的会话步骤停在 pending，前端照常轮询。
QA_MAX_CONCURRENCY = 10


def _qa_max_concurrency() -> int:
    """流水线并发上限（缺省 10；LITRADAR_QA_MAX_CONCURRENCY 可覆盖）"""
    try:
        return max(1, int(os.getenv("LITRADAR_QA_MAX_CONCURRENCY", "10")))
    except ValueError:
        return QA_MAX_CONCURRENCY


_PIPELINE_SLOTS = threading.BoundedSemaphore(_qa_max_concurrency())

# 排队上限：超过这个时间还拿不到流水线槽位，就直接结束会话并提示「请求过多」。
# 15~30s 口径取 20s：正常一条流水线 40~150s，短于一条流水线时排队意义不大；
# 提示走既有 session["error"]，前端轮询到 finished + error 自动展示，接口仍是 202。
QA_QUEUE_TIMEOUT = 20
QA_BUSY_MESSAGE = "当前 API 请求过多，请稍后再试"


def _qa_queue_timeout() -> float:
    """排队上限（秒，缺省 20；LITRADAR_QA_QUEUE_TIMEOUT 可覆盖，非正数回落默认）"""
    try:
        value = float(os.getenv("LITRADAR_QA_QUEUE_TIMEOUT", str(QA_QUEUE_TIMEOUT)))
    except ValueError:
        return QA_QUEUE_TIMEOUT
    return value if value > 0 else QA_QUEUE_TIMEOUT


# quick 通道检索口径（§42/§43）：只查一轮，把证据条数放宽到 14、并放大向量路在 RRF 里的权重。
# 原因：deepseek 给出的关键词偏通用，词法路把期望文献排到 30~64 名；向量路能排到 5~10 名，
# 等权融合会被词法噪声压出前 8。仅作用于 quick，normal/deep 仍用 8 条 + 等权。
# §43 把权重从 5 收到 1.8、条数 12 放到 14：检索词补进 taxonomy / standard RAG 后词法路
# 自身也能排到前列，权重过高反而把词法路命中的期望文献（Q3 的 #58）压出去；Q3 的两篇期望
# 文献本来就一个靠词法、一个靠向量，1.8 是两路都能进前 14 的平衡点（离线八组词集全中）。
QUICK_LIBRARY_TOP_K = 14
QUICK_VECTOR_WEIGHT = 1.8


def _quick_vector_weight() -> float:
    """quick 向量路权重（缺省 1.8；LITRADAR_QUICK_VECTOR_WEIGHT 可覆盖，便于对照）"""
    try:
        return float(os.getenv("LITRADAR_QUICK_VECTOR_WEIGHT", "1.8"))
    except ValueError:
        return QUICK_VECTOR_WEIGHT


_REVIEW_NOTE_KEYS = ("passed", "defects", "suggestions")   # 前端冻结契约字段（对齐详细设计 §3.5）

# 会话最小索引：sid → {user_id, created_at, mode}；会话正文由 checkpointer 承载（Phase 3：落盘 SQLite）
_sessions: dict[str, dict] = {}
_lock = threading.Lock()

# 会话落盘位置（缺省相对项目根 data/qa_checkpoints.sqlite3；测试经 LITRADAR_QA_DB 覆盖为 :memory:）
_QA_DB_PATH = os.getenv("LITRADAR_QA_DB") or str(PROJECT_ROOT / "data" / "qa_checkpoints.sqlite3")
if _QA_DB_PATH != ":memory:":
    Path(_QA_DB_PATH).parent.mkdir(parents=True, exist_ok=True)
# QA 图与综述图共用同一 saver 实例（SqliteSaver 内部持 threading.Lock，支持跨 daemon 线程）
_PIPELINE_SAVER = SqliteSaver(sqlite3.connect(_QA_DB_PATH, check_same_thread=False))


def ask(user_id: int, query: str, mode: str = "phrase", rewrite: bool = True,
        enable_planner: bool = True) -> str:
    """发起一次问答：创建会话并启动后台流水线，立即返回 session_id

    :param enable_planner: 是否启用 v4 Planner 主-子编排（默认 true；false 强制 v3 固定流水线）
    """
    session_id = uuid.uuid4().hex[:16]
    session = {
        "session_id": session_id,
        "user_id": user_id,
        "query": query,
        "mode": mode,
        "rewrite": rewrite,
        "enable_planner": enable_planner,
        "complexity": "normal",
        "query_struct": None,
        "routes": [],
        "evidence": [],
        "analyst_outputs": {},
        "loop_counter": 0,          # 迭代轮次（steps 响应字段）
        "tool_calls": [],           # 工具调用记录（steps 响应字段）
        "review_notes": None,       # 复审意见（steps 响应字段）
        "review_need_analysts": [],      # 复审提出的多视角补做需求（内部，不入 steps 响应）
        "analyst_backfill_done": False,  # 补跑一次性标记（防重复）
        "planner_notes": [],        # 主 Agent 决策记录（result 响应字段）
        "iteration_count": 0,       # 迭代次数（result 响应字段）
        "planner_fallback": False,  # 是否已降级回退 v3
        "steps": [{"key": k, "label": lb, "status": "pending", "detail": "", "thinking": ""}
                  for k, lb in STEPS],
        "result": None,
        "finished": False,
        "error": None,
        "created_at": datetime.now(),
    }
    with _lock:
        _cleanup_expired()
        _sessions[session_id] = {"user_id": user_id,
                                 "created_at": session["created_at"],
                                 "mode": mode}
    # 相同用户 + 相同问句 + 相同形态：直接回放上次结果（不再派生流水线、不再调上游）
    cached = qa_cache.get(_cache_key(session))
    if cached:
        _apply_cached(session, cached)
        _seed_pipeline(session)
        print(f"[orchestrator] 会话 {session_id} 命中问答缓存，直接返回上次结果")
        return session_id
    _seed_pipeline(session)
    rec = trace.active()
    if rec:  # 评测/调试：记录会话发起参数
        rec.add("session", session_id, query=query, mode=mode, rewrite=rewrite,
                enable_planner=enable_planner)
    threading.Thread(target=_run_pipeline, args=(session,), daemon=True).start()
    return session_id


def _cache_key(session: dict) -> str:
    """会话 → 缓存键（用户 + 归一化问句 + 形态 + 改写/Planner 开关）"""
    return qa_cache.make_key(session["user_id"], session["query"], session["mode"],
                             session.get("rewrite", True),
                             session.get("enable_planner", True))


def _apply_cached(session: dict, snapshot: dict) -> None:
    """缓存命中：用上次终态填充会话字段（会话结构/响应契约保持不变）"""
    session["complexity"] = snapshot.get("complexity", "normal")
    session["query_struct"] = snapshot.get("query_struct")
    session["routes"] = snapshot.get("routes") or []
    session["steps"] = _cached_steps(snapshot.get("steps") or [])
    session["tool_calls"] = [dict(t) for t in (snapshot.get("tool_calls") or [])]
    session["review_notes"] = snapshot.get("review_notes")
    session["planner_notes"] = snapshot.get("planner_notes") or []
    session["iteration_count"] = snapshot.get("iteration_count", 0)
    session["result"] = snapshot.get("result")
    session["finished"] = True
    session["cache_hit"] = True


def _cached_steps(cached: list[dict]) -> list[dict]:
    """缓存命中的步骤条：沿用上次步骤与状态，detail 后缀标注「缓存命中」便于用户区分"""
    if not cached:
        return [{"key": k, "label": lb, "status": "done", "detail": "缓存命中", "thinking": ""}
                for k, lb in STEPS]
    out: list[dict] = []
    for step in cached:
        item = dict(step)
        item.setdefault("thinking", "")
        detail = (item.get("detail") or "").strip()
        item["detail"] = f"{detail}（缓存命中）" if detail else "缓存命中"
        out.append(item)
    return out


def _store_cache(session: dict) -> None:
    """正常完成才写缓存（报错/无答案不写，避免把失败结果回放给用户）"""
    if session.get("error") or not session.get("result") or session.get("mode") == "review":
        return
    snapshot = {"complexity": session.get("complexity", "normal"),
                "query_struct": session.get("query_struct"),
                "routes": session.get("routes") or [],
                "steps": [dict(st) for st in (session.get("steps") or [])],
                "tool_calls": [dict(t) for t in (session.get("tool_calls") or [])],
                "review_notes": session.get("review_notes"),
                "planner_notes": session.get("planner_notes") or [],
                "iteration_count": session.get("iteration_count", 0),
                "result": session.get("result")}
    qa_cache.put(_cache_key(session), session["user_id"], snapshot)


def ask_review(user_id: int, subscription_id: int) -> str:
    """发起方向多视角综述：复用会话机制（耗时分析异步化，前端轮询同 ask）"""
    session_id = uuid.uuid4().hex[:16]
    session = {
        "session_id": session_id,
        "user_id": user_id,
        "query": "",
        "mode": "review",
        "rewrite": False,
        "enable_planner": False,
        "complexity": "deep",
        "subscription_id": subscription_id,
        "evidence": [],
        "analyst_outputs": {},
        "loop_counter": 0,
        "tool_calls": [],
        "review_notes": None,
        "review_need_analysts": [],      # 复审提出的多视角补做需求（内部，不入 steps 响应）
        "analyst_backfill_done": False,  # 补跑一次性标记（防重复）
        "planner_notes": [],
        "iteration_count": 0,
        "steps": [{"key": k, "label": lb, "status": "pending", "detail": "", "thinking": ""}
                  for k, lb in REVIEW_STEPS],
        "result": None,
        "sections": None,
        "finished": False,
        "error": None,
        "created_at": datetime.now(),
    }
    with _lock:
        _cleanup_expired()
        _sessions[session_id] = {"user_id": user_id,
                                 "created_at": session["created_at"],
                                 "mode": "review"}
    _seed_review(session)
    threading.Thread(target=_run_review_pipeline, args=(session,), daemon=True).start()
    return session_id


def get_steps(session_id: str) -> dict | None:
    """轮询多步状态（详细设计 §3.5：8 节点 + 编排字段；正文读 checkpointer）"""
    with _lock:
        meta = _sessions.get(session_id)
    if not meta:
        return None
    s = _load_session(session_id, meta["mode"])
    if not s:
        return None
    return {
        "session_id": s["session_id"],
        "query": s["query"],
        "mode": s["mode"],
        "complexity": s.get("complexity", "normal"),
        "steps": [dict(st) for st in s["steps"]],
        "finished": s["finished"],
        "loop_counter": s.get("loop_counter", 0),
        "tool_calls": [dict(t) for t in (s.get("tool_calls") or [])],
        "review_notes": s.get("review_notes"),
    }


def get_result(session_id: str) -> dict | None:
    """拉取最终回答（含引用来源）；未完成时返回 finished=False；综述会话附 sections"""
    with _lock:
        meta = _sessions.get(session_id)
    if not meta:
        return None
    s = _load_session(session_id, meta["mode"])
    if not s:
        return None
    out = {
        "session_id": s["session_id"],
        "query": s["query"],
        "mode": s["mode"],
        "complexity": s.get("complexity", "normal"),
        "rewritten_query": (s.get("query_struct") or {}).get("query", ""),
        "rewritten": (s.get("query_struct") or {}).get("rewritten", False),
        "points": (s.get("result") or {}).get("points", []),
        "citations": (s.get("result") or {}).get("citations", []),
        "answer": (s.get("result") or {}).get("answer", ""),
        "planner_notes": s.get("planner_notes") or [],
        "iteration_count": s.get("iteration_count", 0),
        "finished": s["finished"],
        "error": s["error"],
    }
    if s.get("mode") == "review":
        out["subscription_id"] = s.get("subscription_id")
        out["direction_name"] = s.get("review_direction", "")
        out["paper_count"] = len(s.get("evidence") or [])
        out["sections"] = s.get("sections") or []
        out["outline"] = (s.get("result") or {}).get("outline", [])
    return out


def get_raw(session_id: str) -> dict | None:
    """评测/调试直读会话正文（不经 HTTP）；无索引或无线程 → None"""
    with _lock:
        meta = _sessions.get(session_id)
    if not meta:
        return None
    return _load_session(session_id, meta["mode"])


def _memory_records(user_id: int, exclude_session: str) -> list[dict]:
    """记忆检索数据源：该用户近期已完成会话的问答摘要（索引取候选 + checkpointer 读正文）"""
    with _lock:
        cand = [(sid, m) for sid, m in _sessions.items()
                if m["user_id"] == user_id and sid != exclude_session]
    cand.sort(key=lambda x: x[1]["created_at"], reverse=True)
    out: list[dict] = []
    for sid, m in cand:
        s = _load_session(sid, m["mode"])
        if not s or not s.get("finished") or not s.get("result"):
            continue
        out.append({
            "session_id": sid,
            "query": s["query"],
            "answer": s["result"].get("answer", ""),
            "points": s["result"].get("points", []),
        })
        if len(out) >= MEMORY_LOOKBACK:
            break
    return out


def _cleanup_expired() -> None:
    """TTL 过期清理（ask 时顺带执行，锁内调用）：移除索引 + 删除线程"""
    now = datetime.now()
    dead = [sid for sid, m in _sessions.items() if now - m["created_at"] > SESSION_TTL]
    for sid in dead:
        m = _sessions.pop(sid, None)
        graph = _REVIEW_GRAPH if m["mode"] == "review" else _PIPELINE_GRAPH
        try:
            graph.checkpointer.delete_thread(_config(sid, m["mode"])["configurable"]["thread_id"])
        except Exception:
            pass


# ---------- 存储辅助（Phase 2：checkpointer 承载会话正文） ----------

def _config(session_id: str, mode: str) -> dict:
    """线程配置：综述与 QA 命名空间隔离"""
    prefix = "rev" if mode == "review" else "qa"
    return {"configurable": {"thread_id": f"{prefix}:{session_id}"}}


def _safe_update(graph, cfg: dict, values: dict) -> None:
    """尽力落盘（checkpointer）；失败不阻断流水线"""
    try:
        graph.update_state(cfg, values)
    except Exception as e:
        print(f"[orchestrator] checkpoint 写入失败（{(cfg.get('configurable') or {}).get('thread_id', '?')}）: {e}")


def _seed_pipeline(session: dict) -> None:
    """启动前同步 seed QA 初始状态，消除首次轮询竞态"""
    _safe_update(_PIPELINE_GRAPH, _config(session["session_id"], "qa"),
                 {"session": session, "round_no": 1, "max_rounds": 0,
                  "plan": None, "note": None, "outcome": ""})


def _seed_review(session: dict) -> None:
    """启动前同步 seed 综述初始状态"""
    _safe_update(_REVIEW_GRAPH, _config(session["session_id"], "review"),
                 {"session": session})


def _load_session(session_id: str, mode: str) -> dict | None:
    """按索引从 checkpointer 取会话正文"""
    graph = _REVIEW_GRAPH if mode == "review" else _PIPELINE_GRAPH
    snap = graph.get_state(_config(session_id, mode))
    return snap.values.get("session") if snap and snap.values else None


def _rebuild_index() -> None:
    """启动时从 checkpointer 重建最小索引（进程重启后旧会话仍可被 get_steps 命中）

    - `saver.list(None)` 全局按 checkpoint_id 新→旧；同 thread 内 id 单调 ⇒ 首次出现即最新
    - 首帧（source=input）的 channel_values 不含 session，故须「每 thread 取首个含 session 的帧」
    - mode 以会话正文 session["mode"] 为准（QA 为 phrase/paragraph/strategy），thread 前缀仅兜底
    """
    seen: set[str] = set()
    for t in _PIPELINE_SAVER.list(None):
        cfg = t.config.get("configurable") or {}
        tid = cfg.get("thread_id") or ""
        if not tid or tid in seen:
            continue
        seen.add(tid)
        prefix, _, sid = tid.partition(":")
        sess = (t.checkpoint.get("channel_values") or {}).get("session")
        if not sid or not isinstance(sess, dict):
            continue
        with _lock:
            _sessions[sid] = {
                "user_id": sess.get("user_id"),
                "created_at": sess.get("created_at") or datetime.now(),
                "mode": sess.get("mode") or ("review" if prefix == "rev" else "phrase"),
            }


# ---------- 流水线（后台线程执行；每步异常落 step=failed，整体不中断） ----------

def _run_pipeline(session: dict) -> None:
    """QA 流水线（后台线程）：等并发槽位 → 启动统一 LangGraph 图并落盘终态

    槽位最多等 QA_QUEUE_TIMEOUT 秒；等不到直接结束会话并提示「请求过多」，
    避免请求越堆越多把上游和线程都拖死（接口仍是 202 立即返回，提示靠轮询拿到）。
    """
    cfg = _config(session["session_id"], "qa")
    if not _PIPELINE_SLOTS.acquire(timeout=_qa_queue_timeout()):
        session["error"] = QA_BUSY_MESSAGE
        session["finished"] = True
        _safe_update(_PIPELINE_GRAPH, cfg, {"session": session})
        print(f"[orchestrator] 会话 {session['session_id']} 排队超过 "
              f"{_qa_queue_timeout():.0f}s，已提示稍后再试")
        return
    try:
        _PIPELINE_GRAPH.invoke(
            {"session": session, "round_no": 1, "max_rounds": 0,
             "plan": None, "note": None, "outcome": ""}, cfg)
    except Exception as e:
        session["error"] = f"流水线异常: {e}"
        print(f"[orchestrator] 会话 {session['session_id']} 异常: {e}")
    finally:
        _PIPELINE_SLOTS.release()
        session["finished"] = True
        _safe_update(_PIPELINE_GRAPH, cfg, {"session": session})
        _store_cache(session)   # 正常完成的结果进缓存；失败/空答案自动跳过


# ---------- Planner 闭环子过程 ----------

def _apply_plan(session: dict, plan: dict, round_no: int, max_rounds: int) -> None:
    """落盘一轮规划：迭代轮次 + 规划/拆解节点 + 决策记录"""
    session["loop_counter"] = round_no
    thought = plan['thought'] or '（无说明）'
    _mark(session, "plan", "done", f"第 {round_no}/{max_rounds} 轮｜{thought}")
    _think(session, "plan", f"第 {round_no} 轮：{thought}")
    subtasks = plan.get("subtasks") or []
    decompose = "；".join(subtasks) if subtasks else "单步任务，无需拆解"
    _mark(session, "decompose", "done", decompose)
    _think(session, "decompose", decompose)
    session["planner_notes"].append(f"规划（第 {round_no} 轮）：{plan['thought']}")


def _execute_tool_calls(session: dict, calls: list[dict]) -> None:
    """执行本轮工具调用并回填上下文（权限校验在工具层完成，对齐 §5.1.2）"""
    for call in calls or []:
        name, args = call["tool"], call.get("args") or {}
        t0 = time.perf_counter()
        res = tools_mod.execute(name, args, session)
        session["tool_calls"].append(
            {"tool": name, "args": args, "result_brief": res.get("brief", "")})
        node = {"rewrite_query": "rewrite", "library_search": "library",
                "web_search": "web", "memory_search": "memory",
                "run_analysts": "analyze", "run_reviewer": "review"}.get(name)
        if node:
            _think(session, node, f"调用 {name}：{json.dumps(args, ensure_ascii=False, default=str)}")
            if not res.get("ok"):
                _think(session, node, f"失败：{res.get('brief', '')}")
        rec = trace.active()
        if rec:  # 评测/调试：记录工具调用（名/入参/成败/摘要 + 耗时）
            rec.tool(name, args, bool(res.get("ok")), res.get("brief", ""),
                     duration_ms=int((time.perf_counter() - t0) * 1000),
                     session_id=session.get("session_id", ""))
        if res.get("ok"):
            _apply_tool_result(session, name, res.get("data"))


def _apply_tool_result(session: dict, name: str, data) -> None:
    """工具结果回填：检索证据 / 检索式 / 分析师输出"""
    if name in ("library_search", "web_search", "memory_search"):
        node = {"library_search": "library", "web_search": "web", "memory_search": "memory"}[name]
        merged = retriever_mod.merge_evidence(session.get("evidence") or [], data or [])
        session["evidence"] = merged
        _mark(session, node, "done", f"累计证据 {len(merged)} 条")
        titles = "；".join((d.get("title") or "")[:80] for d in (data or [])[:6])
        _think(session, node, f"累计证据 {len(merged)} 条" + (f"\n命中：{titles}" if titles else ""))
    elif name == "rewrite_query" and isinstance(data, dict):
        session["query_struct"] = data
    elif name == "run_analysts" and isinstance(data, dict):
        merged = dict(session.get("analyst_outputs") or {})
        merged.update({k: v for k, v in data.items() if v})
        session["analyst_outputs"] = merged
        for perspective, text in data.items():
            if text:
                _think(session, "analyze", f"【{perspective}】\n{text}")


def _do_review(session: dict) -> None:
    """复审质检节点：异常 → 跳过质检并标注「未经过完整复审质检」（v4 §5 / §8.1）"""
    res = tools_mod.execute("run_reviewer", {}, session)
    if res.get("ok"):
        data = res.get("data") or {}
        session["review_need_analysts"] = list(data.get("need_analysts") or [])  # 内部通道，不入响应
        session["review_notes"] = {k: data.get(k) for k in _REVIEW_NOTE_KEYS}
        note = session["review_notes"]
        detail = "通过" if note["passed"] else f"未通过（{len(note['defects'])} 项缺陷）"
        _mark(session, "review", "done", f"复审{detail}")
        review_think = []
        if note["defects"]:
            review_think.append("缺陷：" + "；".join(note["defects"]))
        if note["suggestions"]:
            review_think.append("建议：" + "；".join(note["suggestions"]))
        if not review_think:
            review_think.append("草稿满足当前质检要求，未发现需要修改的问题")
        _think(session, "review", "\n".join(review_think))
    else:
        session["review_notes"] = None
        session["review_need_analysts"] = []
        session["review_skipped"] = True
        _mark(session, "review", "failed", "复审异常，跳过质检（未经过完整复审质检）")


def _maybe_backfill_analysts(session: dict) -> bool:
    """复审要求补做多视角 → 编排层确定性补跑一次（不经 Planner，单会话至多一次）

    触发需同时满足：有需求 / 未补跑过 / 证据 ≥ 2 篇 / 需求视角中至少一个当前为空。
    """
    need = [k for k in (session.get("review_need_analysts") or []) if k]
    if not need or session.get("analyst_backfill_done"):
        return False
    if len(session.get("evidence") or []) < 2:
        session["planner_notes"].append("复审要求补做多视角分析，但证据不足 2 篇，未补做")
        return False
    have = session.get("analyst_outputs") or {}
    missing = [k for k in need if not str(have.get(k) or "").strip()]
    if not missing:
        return False
    session["analyst_backfill_done"] = True          # 置位先于执行，重复触发免疫
    _execute_tool_calls(session, [{"tool": "run_analysts",
                                   "args": {"enable_list": missing}}])
    session["planner_notes"].append(
        f"复审要求补做多视角分析（{'、'.join(missing)}），编排层已确定性补跑")
    return True


def _run_retrieval_by_routes(session: dict, top_k: int | None = None,
                             vector_weight: float = 1.0) -> None:
    """按路由结果执行三路检索节点（quick / v3 回退共用；v4 由工具调用驱动）

    :param top_k: 库内证据条数；None 用 retriever 缺省。quick 传 12，其余路径不变
    :param vector_weight: 库内 RRF 里向量路的权重；缺省 1.0 即等权
    """
    struct, routes = session["query_struct"], session.get("routes") or []
    tk = top_k or retriever_mod.LIBRARY_TOP_K
    lists: list[list[dict]] = []
    sources = (
        ("library", "library", lambda: retriever_mod.search_library(
            struct, top_k=tk, vector_weight=vector_weight)),
        ("web", "web", lambda: retriever_mod.search_web(struct)),
        ("memory", "memory", lambda: retriever_mod.search_memory(
            _memory_records(session["user_id"], session["session_id"]),
            session["query"], struct.get("keywords"))),
    )
    for node, route, fn in sources:
        if route not in routes:
            _mark(session, node, "skipped", "本次未调度")
            continue
        try:
            ev = fn()
            lists.append(ev)
            _mark(session, node, "done", f"命中 {len(ev)} 条")
        except Exception as e:
            _mark(session, node, "failed", str(e))
    evidence = retriever_mod.merge_evidence(*lists)
    if not evidence:
        # 零证据兜底：强制补一轮库内检索（对齐现状）
        evidence = retriever_mod.search_library(struct, top_k=tk,
                                                vector_weight=vector_weight)
        _mark(session, "library", "done", f"兜底补检命中 {len(evidence)} 条")
    session["evidence"] = evidence


def _flush_retrieval_nodes(session: dict) -> None:
    """闭环结束：未被调度的检索节点置为 skipped（保持步骤条语义完整）"""
    for node in ("library", "web", "memory"):
        st = next(s for s in session["steps"] if s["key"] == node)
        if st["status"] == "pending":
            st["status"], st["detail"] = "skipped", "本轮未调度"


# ---------- 步骤执行器 ----------

def _step(session: dict, key: str, fn, prefix: str = "") -> None:
    """单步执行：置 running → fn → done；异常置 failed 并向上抛"""
    st = next(s for s in session["steps"] if s["key"] == key)
    st["status"], st["detail"] = "running", ""
    _trace_step(session, key, "running", "")
    try:
        detail = fn(session) or ""
        st["status"] = "done"
        st["detail"] = f"{prefix}｜{detail}" if prefix else str(detail)
        _trace_step(session, key, "done", st["detail"])
    except Exception as e:
        st["status"], st["detail"] = "failed", str(e)
        _trace_step(session, key, "failed", str(e))
        raise


def _mark(session: dict, key: str, status: str, detail: str) -> None:
    """直接置步骤状态（skipped / done 等非执行态）"""
    st = next(s for s in session["steps"] if s["key"] == key)
    st["status"], st["detail"] = status, detail
    _trace_step(session, key, status, detail)


def _trace_step(session: dict, key: str, status: str, detail: str) -> None:
    """评测/调试：记录步骤状态流转时间戳（追踪器未启用时为空操作）"""
    rec = trace.active()
    if rec:
        rec.step(key, status, detail, session_id=session.get("session_id", ""))


def _think(session: dict, key: str, text) -> None:
    """追加步骤的模型思考/输出文本（前端按步骤折叠展示，缺省不影响编排）"""
    if text is None:
        return
    clean = str(text).strip()
    if not clean:
        return
    st = next((s for s in session.get("steps") or [] if s["key"] == key), None)
    if st is None:
        return
    old = (st.get("thinking") or "").strip()
    st["thinking"] = f"{old}\n{clean}" if old else clean


# ---------- 步骤实现 ----------

def _step_rewrite(session: dict) -> str:
    """查询改写 + 检索路由 + 复杂度分级（三档共用入口）"""
    query, mode = session["query"], session["mode"]
    # 查询改写（strategy 跳过；phrase 不走 LLM；paragraph 走 LLM 失败回退）
    query_struct = rewrite_mod.rewrite_query(query, mode=mode, enabled=session["rewrite"])
    # 检索路由 + 复杂度分级（规则优先 → LLM 兜底）
    decision = router_agent.route(query, query_struct.get("keywords"))
    session["query_struct"] = query_struct
    session["routes"] = decision["routes"]
    session["complexity"] = decision.get("complexity") or "normal"
    cx_label = {"quick": "快速通道", "normal": "标准编排", "deep": "深度编排"}[session["complexity"]]
    routes_txt = "+".join(decision["routes"])
    rew = "已改写" if query_struct["rewritten"] else "原始问句"
    keywords = "、".join(query_struct.get("keywords") or [])
    _think(session, "rewrite",
           f"判定：{decision['reason']}\n路由：{routes_txt}\n状态：{rew}"
           + (f"\n改写后检索式：{query_struct.get('query') or ''}" if query_struct.get("query") else "")
           + (f"\n关键词：{keywords}" if keywords else ""))
    return f"{cx_label}｜{routes_txt}｜{rew}｜{decision['reason']}"


def _analyze(session: dict) -> str:
    """多视角分析（v3 回退 / 综述共用；v4 经 run_analysts 工具触发）"""
    evidence = session.get("evidence") or []
    if len(evidence) < 2:
        session["analyst_outputs"] = {}
        return "证据不足 2 篇，跳过多视角分析"
    outputs = analysts_mod.run_analysts(evidence)
    session["analyst_outputs"] = outputs
    for perspective, text in outputs.items():
        if text:
            _think(session, "analyze", f"【{perspective}】\n{text}")
    ok = [k for k, v in outputs.items() if v]
    return f"完成视角: {', '.join(ok) if ok else '全部降级跳过'}"


def _step_summarize(session: dict) -> str:
    """汇总草稿：Summarizer 生成待交付回答（⚠ 标注由 _apply_alerts 单独承担，见 §33 汇总前移）"""
    result = summarizer.synthesize(
        session["query"], session.get("evidence") or [],
        session.get("analyst_outputs") or {})
    session["result"] = result
    _think(session, "summarize", result.get("answer") or "")
    return f"要点 {len(result['points'])} 条，引用 {len(result['citations'])} 处"


def _apply_alerts(session: dict) -> None:
    """质检 / 迭代完整性告警：前置标注到已生成的回答（v4 §5.1.3）

    拆出为独立函数的原因（§33）：汇总前移后，⚠ 标注仍须在复审结论落定**之后**执行，
    故改由 finalize（planner 路径）/ quick / fallback（节点内收尾）在草稿产出后统一调用。
    """
    result = session.get("result")
    if not result:          # 无草稿（如汇总未跑）：无可标注对象
        return
    alerts = []
    if session.get("review_skipped"):
        alerts.append("本次回答未经完整质检，仅供参考")
    if session.get("iteration_limit"):
        alerts.append("分析已达到轮次上限，结果可能不完整，建议核对")
    if alerts:
        result["answer"] = "\n".join(f"> ⚠ 【{a}】" for a in alerts) + "\n\n" + result["answer"]


# ---------- 编排辅助 ----------

def _evidence_summary(session: dict) -> str:
    """确定性证据概况（管道用代码）：各通道命中数 + top 标题，供 Planner 决策"""
    evidence = session.get("evidence") or []
    if not evidence:
        return "零命中"
    by_origin: dict[str, int] = {}
    for ev in evidence:
        by_origin[ev.get("origin", "?")] = by_origin.get(ev.get("origin", "?"), 0) + 1
    parts = " + ".join(f"{k}:{v}" for k, v in by_origin.items())
    titles = "；".join((ev.get("title") or "")[:40] for ev in evidence[:5])
    return f"共 {len(evidence)} 条（{parts}）top: {titles}"


def _suggestion_text(note: dict | None) -> str:
    """复审意见 → 给 Planner 的改进指令（缺陷 + 建议合并）"""
    if not note:
        return ""
    parts = list(note.get("defects") or []) + list(note.get("suggestions") or [])
    return "；".join(parts)[:300]


# ---------- QA 流水线图（LangGraph · 会话正文落 checkpointer） ----------
# 顶层 8 节点（rewrite/quick/plan/execute_tools/summarize/review/finalize/fallback）+ 3 条条件边
# 汇总前移（§33）：execute_tools → summarize → review，复审的判定对象改为待交付草稿
# 状态单通道：session dict 承载会话正文；节点 return {"session": session} 整体落盘
# 引用语义：实测 langgraph 1.1.9 的 invoke 不对入参 state 做 deepcopy——
#           节点内拿到的 session 即传入 invoke 的同一对象；节点显式 return {"session": session} 锁引用

class QAGraphState(TypedDict):
    """闭环图状态：session=会话 dict 引用；round_no/max_rounds=轮次控制；outcome=路由判据"""
    session: dict
    round_no: int
    max_rounds: int
    plan: dict | None
    note: dict | None
    outcome: str


def _node_plan(state: QAGraphState) -> dict:
    """规划节点：首轮调 Planner 并落盘；retry 轮先自增 loop_counter 再带复审建议重规划"""
    session = state["session"]
    round_no = state["round_no"]
    if state["outcome"] == "retry":
        # retry 轮：先自增 loop_counter（等价性清单第 4 条）再调 Planner
        session["loop_counter"] = round_no
        plan = planner_mod.plan(session["query"], round_no,
                                _evidence_summary(session), _suggestion_text(state["note"]))
        if plan is None:
            session["planner_notes"].append(f"第 {round_no} 轮规划失败，基于现有证据产出")
            return {"session": session, "plan": None, "outcome": "plan_degraded"}
        _apply_plan(session, plan, round_no, state["max_rounds"])
        return {"session": session, "plan": plan, "outcome": "planned"}

    # 首轮
    plan = planner_mod.plan(session["query"], 1, _evidence_summary(session), "")
    if plan is None:
        session["planner_notes"].append(
            "Planner 不可用（输出解析失败 / 无 function-call 模型），回退 v3 固定流水线")
        _mark(session, "plan", "failed", "Planner 不可用，回退 v3 固定流水线")
        return {"session": session, "plan": None, "outcome": "planner_unavailable"}
    _apply_plan(session, plan, 1, state["max_rounds"])
    return {"session": session, "plan": plan, "outcome": "planned"}


def _node_execute_tools(state: QAGraphState) -> dict:
    """工具执行节点：按本轮规划执行工具调用并回填上下文（权限校验在工具层）"""
    session = state["session"]
    _execute_tool_calls(session, (state["plan"] or {}).get("tool_calls"))
    return {"session": session}


def _node_summarize(state: QAGraphState) -> dict:
    """汇总节点：本轮先出待交付草稿，交复审校验（§33 汇总前移）"""
    session = state["session"]
    _step(session, "summarize", _step_summarize)
    return {"session": session}


def _node_review(state: QAGraphState) -> dict:
    """复审节点：零证据短路 → 复审质检 → 判定 通过 / 达上限 / retry"""
    session = state["session"]
    round_no = state["round_no"]
    if not session.get("evidence"):
        # 所有检索工具无结果 → 证据不足提示并结束（v4 §5）
        session["planner_notes"].append("所有检索工具无结果，基于现有证据结束（证据不足）")
        session["review_skipped"] = True
        _mark(session, "review", "skipped", "无检索证据，跳过复审")
        return {"session": session, "note": None, "outcome": "no_evidence"}

    _do_review(session)
    backfilled = _maybe_backfill_analysts(session)   # 复审要求多视角 → 确定性补跑（至多一次）
    note = session.get("review_notes")
    passed = bool(note and note.get("passed"))
    reached_limit = round_no >= state["max_rounds"]
    if backfilled and (passed or reached_limit):
        # 本轮回合收尾（不再有下一轮汇总）：补跑出的分析师内容需重出草稿，避免末轮草稿缺新视角
        _step(session, "summarize", _step_summarize)
    if passed:
        return {"session": session, "note": note, "outcome": "passed"}
    if reached_limit:
        session["iteration_limit"] = True
        session["planner_notes"].append(
            f"达到迭代上限（{state['max_rounds']} 轮），强制结束")
        return {"session": session, "note": note, "outcome": "iteration_limit"}
    session["loop_counter"] = round_no + 1
    return {"session": session, "note": note, "round_no": round_no + 1,
            "outcome": "retry"}


def _node_finalize(state: QAGraphState) -> dict:
    """收尾节点：落迭代次数 → 补齐检索节点状态 → 标注质检/迭代告警（不再汇总，§33）"""
    session = state["session"]
    session["iteration_count"] = state["round_no"]
    _flush_retrieval_nodes(session)
    _apply_alerts(session)
    return {"session": session}


def _node_rewrite(state: QAGraphState) -> dict:
    """① 查询改写 + 路由 + 复杂度分级；回填 max_rounds（等价原 L248）"""
    session = state["session"]
    _step(session, "rewrite", _step_rewrite)
    # normal 档：首轮 + 一次复审回炉（仅复审未通过时才用上）；deep 档：最多 MAX_ITERATION 轮
    max_rounds = 2 if session.get("complexity") == "normal" else MAX_ITERATION
    return {"session": session, "max_rounds": max_rounds}


def _apply_quick_llm_keywords(session: dict) -> None:
    """quick 通道关键词兜底：正则 bigram → 一次 LLM 英文改写（§35）

    原 quick 只用正则抽词，中文问句切出的碎片词会撞上 MySQL 全文检索 50% 阈值而在词法路零命中。
    此处仅在「开关开启且本轮未经 LLM 改写」时把关键词来源换成 LLM 英文术语；成功只替换 keywords，
    失败/关闭则保持正则关键词，不阻断检索。归因文案追加到 rewrite 步骤 detail 尾部（不覆盖原值）。
    """
    struct = session.get("query_struct") or {}
    if struct.get("rewritten"):                 # 本轮已经过 LLM 改写 → 不重复
        return
    if not rewrite_mod._quick_llm_enabled():    # 开关关闭 → 保持正则关键词
        return
    base = next(s for s in session["steps"] if s["key"] == "rewrite")["detail"]
    out = rewrite_mod.rewrite_for_quick(session["query"])
    if out and out.get("keywords"):
        struct = {**struct, "keywords": list(out["keywords"])}
        if out.get("alt_keywords"):             # §39 续：施事名词派生词集 → 检索层做第二组召回
            struct["alt_keywords"] = list(out["alt_keywords"])
        session["query_struct"] = struct
        detail = f"{base}｜LLM英文兜底关键词 {len(out['keywords'])} 个"
        if struct.get("alt_keywords"):
            detail += f"｜派生词集 {len(struct['alt_keywords'])} 个"
        _mark(session, "rewrite", "done", detail)
    else:                                       # 改写失败 → 保留正则关键词
        _mark(session, "rewrite", "done", f"{base}｜LLM英文兜底失败，保留正则关键词")


def _node_quick(state: QAGraphState) -> dict:
    """快速通道（原 _run_quick 函数体，逐字搬运）"""
    session = state["session"]
    _mark(session, "plan", "skipped", "快速通道跳过规划调度")
    _mark(session, "decompose", "skipped", "快速通道跳过规划调度")
    _apply_quick_llm_keywords(session)          # §35：关键词来源改为一次 LLM 英文改写
    # §42：quick 只查一轮，放宽证据条数并放大向量路权重
    _run_retrieval_by_routes(session, top_k=QUICK_LIBRARY_TOP_K,
                             vector_weight=_quick_vector_weight())
    _mark(session, "review", "skipped", "快速通道跳过复审校验")
    _step(session, "summarize", _step_summarize)
    _apply_alerts(session)          # 两标志均未置位 → 不产生标注（与改前等价）
    return {"session": session}


def _node_fallback(state: QAGraphState) -> dict:
    """v3 回退（原 _run_fallback 函数体 + planner_fallback 置位，逐字搬运）"""
    session = state["session"]
    session["planner_fallback"] = True
    _mark(session, "plan", "skipped", "回退 v3 固定流水线（未启用 / 不可用 Planner）")
    _mark(session, "decompose", "skipped", "回退 v3 固定流水线")
    _run_retrieval_by_routes(session)
    analyze_detail = _analyze(session)
    session["review_skipped"] = True
    _mark(session, "review", "skipped", "未经过完整复审质检")
    _step(session, "summarize", _step_summarize, prefix=analyze_detail)
    _apply_alerts(session)          # review_skipped 已置位 → 保留既有 ⚠ 标注（与改前等价）
    return {"session": session}


def _route_after_rewrite(state: QAGraphState) -> str:
    """rewrite 出边：quick → quick；启用 Planner → plan；否则 → fallback"""
    s = state["session"]
    if s.get("complexity") == "quick":
        return "quick"
    return "plan" if s.get("enable_planner") else "fallback"


def _route_after_plan(state: QAGraphState) -> str:
    """plan 出边：首轮不可用 → fallback；第 N 轮规划失败 → summarize（仍须出草稿）；否则 → execute_tools"""
    return {"planner_unavailable": "fallback", "plan_degraded": "summarize"}.get(
        state["outcome"], "execute_tools")


def _route_after_review(state: QAGraphState) -> str:
    """review 出边路由：retry → plan；否则（无证据/通过/达上限）→ finalize"""
    return "plan" if state["outcome"] == "retry" else "finalize"


def _build_pipeline_graph():
    """装配并编译统一 QA 流水线图（挂 InMemorySaver）"""
    g = StateGraph(QAGraphState)
    g.add_node("rewrite", _node_rewrite)
    g.add_node("quick", _node_quick)
    g.add_node("plan", _node_plan)
    g.add_node("execute_tools", _node_execute_tools)
    g.add_node("summarize", _node_summarize)
    g.add_node("review", _node_review)
    g.add_node("finalize", _node_finalize)
    g.add_node("fallback", _node_fallback)
    g.set_entry_point("rewrite")
    g.add_conditional_edges("rewrite", _route_after_rewrite,
                            {"quick": "quick", "plan": "plan", "fallback": "fallback"})
    g.add_conditional_edges("plan", _route_after_plan,
                            {"fallback": "fallback", "summarize": "summarize",
                             "execute_tools": "execute_tools"})
    g.add_edge("execute_tools", "summarize")     # 汇总前移（§33）：先出草稿再交复审
    g.add_edge("summarize", "review")
    g.add_conditional_edges("review", _route_after_review,
                            {"plan": "plan", "finalize": "finalize"})
    g.add_edge("quick", END)
    g.add_edge("fallback", END)
    g.add_edge("finalize", END)
    return g.compile(checkpointer=_PIPELINE_SAVER)


_PIPELINE_GRAPH = _build_pipeline_graph()


# ---------- 综述流水线 ----------

# 综述输入文献上限（token 边界控制）
REVIEW_MAX_PAPERS = 20


class ReviewGraphState(TypedDict):
    """综述图状态：session=会话 dict"""
    session: dict


def _node_review_route(state: ReviewGraphState) -> dict:
    _step(state["session"], "route", lambda s: "综述模式（方向文献集）")
    return {"session": state["session"]}


def _node_review_library(state: ReviewGraphState) -> dict:
    _step(state["session"], "library", _step_review_retrieve)
    return {"session": state["session"]}


def _node_review_analyze(state: ReviewGraphState) -> dict:
    _step(state["session"], "analyze", _analyze)
    return {"session": state["session"]}


def _node_review_summarize(state: ReviewGraphState) -> dict:
    _step(state["session"], "summarize", _step_review_generate)
    return {"session": state["session"]}


def _build_review_graph():
    g = StateGraph(ReviewGraphState)
    g.add_node("route", _node_review_route)
    g.add_node("library", _node_review_library)
    g.add_node("analyze", _node_review_analyze)
    g.add_node("summarize", _node_review_summarize)
    g.set_entry_point("route")
    g.add_edge("route", "library")
    g.add_edge("library", "analyze")
    g.add_edge("analyze", "summarize")
    g.add_edge("summarize", END)
    return g.compile(checkpointer=_PIPELINE_SAVER)


_REVIEW_GRAPH = _build_review_graph()


def _run_review_pipeline(session: dict) -> None:
    """综述流水线（后台线程）：等并发槽位 → 启动综述图并落盘终态"""
    cfg = _config(session["session_id"], "review")
    with _PIPELINE_SLOTS:
        try:
            _REVIEW_GRAPH.invoke({"session": session}, cfg)
        except Exception as e:
            session["error"] = f"综述流水线异常: {e}"
            print(f"[orchestrator] 综述会话 {session['session_id']} 异常: {e}")
        finally:
            session["finished"] = True
            _safe_update(_REVIEW_GRAPH, cfg, {"session": session})


def _step_review_retrieve(session: dict) -> str:
    """取该方向 top 文献（按关联打分）作为综述证据集"""
    t = Paper.__table__   # 列形态 select：mappings() 键即列名，兼容下方 p["..."] / p.get() 读法
    with SessionLocal() as db_session:   # 注意：本文件的 session 是 QA 会话 dict，不能占用该名字
        sub = db_session.execute(
            select(Subscription.__table__).where(
                Subscription.id == session["subscription_id"],
                Subscription.user_id == session["user_id"])).mappings().first()
        if not sub:
            raise RuntimeError("订阅不存在")
        papers = db_session.execute(
            select(t)
            .join(SubscriptionPaper, SubscriptionPaper.paper_id == t.c.id)
            .where(SubscriptionPaper.subscription_id == session["subscription_id"])
            .order_by(SubscriptionPaper.score.desc())
            .limit(REVIEW_MAX_PAPERS)
        ).mappings().all()
    evidence = [{
        "id": str(p["id"]), "title": p["title"], "abstract": p.get("abstract") or "",
        "journal": p.get("journal") or "", "source": p.get("source") or "",
        "url": p.get("url") or "", "published_at": str(p.get("published_at") or ""),
        "authors": [], "origin": "library",
    } for p in papers]
    if not evidence:
        raise RuntimeError("该方向暂无入库文献，无法综述")
    session["evidence"] = evidence
    session["review_direction"] = sub["name"]
    return f"方向「{sub['name']}」文献 {len(evidence)} 篇"


def _step_review_generate(session: dict) -> str:
    """综述生成：三视角结果组装 sections + summarizer 汇总正文，并产出可编辑大纲（F-REV-04）"""
    outputs = session.get("analyst_outputs") or {}
    evidence = session.get("evidence") or []
    question = f"研究方向「{session.get('review_direction', '')}」的文献综述"
    result = summarizer.synthesize(question, evidence, outputs)
    result["outline"] = summarizer.build_outline(question, evidence, outputs)
    session["result"] = result
    labels = {"method": "方法抽取", "critic": "批判审阅", "compare": "横向对比"}
    session["sections"] = [{"title": labels[k], "content": outputs[k]}
                           for k in ("method", "critic", "compare") if outputs.get(k)]
    ok = [labels[k] for k, v in outputs.items() if v]
    return f"视角 {len(ok)}/3，引用 {len(result['citations'])} 处，大纲 {len(result['outline'])} 章"


_rebuild_index()   # 启动时重建索引（Phase 3：进程重启后旧会话仍可读）
