# LangGraph 会话存储迁移（Phase 2）· 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 QA 编排的会话状态从进程内 `_sessions` dict 迁移到 LangGraph **checkpointer**（`InMemorySaver`），`get_steps`/`get_result` 改读 `graph.get_state()`，**前端响应契约逐字段冻结、四路径行为等价**。

**Architecture:** 三条 QA 路径（quick / planner / fallback）+ 综述流水线全部图化；planner 节点**内联**进统一顶层图（保逐节点 checkpoint 粒度）；`_sessions` 降级为最小索引（`user_id/created_at/mode`）；删除 `session["_memory_records"]` 闭包，工具层惰性取记录。

**Tech Stack:** Python 3.11 / `langgraph>=1.1.9`（`InMemorySaver` 自带）/ pytest / 现有 `ai/*` 编排栈（**不引入 `ChatOpenAI`**）

**依据文档：** [LangGraph会话存储迁移设计.md](file:///d:/实习/my_project/deep_search_pro-master/doc/变更文档/LangGraph会话存储迁移设计.md)（Phase 2 spec v1.0，用户已确认含 3 处偏离）

---

## Global Constraints

以下为 spec 的全局要求，**每个任务的隐含前提**，精确值逐字取自 spec：

- **迁移范围**：QA 四路径 + 综述全部入图；**不改前端**（`app/routers/qa.py` / `app/schemas/qa.py` 一行不改）；响应 JSON schema 逐字冻结（见下文「契约冻结清单」）。
- **checkpointer = `InMemorySaver`**，QA 图与综述图**共用同一 saver 实例**；`thread_id = f"qa:{sid}"` / `f"rev:{sid}"`。
- **state 单通道**（D6）：`{session: dict, round_no, max_rounds, plan, note, outcome}`，节点 `return {"session": session}` 整体落盘。
- **planner 节点内联**（D5），**不用子图**；`_QA_GRAPH` / `_run_planner_loop` 退役。
- **`_sessions` 降级为索引**：`sid → {"user_id", "created_at", "mode"}`，不再持有会话正文；`_lock` 仅护索引。
- **中文文案逐字搬运**：见「行为等价性清单」。
- **不改** LLM 层 / 检索策略 / 提示词 / reviewer 入参语义 / `app/*`。
- **工具层解冻**（D3）：仅 `tools._memory_search` 一处改为惰性 import orchestrator 取记录。
- **依赖**：无需新增（`InMemorySaver` 随 `langgraph>=1.1.9` 自带）。
- **验收门禁**：`tests/test_orchestrator_paths.py` 全绿 → `pytest tests/ -q` 全量绿 → 全量 `eval/run_eval.py` 对照**新基线**（先对 main 现 Phase 1 代码重跑一份，再跑 Phase 2）。
- **单一变量**：Phase 2 独立对照，不与其它变更混轮。
- **提交策略**：每阶段验证通过后单独提交，**由用户确认后再提交（不自动提交）**。
- **环境**：解释器 `D:\Anaconda\envs\shixi\python.exe`；工作目录 `d:\实习\my_project\deep_search_pro-master`；shell **PowerShell 5（不支持 `&&` / heredoc）**，多命令用 `;` 或分次；命令前加 `$env:PYTHONIOENCODING="utf-8"`。分支 `refactor/langgraph-storage`。

---

## 行为等价性清单（逐字锁定 · spec §5）

| # | 必须保持的语义 | 改造后位置 |
|---|---|---|
| 1 | 分档：quick→`quick`节点；normal/deep+`enable_planner`→`plan`；否则→`fallback`节点 | `_route_after_rewrite` |
| 2 | `max_rounds`：normal=1 / deep=`MAX_ITERATION` | `_node_rewrite` 回填 |
| 3 | 首轮不可用：plan 标 failed「Planner 不可用，回退 v3 固定流水线」+ note「Planner 不可用（输出解析失败 / 无 function-call 模型），回退 v3 固定流水线」+ 执行 fallback（review 未执行） | `_node_plan` + `plan→fallback` 边 |
| 4 | 未启用开关：`planner_fallback=True` + 直接 fallback；plan 标 skipped「回退 v3 固定流水线（未启用 / 不可用 Planner）」 | `_route_after_rewrite` + `_node_fallback` |
| 5 | retry 轮"先自增 `loop_counter` 再调 plan"顺序 | `_node_plan` retry 分支（Phase 1 原样） |
| 6 | 零证据短路：「无检索证据，跳过复审」+ `review_skipped=True` | `_node_review`（Phase 1 原样） |
| 7 | 达上限：`iteration_limit=True` + 「达到迭代上限（N 轮），强制结束」 | `_node_review`（Phase 1 原样） |
| 8 | 收尾：`iteration_count=round_no` + `_flush_retrieval_nodes` + summarize + 质检/迭代告警前缀 | `_node_finalize` + `_step_summarize`（原样） |
| 9 | 异常：整体不中断，`error` 落地 + `finished=True` | `_run_pipeline` try/except/finally |
| 10 | 综述 4 节点：route→library→analyze→summarize，文案不变 | 综述图节点 |
| 11 | `_memory_records` 字段与过滤（finished + 有 result）不变 | `_memory_records` 改源 |

---

## 契约冻结清单（spec §4.6 · 一字不改）

**`get_steps` 返回**：`session_id` / `query` / `mode` / `complexity` / `steps`(list[dict]) / `finished` / `loop_counter` / `tool_calls`(list[dict]) / `review_notes`。

**`get_result` 返回**：`session_id` / `query` / `mode` / `complexity` / `rewritten_query` / `rewritten` / `points` / `citations` / `answer` / `planner_notes` / `iteration_count` / `finished` / `error`；`mode=="review"` 时附 `subscription_id` / `direction_name` / `paper_count` / `sections` / `outline`。

字段名 / 类型 / 缺省值逐字取自现 [orchestrator.py](file:///d:/实习/my_project/deep_search_pro-master/ai/orchestrator.py) 的 `get_steps`(L133–149) / `get_result`(L152–179)。

---

## Phase 2-a · 清不可序列化项（先做，最小且必绿）

### Task 1: 删除 `_memory_records` 闭包 + 工具层惰性取记录

**Files:**
- Modify: `ai/orchestrator.py`（`ask` 内删一行闭包赋值）
- Modify: `ai/tools.py`（`_memory_search` 改惰性取记录）

**Interfaces:**
- Consumes: `orchestrator._memory_records(user_id, session_id) -> list[dict]`（已存在）
- Produces: 会话 dict 不再含函数对象（可被 msgpack 序列化）；`memory_search` 仍返回「历史会话命中 N 条」

- [ ] **Step 1: 删除 `ai/orchestrator.py` 中 `ask()` 的闭包赋值**

删除 L87–88 两行（含注释）：

```python
    # memory_search 工具的数据源回调（读取该用户近期会话）
    session["_memory_records"] = lambda: _memory_records(user_id, session_id)
```

（`_memory_records` 函数本身**保留不动**；`_run_retrieval_by_routes` 对它的**直接调用也不动**。）

- [ ] **Step 2: 改 `ai/tools.py::_memory_search`**

将 L69–70：

```python
    recall = ctx.get("_memory_records")
    records = recall() if callable(recall) else []
```

替换为：

```python
    from ai import orchestrator as _orch        # 惰性导入：运行期解析，避免模块级循环依赖
    records = _orch._memory_records(ctx.get("user_id"), session_id)
```

（其余不变。）

- [ ] **Step 3: 跑既有测试确认全绿**

Run:

```powershell
$env:PYTHONIOENCODING="utf-8"; D:\Anaconda\envs\shixi\python.exe -m pytest tests/ -q
```

Expected: 全绿（37 passed；本步不改任何被测行为）

- [ ] **Step 4: 自查会话可序列化**

Run:

```powershell
$env:PYTHONIOENCODING="utf-8"; D:\Anaconda\envs\shixi\python.exe -c "import ormsgpack; from ai import orchestrator; import inspect; src=inspect.getsource(orchestrator.ask); print('_memory_records' in src)"
```

Expected: `False`（闭包赋值已移除）

---

## Phase 2-b · 图与存储骨架（核心）

### Task 2: 统一 QA 流水线图 + 综述图 + 索引 + 接口层改源

**Files:**
- Modify: `ai/orchestrator.py`（多处）
- Modify: `eval/run_eval.py`（L85 直读改源）
- Test: `tests/test_orchestrator_paths.py`（改造 ⑧⑪⑫）

**Interfaces:**
- Consumes: 既有 helper `_step_rewrite` / `_run_retrieval_by_routes` / `_analyze` / `_step_summarize` / `_node_plan` / `_node_execute_tools` / `_node_review` / `_node_finalize` / `_mark` / `_step` / `_step_review_retrieve` / `_step_review_generate`
- Produces: `_PIPELINE_SAVER` / `_PIPELINE_GRAPH` / `_REVIEW_GRAPH` / `_node_rewrite` / `_node_quick` / `_node_fallback` / `_node_review_*` / `ReviewGraphState` / `_config(sid, mode)` / `_safe_update(graph, cfg, values)` / `_seed_pipeline` / `_seed_review` / `_load_session(sid, mode)` / `get_raw(sid)`；改后的 `ask` / `ask_review` / `get_steps` / `get_result` / `_run_pipeline` / `_run_review_pipeline`；`_QA_GRAPH` / `_run_planner_loop` 删除

- [ ] **Step 1: 改造测试 ⑧⑪⑫（RED）**

将 `tests/test_orchestrator_paths.py` 的 ⑧ `test_first_round_planner_unavailable` 整段替换为：

```python
# ---------- ⑧ 首轮 Planner 不可用（并入流水线图：等价断言，spec §5 #3） ----------

def test_first_round_planner_unavailable(stub):
    stub["plans"] = [None]                      # 首轮 plan 返回 None
    s = _make_session()
    orchestrator._run_pipeline(s)

    assert s["error"] is None and s["finished"] is True
    assert s["planner_fallback"] is True
    assert "Planner 不可用（输出解析失败 / 无 function-call 模型），回退 v3 固定流水线" in s["planner_notes"]
    assert _step(s, "plan")["status"] == "skipped"
    assert _step(s, "plan")["detail"] == "回退 v3 固定流水线（未启用 / 不可用 Planner）"
    assert stub["review_calls"] == 0            # 复审从未执行
    assert s["iteration_count"] == 0            # 未走 finalize
```

将文件末尾两条结构用例替换为：

```python
# ---------- 新图 API（Phase 2 存储迁移；改造前应为红灯） ----------

def test_pipeline_graph_topology_has_seven_nodes():
    """统一流水线图在模块级编译一次，含 7 个业务节点"""
    nodes = set(orchestrator._build_pipeline_graph().get_graph().nodes)
    assert {"rewrite", "quick", "plan", "execute_tools",
            "review", "finalize", "fallback"} <= nodes


def test_qagraphstate_fields():
    """QAGraphState 字段集固定为 6 项"""
    assert set(orchestrator.QAGraphState.__annotations__) == {
        "session", "round_no", "max_rounds", "plan", "note", "outcome"}
```

**同时删除夹具残留**：`tests/test_orchestrator_paths.py` 的 `_make_session()` 内 `s["_memory_records"] = lambda: []`（原 L42）必须删除 —— 会话 dict 将被 checkpointer 序列化，函数对象会触发 `TypeError: Type is not msgpack serializable: function`（已实测）。Task 1 已使生产代码不再写入该键，此行为残留夹具。

- [ ] **Step 2: 跑测试确认新用例 FAIL（RED）**

Run:

```powershell
$env:PYTHONIOENCODING="utf-8"; D:\Anaconda\envs\shixi\python.exe -m pytest tests/test_orchestrator_paths.py -q
```

Expected: `test_pipeline_graph_topology_has_seven_nodes` FAIL（`AttributeError: _build_pipeline_graph`）；⑧ 亦 FAIL（`_run_planner_loop` 仍在但行为未变 → ⑧ 新断言要求 planner_fallback 等，现状不符）。其余 10 条 PASS。

- [ ] **Step 3: 改 `ai/orchestrator.py` —— 顶部 import 与模块级存储**

在既有 `from langgraph.graph import END, StateGraph` 之后补：

```python
from langgraph.checkpoint.memory import InMemorySaver
```

将 `_sessions` 段（L52–53）改为：

```python
# 会话最小索引：sid → {user_id, created_at, mode}；会话正文由 checkpointer 承载（Phase 2）
_sessions: dict[str, dict] = {}
_lock = threading.Lock()
_PIPELINE_SAVER = InMemorySaver()          # QA 图与综述图共用同一 saver 实例
```

- [ ] **Step 4: `ask` / `ask_review` 改 seed**

`ask` 内：`_sessions[session_id] = session`（以及其上的 `with _lock: _cleanup_expired()`）改为：

```python
    with _lock:
        _cleanup_expired()
        _sessions[session_id] = {"user_id": user_id,
                                 "created_at": session["created_at"],
                                 "mode": mode}
    _seed_pipeline(session)
```

`ask_review` 内同理：

```python
    with _lock:
        _cleanup_expired()
        _sessions[session_id] = {"user_id": user_id,
                                 "created_at": session["created_at"],
                                 "mode": "review"}
    _seed_review(session)
```

- [ ] **Step 5: `get_steps` / `get_result` / 新增 `get_raw` 改源**

`get_steps` 整个函数体替换为：

```python
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
```

`get_result` 整个函数体替换为（字段集与原文逐字一致，仅取数源改 `_load_session`）：

```python
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
```

- [ ] **Step 6: 新增存储辅助（放在 `_cleanup_expired` 之后）**

```python
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
        print(f"[orchestrator] checkpoint 写入失败: {e}")


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
```

- [ ] **Step 7: `_cleanup_expired` 改源**

```python
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
```

- [ ] **Step 8: `_memory_records` 改源**

```python
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
```

- [ ] **Step 9: `_run_pipeline` / `_run_quick` / `_run_fallback` 图中化**

`_run_pipeline` 替换为：

```python
def _run_pipeline(session: dict) -> None:
    """QA 流水线（后台线程）：启动统一 LangGraph 图并落盘终态"""
    cfg = _config(session["session_id"], "qa")
    try:
        _PIPELINE_GRAPH.invoke(
            {"session": session, "round_no": 1, "max_rounds": 0,
             "plan": None, "note": None, "outcome": ""}, cfg)
    except Exception as e:
        session["error"] = f"流水线异常: {e}"
        print(f"[orchestrator] 会话 {session['session_id']} 异常: {e}")
    finally:
        session["finished"] = True
        _safe_update(_PIPELINE_GRAPH, cfg, {"session": session})
```

`_run_quick` / `_run_fallback` 两个函数体抽为节点（函数本身删除，逻辑迁入 `_node_quick` / `_node_fallback`，见 Step 10）。

- [ ] **Step 10: 新增 QA 图节点 + 路由 + 装配（替换 Phase 1 的 `_QA_GRAPH`/`_run_planner_loop`/`_build_qa_graph` 段）**

删除 Phase 1 的 `_run_planner_loop`(L240–253)、`_route_after_plan` 中 `"end"` 目标、`_build_qa_graph`、`_QA_GRAPH`（`_node_plan`/`_node_execute_tools`/`_node_review`/`_node_finalize`/`_route_after_review` 保留改用于新图）。新增：

```python
# ---------- QA 流水线图（LangGraph · 会话正文落 checkpointer） ----------
# 顶层 7 节点（rewrite/quick/plan/execute_tools/review/finalize/fallback）+ 3 条条件边
# 状态单通道：session dict 承载会话正文；节点 return {"session": session} 整体落盘

def _node_rewrite(state: QAGraphState) -> dict:
    """① 查询改写 + 路由 + 复杂度分级；回填 max_rounds（等价原 L248）"""
    session = state["session"]
    _step(session, "rewrite", _step_rewrite)
    max_rounds = 1 if session.get("complexity") == "normal" else MAX_ITERATION
    return {"session": session, "max_rounds": max_rounds}


def _node_quick(state: QAGraphState) -> dict:
    """快速通道（原 _run_quick 函数体，逐字搬运）"""
    session = state["session"]
    _mark(session, "plan", "skipped", "快速通道跳过规划调度")
    _mark(session, "decompose", "skipped", "快速通道跳过规划调度")
    _run_retrieval_by_routes(session)
    _mark(session, "review", "skipped", "快速通道跳过复审校验")
    _step(session, "summarize", _step_summarize)
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
    return {"session": session}


def _route_after_rewrite(state: QAGraphState) -> str:
    """rewrite 出边：quick → quick；启用 Planner → plan；否则 → fallback"""
    s = state["session"]
    if s.get("complexity") == "quick":
        return "quick"
    return "plan" if s.get("enable_planner") else "fallback"


def _route_after_plan(state: QAGraphState) -> str:
    """plan 出边：首轮不可用 → fallback；第 N 轮规划失败 → finalize；否则 → execute_tools"""
    return {"planner_unavailable": "fallback", "plan_degraded": "finalize"}.get(
        state["outcome"], "execute_tools")


def _build_pipeline_graph():
    """装配并编译统一 QA 流水线图（挂 InMemorySaver）"""
    g = StateGraph(QAGraphState)
    g.add_node("rewrite", _node_rewrite)
    g.add_node("quick", _node_quick)
    g.add_node("plan", _node_plan)
    g.add_node("execute_tools", _node_execute_tools)
    g.add_node("review", _node_review)
    g.add_node("finalize", _node_finalize)
    g.add_node("fallback", _node_fallback)
    g.set_entry_point("rewrite")
    g.add_conditional_edges("rewrite", _route_after_rewrite,
                            {"quick": "quick", "plan": "plan", "fallback": "fallback"})
    g.add_conditional_edges("plan", _route_after_plan,
                            {"fallback": "fallback", "finalize": "finalize",
                             "execute_tools": "execute_tools"})
    g.add_edge("execute_tools", "review")
    g.add_conditional_edges("review", _route_after_review,
                            {"plan": "plan", "finalize": "finalize"})
    g.add_edge("quick", END)
    g.add_edge("fallback", END)
    g.add_edge("finalize", END)
    return g.compile(checkpointer=_PIPELINE_SAVER)


_PIPELINE_GRAPH = _build_pipeline_graph()
```

（`_node_plan`/`_node_execute_tools`/`_node_review`/`_node_finalize`/`_route_after_review` 保持 Phase 1 原样，无需改动。）

- [ ] **Step 11: 新增综述图 + `_run_review_pipeline` 改图**

删除原 `_run_review_pipeline` 函数体，替换为：

```python
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
    """综述流水线（后台线程）：启动综述图并落盘终态"""
    cfg = _config(session["session_id"], "review")
    try:
        _REVIEW_GRAPH.invoke({"session": session}, cfg)
    except Exception as e:
        session["error"] = f"综述流水线异常: {e}"
        print(f"[orchestrator] 综述会话 {session['session_id']} 异常: {e}")
    finally:
        session["finished"] = True
        _safe_update(_REVIEW_GRAPH, cfg, {"session": session})
```

- [ ] **Step 12: 改 `eval/run_eval.py` L85 直读**

将：

```python
    raw = orchestrator._sessions.get(sid) or {}  # noqa: SLF001
```

替换为：

```python
    raw = orchestrator.get_raw(sid) or {}
```

- [ ] **Step 13: 跑测试确认全绿（GREEN）**

Run:

```powershell
$env:PYTHONIOENCODING="utf-8"; D:\Anaconda\envs\shixi\python.exe -m pytest tests/ -q
```

Expected: 全量绿（37 passed：原 8 用例中 ①–⑦⑨⑩ 不动 + ⑧⑪⑫ 已改造）

- [ ] **Step 14: 提交（需用户确认）**

```powershell
git add ai/orchestrator.py ai/tools.py eval/run_eval.py tests/test_orchestrator_paths.py
git commit -m "refactor(qa): 会话存储迁移至 LangGraph checkpointer（统一流水线图 + 综述图 + 最小索引，接口层改读 get_state）"
```

---

## Phase 2-c · 存储层用例与评测

### Task 3: 新增存储层用例

**Files:**
- Test: `tests/test_orchestrator_paths.py`（追加）

**Interfaces:**
- Consumes: `orchestrator._PIPELINE_GRAPH` / `orchestrator._load_session` / `orchestrator.get_steps` / `orchestrator.get_raw` / `orchestrator._memory_records` / `orchestrator._sessions` / `orchestrator._cleanup_expired`
- Produces: §6.2 五条用例

- [ ] **Step 1: 追加用例**

追加到 `tests/test_orchestrator_paths.py` 末尾（复用既有 `stub` / `_make_session` / `_plan`）：

```python
# ---------- 存储层（Phase 2） ----------

def test_no_lambda_in_session(stub):
    """会话 dict 可被 checkpointer serde 序列化（防闭包回归）"""
    import ormsgpack
    s = _make_session()
    ormsgpack.packb({k: v for k, v in s.items() if k != "created_at"})


def test_session_persisted_in_checkpointer(stub):
    """_run_pipeline 后，可经 checkpointer 读回会话正文"""
    stub["plans"] = [_plan()]
    stub["reviews"] = [True]
    s = _make_session(session_id="persist-1")
    orchestrator._run_pipeline(s)
    back = orchestrator._load_session("persist-1", "phrase")
    assert back is not None
    assert back["iteration_count"] == 1
    assert back["finished"] is True


def test_get_steps_maps_from_get_state(stub):
    """get_steps 字段集与冻结契约一致"""
    stub["plans"] = [_plan()]
    stub["reviews"] = [True]
    s = _make_session(session_id="steps-1")
    orchestrator._sessions["steps-1"] = {"user_id": 1, "created_at": s["created_at"], "mode": "phrase"}
    orchestrator._run_pipeline(s)
    out = orchestrator.get_steps("steps-1")
    assert set(out) == {"session_id", "query", "mode", "complexity", "steps",
                        "finished", "loop_counter", "tool_calls", "review_notes"}
    assert out["finished"] is True and out["loop_counter"] == 1


def test_ttl_cleanup_deletes_thread(stub):
    """过期会话：索引移除 + get_steps 返回 None"""
    import datetime as _d
    s = _make_session(session_id="ttl-1")
    orchestrator._sessions["ttl-1"] = {
        "user_id": 1, "created_at": _d.datetime.now() - orchestrator.SESSION_TTL - _d.timedelta(minutes=1),
        "mode": "phrase"}
    orchestrator._cleanup_expired()
    assert "ttl-1" not in orchestrator._sessions
    assert orchestrator.get_steps("ttl-1") is None


def test_memory_records_from_checkpointer(stub):
    """_memory_records 从 checkpointer 读已完成会话"""
    stub["plans"] = [_plan()]
    stub["reviews"] = [True]
    for sid in ("mem-1", "mem-2"):
        s = _make_session(session_id=sid)
        orchestrator._sessions[sid] = {"user_id": 1, "created_at": s["created_at"], "mode": "phrase"}
        orchestrator._run_pipeline(s)
    recs = orchestrator._memory_records(1, "nobody")
    assert {r["session_id"] for r in recs} >= {"mem-1", "mem-2"}
    assert all({"session_id", "query", "answer", "points"} <= set(r) for r in recs)
```

- [ ] **Step 2: 跑全量测试**

Run:

```powershell
$env:PYTHONIOENCODING="utf-8"; D:\Anaconda\envs\shixi\python.exe -m pytest tests/ -q
```

Expected: 全绿（37 + 5 = 42 passed）

---

### Task 4: 全量评测对照（单一变量验收门禁）

**Files:**
- 只读：`eval/run_eval.py`、`eval/cases.json`
- 产出：`eval/record/测试记录_<时间戳>.txt`（`eval/record/` 已 gitignore，不入库）

**Interfaces:**
- Consumes: Task 2/3 改造后的 orchestrator
- Produces: 与**新基线**（对 main 现 Phase 1 代码重跑）的逐项对照

- [ ] **Step 1: 跑 Phase 2 新基线（main 现代码）**

切到 main（或 stash），跑：

```powershell
$env:PYTHONIOENCODING="utf-8"; D:\Anaconda\envs\shixi\python.exe eval/run_eval.py
```

记录命中率 / 总 token / reviewer 通过数。

- [ ] **Step 2: 跑 Phase 2 对照**

切回 `refactor/langgraph-storage`，跑：

```powershell
$env:PYTHONIOENCODING="utf-8"; D:\Anaconda\envs\shixi\python.exe eval/run_eval.py
```

- [ ] **Step 3: 逐项对照**

| 指标 | 期望 |
|---|---|
| 期望引用命中率 | 与基线**一致**（±0；LLM 非确定性可解释小幅波动） |
| reviewer 通过数 | 与基线**一致** |
| 总 token | 与基线一致（±10% 内视为波动） |

- [ ] **Step 4: 漂移处置**

任一指标超阈：**停止**，按 spec §7 风险表定位（存储迁移 vs LLM 非确定性）；属实现缺陷则回修 Task 2 重跑；属非确定性（Q8 已知波动）则在记录标注并交用户裁定。

- [ ] **Step 5: 汇报结论（不提交评测记录）**，等用户确认 Phase 2 通过。

---

### Task 5: 登记文档变更 + Phase 2 收尾

**Files:**
- Modify: `doc/项目文档/文档变更记录.md`（追加条目）

**Interfaces:**
- Consumes: Task 2/3/4 的产出与结论
- Produces: 文档与代码状态一致

- [ ] **Step 1: 追加变更条目**

登记：变更名称「LangGraph 会话存储迁移（Phase 2）」、日期、涉及文件（`ai/orchestrator.py` / `ai/tools.py` / `eval/run_eval.py` / `tests/test_orchestrator_paths.py`）、关联设计/计划文档链接、验收结论（四路径 42 用例绿 / pytest 全量绿 / 全量 eval 逐项一致 / 3 处偏离已落地）。

- [ ] **Step 2: 提交（需用户确认）**

```powershell
git add doc/项目文档/文档变更记录.md
git commit -m "docs: 登记 LangGraph 会话存储迁移（Phase 2）变更"
```

---

## 自审记录（Self-Review）

**1. Spec 覆盖**：spec §4.2 存储后端 → Task 2 Step 3/6；§4.3 统一图 → Step 10；§4.4 `ask`/`_run_pipeline` → Step 4/9；§4.5 综述图 → Step 11；§4.6 接口层 → Step 5；§4.7 工具层 → Task 1；§4.8 TTL/评测直读 → Step 7/12；§5 等价清单 → 上文「行为等价性清单」+ Task 2 Step 1/13；§6 测试 → Task 2 Step 1 + Task 3；§7 风险 → 见各 Task；§8 文件布局 → 各 Task Files；§9 分阶段 → Phase 2-a/b/c；§10 偏离 → Global Constraints + Step 8/10/11。

**2. 占位符扫描**：全文无 `TBD` / `TODO` /「稍后补充」；每个代码 step 均给出可整段粘贴的代码；每条 `Run` 均给出完整命令与 Expected。

**3. 类型一致性**：`QAGraphState` 6 字段（`session/round_no/max_rounds/plan/note/outcome`）在 `_node_rewrite` 写入 `max_rounds`、`_run_pipeline` 组装入参、结构用例三处一致；`outcome` 取值域 `"" / planned / planner_unavailable / plan_degraded / no_evidence / passed / iteration_limit / retry` 与两条路由函数读出处一致；综述图 `ReviewGraphState` 单字段 `session`。

**4. 已知取舍**：⑧⑪⑫ 三条用例为「接口变更」的显式登记（Phase 1 的 `_QA_GRAPH`/`_run_planner_loop` 退役）；①–⑦⑨⑩ 五条路径用例**一行不改**，是本次迁移的核心护栏。
