# LangGraph 编排重构设计（Planner 主-子闭环）

> 版本：v1.0　创建：2026-10-19
> 定位：把 `ai/orchestrator.py::_run_planner_loop()` 的硬编码 `while` 控制流，重构为 LangGraph `StateGraph`。**行为严格等价**（同一批 helper、同一 LLM 层、同一终止语义）。
> 关联文档：《多agent编排方案v4.md》§4.1（Planner 主-子闭环流程图）§4.2（MAX_LOOP=3）§5.1.3（迭代与降级）；《详细设计文档.md》§3.5（8 节点步骤条）§5.1（编排调度）

---

## 1. 背景与问题

### 1.1 现状

编排分档三通道（[orchestrator.py](file:///d:/实习/my_project/deep_search_pro-master/ai/orchestrator.py)）：

| 通道 | 入口 | 结构 |
|---|---|---|
| quick 快速通道 | `_run_quick` | 线性 3 步 |
| normal/deep Planner 主-子闭环 | `_run_planner_loop` | **`while True` + 8 节点 + 复审回边** |
| v3 回退 / 综述 | `_run_fallback` / `_run_review_pipeline` | 线性 4 步 |

`_run_planner_loop` 的循环骨架由 `break`/`return` 手写而成：规划 → 执行工具 → 复审 → 「通过 / 达上限 / 无证据 / 规划失败」四类终止 + 「未通过则回到规划」的回边。控制流与业务逻辑混在同一函数体内（`plan` 调用、`_execute_tool_calls`、`_do_review`、收尾三件套）。

### 1.2 问题

1. **控制流不可声明式阅读**：终止条件散落在 `while` 体内多个 `break` 分支，加档位、加节点、加人工介入都需要改函数体。
2. **扩展受限**：将来要做断点续跑、按轮回放、`interrupt()` 人工介入、跨进程会话恢复，手写 `while` + 进程内 dict 都无从落地。
3. **编排层零测试覆盖**：现有 25 项测试全是组件单测（`reviewer` / `retriever` 等），四条编排路径**没有任何集成测试**——重构无回归锚点。

### 1.3 为什么引入 LangGraph

LangGraph 的 `StateGraph` 把"节点 + 条件边"显式化，恰好匹配本闭环"有环、有条件分支、状态需显式流转"的形态；其 checkpointer 体系后续可直接支撑断点续跑与人工介入。

---

## 2. 目标与非目标

### 2.1 目标

1. `_run_planner_loop` 的控制流改由图（4 节点 + 2 条条件边）表达，**行为与现状严格等价**。
2. 补齐编排层四路径集成测试，作为本次及后续重构的回归基线。
3. 为 Phase 2 的存储迁移（checkpointer）预留干净的接口边界。

### 2.2 非目标（明确不做）

- 不迁 `_run_quick` / `_run_fallback` / `_run_review_pipeline`（无环线性流程，迁图收益为零）；
- 不改 `ask` / `ask_review` / `get_steps` / `get_result` / `_cleanup_expired`（Phase 1 内一行不改，前端契约零变更）；
- 不改 Reviewer 入参语义（"方案 B / B-lite：给 reviewer 草稿正文"另案处理）；
- 不改检索策略、提示词、工具层、`ai/llm_provider.py`；
- 不引入 `langchain-openai` 的 `ChatOpenAI`（LLM 调用继续走 `llm_provider.chat()` 原生 OpenAI SDK，也就不涉及 `extra_body` / `reasoning_effort` 透传问题）。

---

## 3. 已确认的关键决策

| # | 决策 | 结论 |
|---|---|---|
| D1 | 迁移范围 | **只迁 A**（`_run_planner_loop`）；B/C/D 原样保留 |
| D2 | 状态桥接 | **方案 2，分两阶段**：Phase 1 只迁控制流（state 仍落 `_sessions`），Phase 2 迁存储（`_sessions` → checkpointer） |
| D3 | 节点实现 | 节点函数**复用现有 helper**，只负责"何时调谁 + 返回 state 更新" |
| D4 | Reviewer 语义 | 本轮**不并入方案 B**，review 节点入参原样搬运 |
| D5 | 回归基线 | Phase 0 先落 `tests/test_orchestrator_paths.py` 四路径集成测试，**对现状硬编码 `while` 跑绿**后再动代码 |
| D6 | 单一变量 | Phase 1 与 Phase 2 各自独立回归对照，避免"控制流 + 存储"同时变更导致归因失效 |

---

## 4. 设计

### 4.1 图拓扑（4 节点 + 2 条条件边）

```
                              START
                                │
                                ▼
                        ┌──────────────┐
             retry ┌────│     plan     │
                   │    └──────────────┘
                   │       │          │
                   │       │          └── planner_unavailable ──► END
                   │       │                                      （外层转 _run_fallback）
                   │       ├── plan_degraded ────────┐
                   │       ▼                        │
                   │  ┌──────────────────┐          │
                   │  │  execute_tools   │          │
                   │  └──────────────────┘          │
                   │           │                    │
                   │           ▼                    │
                   │    ┌────────────┐              │
                   └────│   review   │              │
        retry(round+1)  └────────────┘              │
                             │                      │
             no_evidence / passed / iteration_limit │
                             │                      │
                             └──────────┬───────────┘
                                        ▼
                                 ┌────────────┐
                                 │  finalize  │
                                 └────────────┘
                                        │
                                        ▼
                                       END
```

`plan → execute_tools`（规划成功）与 `plan → finalize`（第 N 轮规划失败）为两条分支；`review` 出边二选一：`finalize`（无证据 / 通过 / 达上限）或 `plan`（retry）。

**路由条件逐条对照原 `while` 的 `break` / `return`：**

| 条件边 | 触发条件 | 对应原代码 |
|---|---|---|
| `plan → END` | 第 1 轮 `plan is None` | L246–249 `return False` |
| `plan → finalize` | 第 N(>1) 轮 `plan is None` | L276–278 `break` |
| `plan → execute_tools` | 规划成功 | L251 / L279 落盘后进循环 |
| `review → finalize` | 零证据 或 复审通过 或 达迭代上限 | L256–261 / L266–267 / L268–271 |
| `review → plan` | 复审未通过 且 未达上限 | L273–279 |

### 4.2 State 定义与桥接（Phase 1）

```python
class QAGraphState(TypedDict):
    session: dict        # 会话 dict 的引用——Phase 1 唯一状态载体
    round_no: int        # 当前轮次（初始 1）
    max_rounds: int      # 1（normal）/ MAX_ITERATION（deep）
    plan: dict | None    # 本轮 Planner 输出
    note: dict | None    # 上轮复审意见（供 retry 轮构造改进指令）
    outcome: str         # "" | planned | planner_unavailable | plan_degraded
                         #    | no_evidence | passed | iteration_limit | retry
```

`_run_planner_loop` 收窄为"图的壳"，对外签名与返回值不变：

```python
def _run_planner_loop(session: dict) -> bool:
    final = _QA_GRAPH.invoke({
        "session": session,
        "round_no": 1,
        "max_rounds": 1 if session.get("complexity") == "normal" else MAX_ITERATION,
        "plan": None, "note": None, "outcome": "",
    })
    return final["outcome"] != "planner_unavailable"
```

图在模块级编译一次复用（Phase 1 不带 checkpointer）：`_QA_GRAPH = _build_qa_graph()`。

**唯一技术风险**：LangGraph 是否会对入参 state 做 deepcopy？若会，节点内的 `session` 成为副本，`_sessions[session_id]` 与前端轮询将看不到变更。

- 首选写法：每个节点显式 `return {"session": session, ...}`，锁死引用；
- 兜底写法（仅当探针证明发生拷贝）：`invoke` 后若 `final["session"] is not session`，执行
  `session.clear(); session.update(final["session"])`——dict 身份不变，`_sessions` / `get_steps` 照常可见（必须带 `is not` 守卫，否则自更新会清空数据）；
- **Phase 0 第一件事即写探针脚本实测**，拿到结论再定实现写法。

### 4.3 节点职责

节点函数全部留在 [orchestrator.py](file:///d:/实习/my_project/deep_search_pro-master/ai/orchestrator.py) 内（`_node_plan` / `_node_execute_tools` / `_node_review` / `_node_finalize`），业务逻辑逐字搬运。

**`_node_plan`**：首轮与 retry 轮共用。
- 首轮：`planner_mod.plan(query, 1, _evidence_summary(session), "")`；返回 `None` → 落文案「Planner 不可用（…），回退 v3 固定流水线」+ `_mark(plan, failed)` + `outcome="planner_unavailable"`。
- retry 轮：**先** `session["loop_counter"] = round_no`**再**调 `planner_mod.plan(query, round_no, _evidence_summary(session), _suggestion_text(state["note"]))`；返回 `None` → 落文案「第 N 轮规划失败，基于现有证据产出」+ `outcome="plan_degraded"`。
- 成功：调 `_apply_plan(session, plan, round_no, max_rounds)`，`outcome="planned"`。

**`_node_execute_tools`**：`_execute_tool_calls(session, (state["plan"] or {}).get("tool_calls"))`。

**`_node_review`**：入口先做零证据短路（落文案 + `review_skipped=True` + `_mark(review, skipped)` → `outcome="no_evidence"`）；否则 `_do_review(session)`，取 `note = session["review_notes"]`，按序判定：
1. `note and note.get("passed")` → `outcome="passed"`；
2. `round_no >= max_rounds` → `iteration_limit=True` + 文案「达到迭代上限（N 轮），强制结束」→ `outcome="iteration_limit"`；
3. 否则 → `round_no+1` 与 `session["loop_counter"]=round_no+1` 一并写入返回值，`outcome="retry"`。

**`_node_finalize`**：`session["iteration_count"]=state["round_no"]` → `_flush_retrieval_nodes(session)` → `_step(session, "summarize", _step_summarize)`。

**路由函数**（纯函数，只读 state）：

```python
def _route_after_plan(state) -> str:
    return {"planner_unavailable": "end", "plan_degraded": "finalize"}.get(
        state["outcome"], "execute_tools")

def _route_after_review(state) -> str:
    return "plan" if state["outcome"] == "retry" else "finalize"
```

**图装配**：

```python
def _build_qa_graph():
    g = StateGraph(QAGraphState)
    g.add_node("plan", _node_plan)
    g.add_node("execute_tools", _node_execute_tools)
    g.add_node("review", _node_review)
    g.add_node("finalize", _node_finalize)
    g.set_entry_point("plan")
    g.add_conditional_edges("plan", _route_after_plan,
                            {"end": END, "finalize": "finalize", "execute_tools": "execute_tools"})
    g.add_edge("execute_tools", "review")
    g.add_conditional_edges("review", _route_after_review,
                            {"plan": "plan", "finalize": "finalize"})
    g.add_edge("finalize", END)
    return g.compile()
```

### 4.4 行为等价性清单（重构验收标准）

所有**中文文案逐字一致**（会进评测记录与前端展示，改字即行为变更）：

| # | 必须保持的语义 | 现状出处 |
|---|---|---|
| 1 | `max_rounds`：normal=1 / deep=3 | L243 |
| 2 | 首轮入参 `plan(query, 1, _evidence_summary(session), "")` | L245 |
| 3 | `_apply_plan` 调用时机（首轮 / retry 轮） | L251、L279 |
| 4 | retry 轮"先自增 `loop_counter` 再调 plan"的顺序 | L273–275 |
| 5 | `_execute_tool_calls` → tool_calls 记录 + `trace.tool` | L254 |
| 6 | 零证据：文案 + `review_skipped=True` + review 标 skipped + 结束 | L256–261 |
| 7 | `_do_review` 异常 → `review_notes=None`、`review_skipped=True`、review 标 failed | L339–350 |
| 8 | 通过判定 `note and note.get("passed")` | L266 |
| 9 | 达上限：`iteration_limit=True` + 文案「达到迭代上限（N 轮），强制结束」 | L268–271 |
| 10 | retry 轮 plan 失败：文案「第 N 轮规划失败，基于现有证据产出」 | L276–278 |
| 11 | 收尾：`iteration_count=round_no` + `_flush_retrieval_nodes` + `_step(summarize)` | L281–283 |
| 12 | 首轮不可用：文案 + `_mark(plan, failed)` + `return False`（**不走 finalize**） | L246–249 |

### 4.5 Phase 2 存储迁移（概要，独立 spec）

Phase 1 绿之后再单独起 spec/plan，要点：

- **state schema 化**：`TypedDict` 字段对齐现有 session 键名（`query` / `evidence` / `steps` / `tool_calls` / `planner_notes` / `loop_counter` / …），最大化 helper 复用；节点显式返回变更字段，append 语义（`tool_calls` / `planner_notes`）用 reducer 处理。
- **清掉不可序列化项**：`session["_memory_records"]` 是闭包 lambda，无法进 checkpointer——改为在需要处直接调 `_memory_records(user_id, session_id)`（该函数已存在，lambda 只是绑定参数）；`datetime` 交由 LangGraph 默认序列化器。
- **checkpointer**：`MemorySaver` 起步 → 需跨进程重启时换 `SqliteSaver`，`thread_id = session_id`。
- **接口层**：`get_steps` / `get_result` 改为读 `graph.get_state(config)` 并映射回**逐字段冻结的响应 JSON schema**。
- **验收**：四路径测试绿 + 响应契约快照比对 + 全量 `run_eval.py` 对照。

> 注：Phase 2 不做，Phase 1 也不白干——`_sessions` 只是可替换的存储后端，图结构 / 节点 / 路由全部复用。

---

## 5. 测试设计（`tests/test_orchestrator_paths.py`）

monkeypatch `planner_mod.plan` / `tools_mod.execute`（或 `ai.llm_provider.chat`），**不走真实 LLM**，断言 session 字段流转：

| 用例 | 构造 | 关键断言 |
|---|---|---|
| quick | router 判 quick | plan/decompose/review 标 `skipped`；summarize `done`；`finished=True` |
| normal·通过 | 1 轮 plan；reviewer `passed=True` | `iteration_count=1`；`loop_counter=1`；review 标 done「复审通过」 |
| normal·未过 | reviewer 连续 `passed=False` | 达 `max_rounds=1` ⇒ `iteration_limit=True` + 文案；`iteration_count=1` |
| deep·烧满 | reviewer 连续 fail | 跑满 3 轮；`loop_counter=3`；`iteration_count=3`；`iteration_limit=True`；planner_notes 含 3 条规划记录 |
| deep·提前结束 | 第 2 轮 reviewer pass | `iteration_count=2`；第 3 轮未执行 |
| deep·零证据 | 工具返回空 | `review_skipped=True`；review 标 `skipped`「无检索证据，跳过复审」 |
| fallback | `enable_planner=False` | plan/decompose/review 标 `skipped`；summarize 带 analyze 前缀 |
| 首轮不可用 | `plan` 首轮返回 `None` | `_run_planner_loop → False`；`planner_fallback=True`；走 fallback；**review 未执行** |

断言粒度：steps 状态与 detail 文案、`loop_counter`、`iteration_count`、`tool_calls`、`planner_notes`、`review_notes`、`result` 字段、`finished` / `error`。

该套测试同时是 Phase 1 的回归基线**和** Phase 2 的迁移护栏。

---

## 6. 分阶段交付与验收标准

**Phase 0 · 前置（动代码之前必须绿）**
1. 探针实测 LangGraph state 引用语义（决定 §4.2 用首选还是兜底写法）；
2. 落 `tests/test_orchestrator_paths.py`，**对现状硬编码 `while` 跑绿**。

**Phase 1 · 控制流迁移**
1. 四路径测试全绿（与 Phase 0 同一套断言）；
2. `pytest tests/ -q` 全量 25+ 项绿；
3. 全量 `eval/run_eval.py` 对照基线 `eval/record/测试记录_20261019_111331.txt`（命中 14/23、总 token 90397、reviewer 通过 2/13）——**行为等价 ⇒ 指标应逐项一致**，任何漂移须先定位根因再决定是否接受。

**Phase 2 · 存储迁移**（Phase 1 绿后另起 spec/plan）

> 提交策略：每阶段验证通过后单独提交，由用户确认后再提交（不自动提交）。

---

## 7. 风险与对策

| 风险 | 对策 |
|---|---|
| LangGraph 对 state deepcopy，导致 `_sessions` 与前端轮询看不到变更 | Phase 0 探针先实测；首选"节点显式回传 session 引用"，兜底"invoke 后带 `is not` 守卫回写" |
| 重构后评测指标漂移，无法判断是控制流还是存储导致 | D2 分两阶段 + D6 每阶段独立全量对照，单一变量归因 |
| 图的"无证据短路"挪进 review 节点，语义与原 `break` 不完全一致 | §4.4 第 6 条逐字锁定文案与状态；四路径用例含"零证据"专项断言 |
| 中文文案在搬运中被无意改写，污染评测记录与前端展示 | §4.4 全部 12 条逐字对照；测试断言 detail 文案字符串 |
| `langgraph` 未在依赖清单，他人克隆后缺包 | 补 `requirements.txt` 与 `pyproject.toml`（`langgraph>=1.1.9`） |
| Phase 2 的 checkpointer 无法序列化现有闭包 / datetime | §4.5 已列不可序列化项清单，Phase 2 spec 内解决 |

---

## 8. 文件布局

| 文件 | 动作 |
|---|---|
| `ai/orchestrator.py` | 新增 `QAGraphState` / `_build_qa_graph` / `_node_plan` / `_node_execute_tools` / `_node_review` / `_node_finalize` / `_route_after_plan` / `_route_after_review`；替换 `_run_planner_loop`。其余函数不动 |
| `tests/test_orchestrator_paths.py` | 新增（Phase 0 落盘） |
| `requirements.txt` + `pyproject.toml` | 补 `langgraph>=1.1.9` |
| `doc/变更文档/LangGraph编排重构设计.md` | 本文件 |
| `doc/项目文档/文档变更记录.md` | 实施后登记本次变更 |
