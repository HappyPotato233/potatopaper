# LangGraph 会话存储迁移设计（Phase 2）

> 版本：v1.0　创建：2026-10-19
> 定位：把 QA 编排的**会话状态**从进程内 `_sessions` dict 迁移到 LangGraph **checkpointer**，接口层改读 `graph.get_state()`，**前端响应契约逐字段冻结**。
> 关联文档：《LangGraph编排重构设计.md》（Phase 1，§4.5 为本阶段概要）§1.2 动机；《多agent编排方案v4.md》§4/§5；《详细设计文档.md》§3.5/§5.1

---

## 1. 背景与问题

### 1.1 Phase 1 遗留

Phase 1 把 `_run_planner_loop` 的控制流图化，但**明确不迁存储**（spec §2.2 / D2）：会话 dict 仍存在模块级进程内 `_sessions`，`_QA_GRAPH` 不带 checkpointer。遗留问题：

1. **不可持久化**：进程重启即丢，Phase 1 §1.2 所列「断点续跑 / 按轮回放 / `interrupt()` 人工介入 / 跨进程会话恢复」全部无从落地。
2. **接口层与存储实现耦合**：[orchestrator.py](file:///d:/实习/my_project/deep_search_pro-master/ai/orchestrator.py) 的 `get_steps` / `get_result` / `_memory_records` 直接读写私有 dict `_sessions`。
3. **不可序列化项**：`session["_memory_records"]` 是闭包 lambda，无法进 checkpointer。
4. **会话枚举无对应 API**：`_memory_records` 需按 `user_id` 枚举历史会话；MemorySaver 无「按用户枚举」能力。

### 1.2 本阶段问题

将「会话」升级为 **图状态 + checkpointer 承载的持久化实体**，同时不改变任何对外可见行为（前端响应 schema、步骤文案、评测指标）。

---

## 2. 目标与非目标

### 2.1 目标

1. 会话状态由 LangGraph checkpointer 承载；`_sessions` **降级为最小会话索引**（不再持有会话正文）。
2. `get_steps` / `get_result` 只读 `graph.get_state()` 并映射回**逐字段冻结的响应 JSON schema**。
3. 清除会话内不可序列化项（`_memory_records` 闭包）。
4. 四路径（quick / normal / deep / fallback）+ 综述行为等价：既有 12 项集成测试护栏全绿、全量 `pytest` 全绿、全量 `eval/run_eval.py` 对照基线逐项一致。

### 2.2 非目标（明确不做）

- **不换 `SqliteSaver`**（需额外依赖 `langgraph-checkpoint-sqlite`）——留作 Phase 3 单独替换，守单一变量。
- 不改 LLM 层 `ai/llm_provider.py`、检索策略、提示词。
- 不改 reviewer 入参语义（方案 B / B-lite 另案）。
- 不改前端（`app/routers/qa.py`、`app/schemas/qa.py`）——响应 schema 冻结，接口层内部换源对前端透明。
- 不改 `eval/run_eval.py` 的对外指标口径（但其内部直读 `orchestrator._sessions` 的一行需随之改源，见 §4.8）。

---

## 3. 已确认的关键决策

| # | 决策 | 结论 |
|---|---|---|
| D1 | 迁移覆盖面 | **全域**：quick / normal / deep / fallback 四条 QA 路径 + 综述流水线全部入图，checkpointer 成为唯一会话正文存储 |
| D2 | checkpointer 选型 | **`InMemorySaver`**（langgraph 内置、零新依赖、进程内）；`SqliteSaver` 另起 Phase 3 |
| D3 | `_memory_records` 闭包去向 | **删除闭包**；`tools._memory_search` 改为按 ctx 的 `user_id`/`session_id` **惰性导入** orchestrator 的 `_memory_records(...)` 直接取（Phase 1 的「工具层不改」在此解冻） |
| D4 | `_sessions` 去留 | **保留为最小索引**：`session_id → {user_id, created_at, mode}`；用于存在性判定、按用户枚举、TTL 清理 |
| D5 | planner 节点归属 | **内联为统一顶层节点**（不采用「`_QA_GRAPH` 作子图」）——子图会把整段闭环塌缩成一个 super-step，粒度退化（见 §4.1 探针结论） |
| D6 | state 形态 | **保留 `session: dict` 单通道**（`{session, round_no, max_rounds, plan, note, outcome}`），不扁平化为「TypedDict 逐字段对齐 session 键名」。理由：单通道让每个节点 `return {"session": session}` 即整体落盘，避免给 append 语义字段引入 reducer 与「节点漏 return 某字段→丢失」的风险，最大化等价性（偏离 Phase 1 spec §4.5 的概要描述，见 §10） |
| D7 | thread 命名 | `thread_id = f"qa:{session_id}"`（QA 图）/ `f"rev:{session_id}"`（综述图），两图共用同一 saver、命名空间不冲突 |
| D8 | 单一变量 + 提交 | Phase 1 已并入 main；Phase 2 独立全量对照，SDD 子代理**不自动提交**，每次提交由用户确认 |

---

## 4. 设计

### 4.1 探针结论（本机 langgraph 1.1.9 实测 · 2026-10-19）

| 探针 | 结论 | 对设计的影响 |
|---|---|---|
| checkpointer 序列化 | `InMemorySaver` 走 msgpack；state 内含函数（lambda）→ `TypeError: Type is not msgpack serializable: function` | `_memory_records` 闭包**必须删除**（D3） |
| `datetime` | `datetime` 可被默认 serde 序列化，`get_state` 取回仍是 `datetime` 类型 | `created_at` 可直接留在会话内 |
| `invoke` 引用语义 | 带 checkpointer 的 `invoke` **不 deepcopy** 入参 state，`out["session"] is session` 为真 | `_run_pipeline(session)` 内联改后的 `session` 与原对象同一引用；集成测试按 `s` 原地断言**仍成立** |
| checkpoint 粒度（顶层节点） | 每个顶层节点结束各落一次 checkpoint，`get_state` 逐步可见 | 采用「planner 节点内联」（D5），保住逐节点轮询粒度 |
| checkpoint 粒度（子图） | 子图作为节点时，**整段子图塌缩为一个 super-step**，父图 `get_state` 只见子图整体结束 | **放弃**子图方案；若用子图，deep 模式前端将长时间停更（UX 退化） |
| `update_state` | 可在**全新 thread** 上落盘并创建 checkpoint；seed 后 `invoke(None)` / `invoke(payload)` 均能从 START 正常运行 | `ask` 可在启动线程**前**同步 seed 初始状态，消除「首次轮询读不到会话」的竞态 |

**结论**：D5（内联）+ D6（单通道）+ `update_state` seed 路线可行且等价性最优。

### 4.2 存储后端与最小索引

```python
from langgraph.checkpoint.memory import InMemorySaver

_PIPELINE_SAVER = InMemorySaver()          # QA 图与综述图共用同一 saver 实例

_sessions: dict[str, dict] = {}            # 最小索引：sid → {"user_id", "created_at", "mode"}
_lock = threading.Lock()                   # 仅保护索引（不再保护会话正文）
```

- **会话正文**（query / evidence / steps / tool_calls / planner_notes / result / …）只存在于 checkpointer，经 `graph.get_state(config)` 读取。
- 索引条目在 `ask` / `ask_review` 创建，在 `_cleanup_expired` 移除；`created_at` 移入索引（正文里的 `created_at` 可保留以兼容 `_memory_records` 读法，或统一从索引读——见 §4.6）。
- **线程配置**：

```python
def _config(session_id: str, mode: str) -> dict:
    prefix = "rev" if mode == "review" else "qa"
    return {"configurable": {"thread_id": f"{prefix}:{session_id}"}}
```

### 4.3 统一 QA 流水线图（planner 节点内联）

state schema 沿用 Phase 1 的 `QAGraphState`（D6）：

```python
class QAGraphState(TypedDict):
    session: dict        # 会话正文（唯一正文载体）
    round_no: int        # 当前轮次（初始 1）
    max_rounds: int      # 1（normal）/ MAX_ITERATION（deep）
    plan: dict | None
    note: dict | None
    outcome: str
```

**节点**（全部为顶层节点，业务逻辑复用既有 helper，逐字搬运）：

| 节点 | 职责 | 出处 |
|---|---|---|
| `rewrite` | `_step(session,"rewrite",_step_rewrite)`；随后据 `session["complexity"]` 回填 `max_rounds` | 原 `_run_pipeline` 第 ① 步 + L248 |
| `plan` | `_node_plan`（首轮 / retry 轮，逻辑同 Phase 1） | Phase 1 `_node_plan` |
| `execute_tools` | `_node_execute_tools` | Phase 1 |
| `review` | `_node_review` | Phase 1 |
| `finalize` | `_node_finalize`（含 `_step_summarize`） | Phase 1 |
| `quick` | `_run_quick` 现有函数体（mark + 检索 + summarize） | 原 `_run_quick` |
| `fallback` | 先 `session["planner_fallback"]=True`，再 `_run_fallback` 现有函数体 | 原 `_run_pipeline` 两处 fallback 分支 + `_run_fallback` |

**边**：

```
START → rewrite
rewrite ──conditional──> quick      (complexity == "quick")
                      ──> plan       (enable_planner 为真)
                      ──> fallback   (enable_planner 为假)
plan    ──conditional──> fallback   (planner_unavailable：首轮不可用 → 与现状一致地回退 v3)
                      ──> finalize   (plan_degraded：第 N 轮规划失败)
                      ──> execute_tools
execute_tools → review
review  ──conditional──> plan       (retry)
                      ──> finalize   (no_evidence / passed / iteration_limit)
quick → END;  fallback → END;  finalize → END
```

**路由函数**（纯函数，只读 state）：

```python
def _route_after_rewrite(state) -> str:
    s = state["session"]
    if s.get("complexity") == "quick":
        return "quick"
    return "plan" if s.get("enable_planner") else "fallback"

def _route_after_plan(state) -> str:          # Phase 1 基础上把 "end" 目标改为 "fallback"
    return {"planner_unavailable": "fallback", "plan_degraded": "finalize"}.get(
        state["outcome"], "execute_tools")

def _route_after_review(state) -> str:        # 与 Phase 1 一致
    return "plan" if state["outcome"] == "retry" else "finalize"
```

> **关键等价点**：原 `_run_pipeline` 在 `_run_planner_loop` 返回 `False` 时执行 `session["planner_fallback"]=True; _run_fallback(session)`。改为 `plan --planner_unavailable--> fallback` 后，`fallback` 节点在入口置 `planner_fallback=True` 再跑 `_run_fallback`，语义完全一致（无论入口来自 `rewrite` 的 fallback 分支还是 `plan` 的 unavailable 分支）。

**`max_rounds` 回填**：`rewrite` 节点在 `_step_rewrite` 后 `return {"session": session, "max_rounds": 1 if session.get("complexity")=="normal" else MAX_ITERATION}`（等价 L248；`complexity=="quick"` 时该值不被 `plan` 使用，无害）。

**图装配**：

```python
def _build_pipeline_graph():
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

### 4.4 `_run_pipeline` / `ask` 改造（seed + invoke + finally 落盘）

```python
def _run_pipeline(session: dict) -> None:
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

- `try/except/finally` 结构、异常文案、`finished=True` 语义与现状一致。
- `finally` 增补 `_safe_update`（`update_state`）：把 `finished`/`error` 落进 checkpoint；`update_state` 对全新 thread 亦可用（探针已证），但仍**加 try 守卫**以防极端情况。
- `ask` 在**启动后台线程前**同步 seed 初始状态（消除首次轮询竞态）：

```python
def ask(user_id, query, mode="phrase", rewrite=True, enable_planner=True) -> str:
    session_id = uuid.uuid4().hex[:16]
    session = { ... 同现状，但删除 "_memory_records" 一行 ... }
    with _lock:
        _cleanup_expired()
        _sessions[session_id] = {"user_id": user_id, "created_at": session["created_at"], "mode": mode}
    _seed_qa(session)                       # update_state 初始状态（同步）
    threading.Thread(target=_run_pipeline, args=(session,), daemon=True).start()
    return session_id
```

`_seed_qa(session)` = `_safe_update(_PIPELINE_GRAPH, _config(sid,"qa"), {"session": session, "round_no":1, "max_rounds":0, "plan":None, "note":None, "outcome":""})`。

### 4.5 综述流水线图

```python
class ReviewGraphState(TypedDict):
    session: dict

def _node_review_route(state):     _step(state["session"], "route", lambda s: "综述模式（方向文献集）");     return {"session": state["session"]}
def _node_review_library(state):   _step(state["session"], "library", _step_review_retrieve);              return {"session": state["session"]}
def _node_review_analyze(state):   _step(state["session"], "analyze", _analyze);                          return {"session": state["session"]}
def _node_review_summarize(state): _step(state["session"], "summarize", _step_review_generate);           return {"session": state["session"]}

_REVIEW_GRAPH = 同上 4 节点线性装配 + compile(checkpointer=_PIPELINE_SAVER)
```

`_run_review_pipeline(session)` 同样改为 `try/invoke/except/finally(+finished 落盘)`；`ask_review` 同步 seed（thread `rev:{sid}`）。

### 4.6 接口层映射（响应 schema 逐字段冻结）

```python
def _load_session(session_id: str, mode: str) -> dict | None:
    graph = _REVIEW_GRAPH if mode == "review" else _PIPELINE_GRAPH
    snap = graph.get_state(_config(session_id, mode))
    return snap.values.get("session") if snap and snap.values else None
```

- `get_steps(session_id)`：索引查存在性 → `_load_session` → 按**与现状完全相同的字段集**返回（`session_id/query/mode/complexity/steps/finished/loop_counter/tool_calls/review_notes`）。索引缺失或 state 缺失 → `None`（HTTP 404）。
- `get_result(session_id)`：同上，字段集不变（含综述分支的 `subscription_id/direction_name/paper_count/sections/outline`）。
- **硬约束**：`app/schemas/qa.py` 的 `StepsResponse` 及前端依赖的字段名/类型/缺省，一字不改。

### 4.7 工具层（解冻）与 `_memory_records`

- 删除 `ask` 中 `session["_memory_records"] = lambda: ...`。
- `tools._memory_search` 改为：

```python
def _memory_search(args, ctx):
    session_id = ctx.get("session_id")
    asked = args.get("session_id")
    if not session_id or (asked and asked != session_id):
        return _fail("越权：session_id 校验不通过")
    from ai import orchestrator as _orch        # 惰性导入：运行期解析，避免模块级循环依赖
    records = _orch._memory_records(ctx.get("user_id"), session_id)
    struct = ctx.get("query_struct") or {}
    evidence = retriever_mod.search_memory(records, ctx.get("query", ""), struct.get("keywords"))
    return _ok(f"历史会话命中 {len(evidence)} 条", evidence)
```

- `_memory_records(user_id, exclude_session)` 改为**从索引取候选、从 checkpoint 读正文**：

```python
def _memory_records(user_id, exclude_session):
    with _lock:
        cand = [(sid, m) for sid, m in _sessions.items()
                if m["user_id"] == user_id and sid != exclude_session]
    cand.sort(key=lambda x: x[1]["created_at"], reverse=True)
    out = []
    for sid, m in cand:
        s = _load_session(sid, m["mode"])
        if not s or not s.get("finished") or not s.get("result"):
            continue
        out.append({"session_id": sid, "query": s["query"],
                    "answer": s["result"].get("answer", ""),
                    "points": s["result"].get("points", [])})
        if len(out) >= MEMORY_LOOKBACK:
            break
    return out
```

- `_run_retrieval_by_routes` 内对 `_memory_records(...)` 的调用**不变**（本就直调模块函数，非闭包）。

### 4.8 TTL 清理 与 评测直读

- `_cleanup_expired`（ask 时锁内调用）：

```python
def _cleanup_expired() -> None:
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

- `eval/run_eval.py` L85 `orchestrator._sessions.get(sid)` 直读正文一行，改为 `orchestrator.get_raw(sid)`（新增薄封装：按索引 → `_load_session`），对外指标口径不变。

---

## 5. 行为等价性清单（重构验收标准）

所有**中文文案逐字一致**（进评测记录与前端展示，改字即行为变更）：

| # | 必须保持的语义 | 现状出处 |
|---|---|---|
| 1 | 四路径分档：quick / normal / deep / fallback 的触发条件与现状一致 | `_run_pipeline` |
| 2 | `max_rounds`：normal=1 / deep=MAX_ITERATION | L248 |
| 3 | 首轮不可用：`planner_fallback=True` + plan 标 failed「Planner 不可用，回退 v3 固定流水线」+ 执行 fallback（review 未执行） | L246–249 + L217–223 |
| 4 | 未启用开关：`planner_fallback=True` + 直接 fallback | L220–223 |
| 5 | retry 轮"先自增 `loop_counter` 再调 plan"顺序 | Phase 1 `_node_plan` |
| 6 | 零证据短路：文案「无检索证据，跳过复审」+ `review_skipped` | Phase 1 `_node_review` |
| 7 | 达上限：`iteration_limit=True` + 「达到迭代上限（N 轮），强制结束」 | Phase 1 `_node_review` |
| 8 | 收尾：`iteration_count=round_no` + `_flush_retrieval_nodes` + summarize + 质检/迭代告警前缀 | Phase 1 `_node_finalize` + `_step_summarize` |
| 9 | 异常：整体不中断，`error` 落地 + `finished=True` | `_run_pipeline` |
| 10 | `get_steps` / `get_result` 响应 JSON schema 逐字不变 | §4.6 |
| 11 | 综述 4 节点顺序与文案不变 | `_run_review_pipeline` |
| 12 | `_memory_records` 返回字段与过滤（finished + 有 result）不变 | §4.7 |

---

## 6. 测试设计

### 6.1 既有 12 项护栏（`tests/test_orchestrator_paths.py`）

- ①–⑦、⑨、⑩ 直接调用 `_run_pipeline(s)` 并断言 `s`（原地引用不变）→ **期望不改一行、全绿**。
- ⑧ `test_first_round_planner_unavailable` 调 `_run_planner_loop(s)`；该函数在 D5 内联方案下**并入流水线图**，改为等价断言：`_run_pipeline(s)` 下 `planner_fallback=True`、plan 标 failed、review 未执行、走 fallback。**须新增该用例，并说明 ⑧ 的契约变更**。
- ⑪ `test_qa_graph_topology_has_four_nodes`、⑫ `test_qagraphstate_fields` 针对 Phase 1 的 `_QA_GRAPH`；改为断言 `_PIPELINE_GRAPH` 的 7 节点（rewrite/quick/plan/execute_tools/review/finalize/fallback）与 `QAGraphState` 6 字段。

### 6.2 新增存储层用例

| 用例 | 断言 |
|---|---|
| `test_session_persisted_in_checkpointer` | `ask` 后 `_load_session` 能读回初始 steps；索引不含会话正文 |
| `test_get_steps_maps_from_get_state` | 完成一次 normal 后，`get_steps` 字段集与现状逐字一致 |
| `test_ttl_cleanup_deletes_thread` | 过期会话：索引移除 + `get_state` 空 / `get_steps` 返回 None |
| `test_memory_records_from_checkpointer` | 完成 2 个会话后，`_memory_records` 按 created_at 倒序返回、字段一致 |
| `test_no_lambda_in_session` | 会话 dict 直接 `msgpack.packb(...)` 不抛异常（防回归） |

### 6.3 评测门禁

- 先对 **main 现（Phase 1）代码重跑一份新基线**，再在 Phase 2 分支跑对照（LLM 非确定性，故基线需同轮次语义、允许 ±10% token 波动；命中率/reviewer 通过数应一致）。

---

## 7. 风险与对策

| 风险 | 对策 |
|---|---|
| 序列化「整段 session」在大证据量下开销上升 | 仅 InMemorySaver；如显著影响耗时，Phase 3 评估按字段拆分通道 |
| `update_state` 在异常路径（thread 尚无 checkpoint）失败 | `ask` 已同步 seed，thread 必然存在；`_safe_update` 仍加 try 守卫 |
| 跨测试复用同一 `session_id` 线程导致 checkpoint 串扰 | `_run_pipeline` 每次 `invoke` 以完整初始 payload 覆盖；必要时测试改用唯一 id |
| `get_state` 粒度 = 每节点一次（现状每 `_mark` 一次） | 仅 UI 刷新粒度微调，评测/测试不可见；spec §4.1 已证实顶层节点粒度接近现状 |
| `tools` 惰性导入 orchestrator 引起循环依赖 | 仅在函数体内导入（运行期解析），模块级无循环 |
| 前端契约被无意改动 | §4.6 冻结字段；§6.1 用例逐字断言 detail 文案与响应字段 |
| 子图粒度陷阱 | D5 明确放弃子图，planner 节点内联；§4.1 探针留档 |

---

## 8. 文件布局

| 文件 | 动作 |
|---|---|
| `ai/orchestrator.py` | 新增 saver/索引/schema/图装配/`_node_*`/路由/`_load_session`/`_safe_update`/`_seed_*`；改造 `ask`/`ask_review`/`get_steps`/`get_result`/`_memory_records`/`_cleanup_expired`/`_run_pipeline`/`_run_review_pipeline`；删除 `_memory_records` 闭包与 `_QA_GRAPH`/`_run_planner_loop` |
| `ai/tools.py` | `_memory_search` 改惰性取记录（D3） |
| `eval/run_eval.py` | L85 直读 `_sessions` 改为读 `get_raw`（§4.8） |
| `tests/test_orchestrator_paths.py` | 改造 ⑧⑪⑫；新增 §6.2 用例 |
| `doc/项目文档/文档变更记录.md` | 实施后登记本次变更 |
| `requirements.txt` | 无需新增（InMemorySaver 随 langgraph 自带） |

---

## 9. 分阶段交付与验收

1. **Phase 2-a · 图与存储骨架**：落 `_PIPELINE_GRAPH` / `_REVIEW_GRAPH` + saver + 索引 + `_load_session`；改造 `ask`/`ask_review`/`get_steps`/`get_result`；测试 ①–⑦⑨⑩ 全绿。
2. **Phase 2-b · 清不可序列化项 + 工具层**：删闭包、改 `_memory_search`、`_memory_records`/TTL 改源；`test_no_lambda_in_session` 绿。
3. **Phase 2-c · 结构用例与评测**：改造 ⑧⑪⑫、新增 §6.2 用例；`pytest tests/ -q` 全量绿；全量 `eval/run_eval.py` 对照新基线。

> 提交策略：每阶段验证通过后单独提交，**由用户确认后再提交（不自动提交）**。

---

## 10. 对 Phase 1 spec §4.5 的偏离说明（需用户知悉）

1. **state 不扁平化**（D6）：§4.5 概要建议「TypedDict 字段对齐 session 键名 + append 语义 reducer」；本设计保留 `session: dict` 单通道。理由：单通道整体落盘最等价、最不易漏字段，且 Phase 1 已验证该形态，扁平化收益不抵风险。
2. **planner 节点内联**（D5）：§4.5 未提子图；探针证明子图会把闭环塌缩为单 super-step，故内联。
3. **`_QA_GRAPH` / `_run_planner_loop` 退役**：内联后二者并入 `_PIPELINE_GRAPH`，属接口变更，测试 ⑧⑪⑫ 随之改造。

以上三点为「计划与文档概要冲突」的显式登记，实施以本 spec 为准。
