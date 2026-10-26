# LangGraph 编排重构（Planner 主-子闭环）· 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `ai/orchestrator.py::_run_planner_loop()` 的硬编码 `while` 控制流，等价替换为 LangGraph `StateGraph`（4 节点 + 2 条条件边），**行为零变化**（同一批 helper、同一 LLM 层、同一终止语义）。

**Architecture:** 分两阶段，单一变量归因。**Phase 0** 先落 8 用例四路径集成测试，对现状硬编码 `while` 跑绿（回归锚点），并实测 LangGraph state 引用语义；**Phase 1** 只迁**控制流**（state 仍落进程内 `_sessions`），节点复用现有 helper，`_run_planner_loop` 收成"图的壳"，对外签名与返回值语义不变；**Phase 2**（另起 spec/plan）才迁存储到 checkpointer。

**Tech Stack:** Python 3.11 / `langgraph>=1.1.9` / pytest / 现有 `ai/*` 编排栈（**不引入 `langchain-openai` 的 `ChatOpenAI`**，LLM 仍走 `ai/llm_provider.chat()`）

**依据文档：** [LangGraph编排重构设计.md](file:///d:/实习/my_project/deep_search_pro-master/doc/变更文档/LangGraph编排重构设计.md)（spec v1.0，已评审通过）

---

## Global Constraints

以下为 spec 的全局要求，**每个任务的隐含前提**，精确值逐字取自 spec：

- **迁移范围只含 A**：只动 `_run_planner_loop`，`_run_quick` / `_run_fallback` / `_run_review_pipeline` / `ask` / `ask_review` / `get_steps` / `get_result` / `_cleanup_expired` **一行不改**（前端契约零变更）。
- **Phase 1 只迁控制流**：会话状态仍存 `_sessions`（进程内 dict），**不引入 checkpointer**。
- **节点只做调度**：复用现有 helper —— `planner_mod.plan` / `tools_mod.execute` / `_apply_plan` / `_execute_tool_calls` / `_do_review` / `_flush_retrieval_nodes` / `_step_summarize` / `_evidence_summary` / `_suggestion_text` / `_mark` / `_step`，**不复制其业务逻辑**。
- **中文文案逐字搬运**：spec §4.4 的 12 条行为等价清单是硬验收，任何文案改动即行为变更（见下文「行为等价性清单（逐字锁定）」）。
- **不改**检索策略 / 提示词 / 工具层 / `ai/llm_provider.py` / reviewer 入参语义。
- **依赖下限**：`langgraph>=1.1.9`，同步补入 `requirements.txt` 与 `pyproject.toml` 的 `dependencies`（两处必须一致）。
- **图在模块级编译一次复用**，不带 checkpointer：`_QA_GRAPH = _build_qa_graph()`。
- **验收门禁**：`tests/test_orchestrator_paths.py` 8 用例全绿 → `pytest tests/ -q` 全量绿（25+ 项）→ 全量 `eval/run_eval.py` 对照基线 `eval/record/测试记录_20261019_111331.txt`（命中 **14/23**、总 token **90397**、reviewer 通过 **2/13**）。**行为等价 ⇒ 指标应逐项一致**，任何漂移须先定位根因再决定是否接受。
- **单一变量原则**：Phase 1 与 Phase 2 各自独立全量对照，不得同轮混改。
- **提交策略**：每阶段验证通过后单独提交，**由用户确认后再提交（不自动提交）**。
- **环境**：解释器 `D:\Anaconda\envs\shixi\python.exe`；工作目录 `d:\实习\my_project\deep_search_pro-master`；shell 为 **PowerShell 5（不支持 `&&`）**，多命令用 `;` 分隔或分次执行；命令前加 `$env:PYTHONIOENCODING="utf-8"`。

---

## 行为等价性清单（逐字锁定 · spec §4.4）

改造后必须逐条成立的语义（文案进评测记录与前端展示，**改字即变更**）：

| # | 必须保持的语义 | 改造后位置 |
|---|---|---|
| 1 | `max_rounds`：normal=1 / deep=3（`MAX_ITERATION`） | `_run_planner_loop` 组装入参时算 |
| 2 | 首轮入参 `plan(query, 1, _evidence_summary(session), "")` | `_node_plan` 首轮分支 |
| 3 | `_apply_plan` 调用时机（首轮 / retry 轮成功各一次） | `_node_plan` 两个分支 |
| 4 | retry 轮"先自增 `loop_counter` 再调 plan"的顺序 | `_node_plan` retry 分支（先置后调） |
| 5 | `_execute_tool_calls` → `tool_calls` 记录 + `trace.tool` | `_node_execute_tools` 原样调用 |
| 6 | 零证据：文案「所有检索工具无结果，基于现有证据结束（证据不足）」 + `review_skipped=True` + review 标 `skipped`「无检索证据，跳过复审」 + 结束 | `_node_review` 短路分支 |
| 7 | `_do_review` 异常 → `review_notes=None`、`review_skipped=True`、review 标 `failed` | `_do_review` 未改，`_node_review` 原样调用 |
| 8 | 通过判定 `note and note.get("passed")` | `_node_review` |
| 9 | 达上限：`iteration_limit=True` + 文案「达到迭代上限（N 轮），强制结束」 | `_node_review` |
| 10 | retry 轮 plan 失败：文案「第 N 轮规划失败，基于现有证据产出」 | `_node_plan` retry 分支 |
| 11 | 收尾：`iteration_count=round_no` + `_flush_retrieval_nodes` + `_step(summarize)` | `_node_finalize` |
| 12 | 首轮不可用：文案「Planner 不可用（输出解析失败 / 无 function-call 模型），回退 v3 固定流水线」 + `_mark(plan, failed)「Planner 不可用，回退 v3 固定流水线」` + `return False`（**不走 finalize**） | `_node_plan` 首轮分支 + `plan → END` 边 |

---

## Phase 0 · 回归基线（动代码之前必须绿）

### Task 1: LangGraph state 引用语义探针（已执行 · 仅留档）

**Files:**
- 无（一次性探针脚本，仓库外执行、跑完即删；**不提交任何文件**）

**Interfaces:**
- Consumes: 无
- Produces: 结论 `no-deepcopy` —— 决定 §4.2 用「首选写法（节点显式回传 session 引用）」，**不需要** `is not` 守卫兜底

- [x] **Step 1: 运行探针脚本**

```python
from typing import TypedDict
from langgraph.graph import END, StateGraph

class S(TypedDict):
    session: dict
    n: int

seen = {}
sess = {"k": 0}

def node_a(state):
    seen["a_is_orig"] = state["session"] is sess
    state["session"]["x"] = 1
    return {"session": state["session"], "n": state["n"] + 1}

def node_b(state):
    seen["b_is_orig"] = state["session"] is sess
    return {}

g = StateGraph(S)
g.add_node("a", node_a); g.add_node("b", node_b)
g.set_entry_point("a"); g.add_edge("a", "b"); g.add_edge("b", END)
out = g.compile().invoke({"session": sess, "n": 0})
print("a_is_orig", seen["a_is_orig"], "b_is_orig", seen["b_is_orig"],
      "out_is_orig", out["session"] is sess, "sess_x", sess.get("x"))
```

Run: `D:\Anaconda\envs\shixi\python.exe <探针脚本路径>`

Expected（**本机实测 2026-10-19 已确认**）：`a_is_orig True b_is_orig True out_is_orig True sess_x 1 out_x 1`

- [x] **Step 2: 记录结论**

LangGraph 1.1.9 的 `invoke` **不对入参 state 做 deepcopy**：首个节点拿到的 `session` 即调用方传入的**同一对象**，返回值中的 `session` 也是同一对象。⇒ Phase 1 节点内直接原地改 `session` 即可，`_sessions[session_id]` 与前端轮询正常可见；节点仍显式 `return {"session": session, ...}`（显式锁引用，兼作意图声明），但**无需** invoke 后回写兜底。

---

### Task 2: 落 `tests/test_orchestrator_paths.py`，对现状硬编码 `while` 跑绿

**Files:**
- Create: `tests/test_orchestrator_paths.py`
- 只读参考：`ai/orchestrator.py`、`ai/planner.py`、`ai/tools.py`、`ai/rewrite.py`、`ai/router_agent.py`、`ai/retriever.py`、`ai/analysts.py`、`ai/summarizer.py`

**Interfaces:**
- Consumes: `orchestrator.STEPS`（8 节点定义）、`orchestrator._run_pipeline(session)`、`orchestrator._run_planner_loop(session) -> bool`
- Produces: 8 个 pytest 用例 + 夹具 `stub` / `_make_session`；本文件同时是 Phase 1 的回归基线**和** Phase 2 的迁移护栏

- [ ] **Step 1: 写测试文件**

```python
# 用例：QA 编排四路径集成测试（quick / normal / deep / fallback）——对现状硬编码 while 跑绿
# 纯集成测试：monkeypatch 改写 / 路由 / 规划 / 工具 / 检索 / 分析师 / 汇总，不走真实 LLM 与数据库
# 该套测试既是本次 LangGraph 重构的回归基线，也是后续存储迁移（Phase 2）的护栏
# 断言粒度：steps 状态与 detail 文案、loop_counter、iteration_count、tool_calls、
#          planner_notes、review_notes、result 字段、finished / error
import datetime as _dt

import pytest

from ai import orchestrator


# ---------- 夹具 ----------

def _make_session(**over) -> dict:
    """按 ask() 的会话形态构造（不启后台线程，测试内同步驱动 _run_pipeline）"""
    s = {
        "session_id": "test-sess",
        "user_id": 1,
        "query": "注意力机制在哪些任务上有效",
        "mode": "phrase",
        "rewrite": True,
        "enable_planner": True,
        "complexity": "normal",
        "query_struct": None,
        "routes": [],
        "evidence": [],
        "analyst_outputs": {},
        "loop_counter": 0,
        "tool_calls": [],
        "review_notes": None,
        "planner_notes": [],
        "iteration_count": 0,
        "planner_fallback": False,
        "steps": [{"key": k, "label": lb, "status": "pending", "detail": ""}
                  for k, lb in orchestrator.STEPS],
        "result": None,
        "finished": False,
        "error": None,
        "created_at": _dt.datetime.now(),
    }
    s["_memory_records"] = lambda: []
    s.update(over)
    return s


def _ev(i: int) -> dict:
    """一条库内证据（title 唯一以通过 merge_evidence 的标题去重）"""
    return {"id": str(i), "title": f"论文{i}", "abstract": "摘要", "origin": "library",
            "source": "arXiv", "journal": "J", "published_at": "2025", "url": ""}


def _plan(thought: str = "规划说明", calls=None, subtasks=None) -> dict:
    """一份 Planner 输出（默认带 1 个 library_search 调用）"""
    return {"thought": thought, "subtasks": subtasks or [],
            "tool_calls": calls if calls is not None else [{"tool": "library_search", "args": {}}]}


def _step(session: dict, key: str) -> dict:
    return next(s for s in session["steps"] if s["key"] == key)


@pytest.fixture()
def stub(monkeypatch):
    """桩件装配：把四路径上所有外部依赖替换为可控返回值，返回控制板 board"""
    board = {
        "complexity": "normal",     # router 判定的档位
        "routes": ["library"],      # 检索通道
        "plans": [],                # planner_mod.plan 依次弹出的返回值（空则返回 None）
        "evidence": [_ev(1), _ev(2)],  # 检索工具返回的证据
        "reviews": [],              # run_reviewer 依次弹出的 passed 布尔（空则 False）
        "review_calls": 0,          # run_reviewer 被调用次数
    }

    def fake_rewrite(query, mode="phrase", enabled=True):
        return {"query": query, "rewritten": False, "keywords": ["attention"],
                "filters": {"year_from": "", "year_to": "", "sources": [], "cats": []}}

    def fake_route(question, keywords=None):
        return {"routes": board["routes"], "keywords": keywords or [],
                "confidence": 0.9, "reason": "测试桩", "complexity": board["complexity"]}

    def fake_plan(question, round_no, evidence_summary, review_suggestion=""):
        return board["plans"].pop(0) if board["plans"] else None

    def fake_execute(name, args, ctx):
        if name == "run_reviewer":
            board["review_calls"] += 1
            passed = board["reviews"].pop(0) if board["reviews"] else False
            return {"ok": True, "brief": "复审", "error": "",
                    "data": {"passed": passed, "defects": [], "suggestions": []}}
        if name in ("library_search", "web_search", "memory_search"):
            return {"ok": True, "brief": "命中", "error": "", "data": board["evidence"]}
        return {"ok": True, "brief": "其它工具", "error": "", "data": None}

    def fake_synthesize(question, evidence, analyst_outputs=None):
        return {"answer": "回答正文", "points": ["要点1"], "citations": []}

    monkeypatch.setattr(orchestrator.rewrite_mod, "rewrite_query", fake_rewrite)
    monkeypatch.setattr(orchestrator.router_agent, "route", fake_route)
    monkeypatch.setattr(orchestrator.planner_mod, "plan", fake_plan)
    monkeypatch.setattr(orchestrator.tools_mod, "execute", fake_execute)
    monkeypatch.setattr(orchestrator.retriever_mod, "search_library", lambda *a, **k: board["evidence"])
    monkeypatch.setattr(orchestrator.retriever_mod, "search_web", lambda *a, **k: board["evidence"])
    monkeypatch.setattr(orchestrator.retriever_mod, "search_memory", lambda *a, **k: board["evidence"])
    monkeypatch.setattr(orchestrator.analysts_mod, "run_analysts",
                        lambda evidence, keys=None: {"method": None, "critic": None, "compare": None})
    monkeypatch.setattr(orchestrator.summarizer, "synthesize", fake_synthesize)
    return board


# ---------- ① quick 快速通道 ----------

def test_quick_path_skips_planner_and_reviewer(stub):
    stub["complexity"] = "quick"
    s = _make_session()
    orchestrator._run_pipeline(s)

    assert _step(s, "plan")["status"] == "skipped"
    assert _step(s, "plan")["detail"] == "快速通道跳过规划调度"
    assert _step(s, "decompose")["status"] == "skipped"
    assert _step(s, "decompose")["detail"] == "快速通道跳过规划调度"
    assert _step(s, "review")["status"] == "skipped"
    assert _step(s, "review")["detail"] == "快速通道跳过复审校验"
    assert _step(s, "library")["status"] == "done"
    assert _step(s, "library")["detail"] == "命中 2 条"
    assert _step(s, "web")["status"] == "skipped" and _step(s, "web")["detail"] == "本次未调度"
    assert _step(s, "summarize")["status"] == "done"
    assert stub["review_calls"] == 0
    assert s["review_notes"] is None
    assert s["loop_counter"] == 0 and s["iteration_count"] == 0
    assert s["planner_notes"] == []
    assert s["planner_fallback"] is False
    assert s["finished"] is True and s["error"] is None


# ---------- ② normal · 复审通过（1 轮收敛） ----------

def test_normal_path_passes_in_one_round(stub):
    stub["plans"] = [_plan()]
    stub["reviews"] = [True]
    s = _make_session()
    orchestrator._run_pipeline(s)

    assert s["error"] is None and s["finished"] is True
    assert s["planner_fallback"] is False
    assert s["iteration_count"] == 1 and s["loop_counter"] == 1
    assert _step(s, "plan")["detail"] == "第 1/1 轮｜规划说明"
    assert _step(s, "decompose")["detail"] == "单步任务，无需拆解"
    assert _step(s, "library")["detail"] == "累计证据 2 条"
    assert _step(s, "review")["status"] == "done" and _step(s, "review")["detail"] == "复审通过"
    assert _step(s, "web")["detail"] == "本轮未调度"      # _flush_retrieval_nodes
    assert s["review_skipped"] is not True and s["iteration_limit"] is not True
    assert s["planner_notes"] == ["规划（第 1 轮）：规划说明"]
    assert [t["tool"] for t in s["tool_calls"]] == ["library_search"]
    assert s["result"]["answer"] == "回答正文"           # 无质检/迭代告警前缀
    assert stub["review_calls"] == 1


# ---------- ③ normal · 复审未过（达 max_rounds=1 强制结束） ----------

def test_normal_path_iteration_limit(stub):
    stub["plans"] = [_plan()]
    stub["reviews"] = [False]
    s = _make_session()
    orchestrator._run_pipeline(s)

    assert s["error"] is None
    assert s["iteration_limit"] is True
    assert s["iteration_count"] == 1 and s["loop_counter"] == 1
    assert "达到迭代上限（1 轮），强制结束" in s["planner_notes"]
    assert stub["review_calls"] == 1
    assert "分析已达到轮次上限" in s["result"]["answer"]   # 迭代告警前缀


# ---------- ④ deep · 复审连续 fail，烧满 3 轮 ----------

def test_deep_path_burns_three_rounds(stub):
    stub["complexity"] = "deep"
    stub["plans"] = [_plan("第1轮决策"), _plan("第2轮决策"), _plan("第3轮决策")]
    stub["reviews"] = [False, False, False]
    s = _make_session()
    orchestrator._run_pipeline(s)

    assert s["error"] is None
    assert s["loop_counter"] == 3 and s["iteration_count"] == 3
    assert s["iteration_limit"] is True
    assert "达到迭代上限（3 轮），强制结束" in s["planner_notes"]
    assert len([n for n in s["planner_notes"] if n.startswith("规划（第")]) == 3
    assert stub["review_calls"] == 3
    assert len(s["tool_calls"]) == 3            # 每轮各 1 次工具调用


# ---------- ⑤ deep · 第 2 轮复审通过，提前结束 ----------

def test_deep_path_early_pass(stub):
    stub["complexity"] = "deep"
    stub["plans"] = [_plan("第1轮决策"), _plan("第2轮决策")]
    stub["reviews"] = [False, True]
    s = _make_session()
    orchestrator._run_pipeline(s)

    assert s["error"] is None
    assert s["iteration_count"] == 2 and s["loop_counter"] == 2
    assert _step(s, "review")["detail"] == "复审通过"
    assert stub["review_calls"] == 2
    assert s["iteration_limit"] is not True
    assert len(s["planner_notes"]) == 2


# ---------- ⑥ deep · 零证据短路（工具无结果） ----------

def test_deep_path_no_evidence_short_circuit(stub):
    stub["complexity"] = "deep"
    stub["plans"] = [_plan("先不检索", calls=[])]     # 本轮不调工具 → 证据为空
    s = _make_session()
    orchestrator._run_pipeline(s)

    assert s["error"] is None
    assert s["evidence"] == []
    assert s["review_skipped"] is True
    assert _step(s, "review")["status"] == "skipped"
    assert _step(s, "review")["detail"] == "无检索证据，跳过复审"
    assert "所有检索工具无结果，基于现有证据结束（证据不足）" in s["planner_notes"]
    assert stub["review_calls"] == 0
    assert s["iteration_count"] == 1
    assert "本次回答未经完整质检" in s["result"]["answer"]  # 质检告警前缀


# ---------- ⑦ fallback · 未启用 Planner（v3 固定流水线） ----------

def test_fallback_path_when_planner_disabled(stub):
    s = _make_session(enable_planner=False)
    orchestrator._run_pipeline(s)

    assert s["error"] is None and s["finished"] is True
    assert s["planner_fallback"] is True
    assert _step(s, "plan")["status"] == "skipped"
    assert _step(s, "plan")["detail"] == "回退 v3 固定流水线（未启用 / 不可用 Planner）"
    assert _step(s, "decompose")["status"] == "skipped"
    assert _step(s, "decompose")["detail"] == "回退 v3 固定流水线"
    assert s["review_skipped"] is True
    assert _step(s, "review")["detail"] == "未经过完整复审质检"
    assert _step(s, "summarize")["detail"] == "完成视角: 全部降级跳过｜要点 1 条，引用 0 处"
    assert stub["review_calls"] == 0


# ---------- ⑧ 首轮 Planner 不可用（_run_planner_loop → False） ----------

def test_first_round_planner_unavailable(stub):
    stub["plans"] = [None]                      # 首轮 plan 返回 None
    s = _make_session()

    assert orchestrator._run_planner_loop(s) is False
    assert _step(s, "plan")["status"] == "failed"
    assert _step(s, "plan")["detail"] == "Planner 不可用，回退 v3 固定流水线"
    assert s["planner_notes"] == [
        "Planner 不可用（输出解析失败 / 无 function-call 模型），回退 v3 固定流水线"]
    assert stub["review_calls"] == 0            # 复审从未执行
    assert s["iteration_count"] == 0            # 未走 finalize
```

- [ ] **Step 2: 跑测试确认全绿（红→绿的反向：本步是"对现状基线跑绿"）**

Run:

```powershell
$env:PYTHONIOENCODING="utf-8"; D:\Anaconda\envs\shixi\python.exe -m pytest tests/test_orchestrator_paths.py -v
```

Expected: **8 passed**（若任一条 fail，**先修测试/桩件**，不得带着红灯进入 Phase 1）

- [ ] **Step 3: 提交（需用户确认）**

```powershell
git add tests/test_orchestrator_paths.py
git commit -m "test(qa): 新增编排四路径集成测试（Phase 0 基线，对现状 while 跑绿）"
```

---

## Phase 1 · 控制流迁移到 LangGraph

### Task 3: 补依赖 `langgraph>=1.1.9`

**Files:**
- Modify: `requirements.txt`（末尾追加一行）
- Modify: `pyproject.toml`（`[project].dependencies` 追加一项）

**Interfaces:**
- Consumes: 无
- Produces: `from langgraph.graph import END, StateGraph` 在全新克隆环境可用

- [ ] **Step 1: 修改 `requirements.txt`**

在 `tavily-python>=0.5.0` 之后、`python-docx>=1.1.0` 之前插入一行（保持与 pyproject 同步）：

```
langgraph>=1.1.9
```

- [ ] **Step 2: 修改 `pyproject.toml`**

在 `dependencies` 列表的 `"tavily-python>=0.5.0",` 之后插入：

```toml
    "langgraph>=1.1.9",
```

- [ ] **Step 3: 核对已装版本**

Run:

```powershell
D:\Anaconda\envs\shixi\python.exe -m pip show langgraph
```

Expected: `Version: 1.1.9`（本机实测已装；若缺失则 `-m pip install "langgraph>=1.1.9"`）

---

### Task 4: 迁移 `_run_planner_loop` 为 LangGraph 图（TDD）

**Files:**
- Modify: `ai/orchestrator.py`（① 顶部 import；② 替换 `_run_planner_loop` 函数体；③ 文件尾部「编排辅助」段之后新增「Planner 闭环图」段）
- Test: `tests/test_orchestrator_paths.py`（本任务**新增 2 个结构用例**，其余 8 个为既有回归护栏）

**Interfaces:**
- Consumes: `planner_mod.plan(question, round_no, evidence_summary, review_suggestion) -> dict|None`；`_apply_plan(session, plan, round_no, max_rounds)`；`_execute_tool_calls(session, calls)`；`_do_review(session)`；`_flush_retrieval_nodes(session)`；`_step_summarize(session) -> str`；`_evidence_summary(session) -> str`；`_suggestion_text(note) -> str`；`_mark(session, key, status, detail)`；`_step(session, key, fn, prefix="")`
- Produces: 模块级 `QAGraphState`（TypedDict）/ `_build_qa_graph()` / `_QA_GRAPH`（已编译图对象）/ `_node_plan` / `_node_execute_tools` / `_node_review` / `_node_finalize` / `_route_after_plan` / `_route_after_review`；`_run_planner_loop(session: dict) -> bool`（签名与返回语义不变）

- [ ] **Step 1: 在测试文件末尾追加 2 个新 API 结构用例（RED）**

追加到 `tests/test_orchestrator_paths.py` 末尾：

```python
# ---------- 新图 API（Phase 1 新增；改造前应为红灯） ----------

def test_qa_graph_topology_has_four_nodes():
    """图在模块级编译一次，含 4 个业务节点"""
    nodes = set(orchestrator._build_qa_graph().get_graph().nodes)
    assert {"plan", "execute_tools", "review", "finalize"} <= nodes
    assert orchestrator._QA_GRAPH is orchestrator._QA_GRAPH


def test_qagraphstate_fields():
    """QAGraphState 字段集固定为 6 项"""
    assert set(orchestrator.QAGraphState.__annotations__) == {
        "session", "round_no", "max_rounds", "plan", "note", "outcome"}
```

- [ ] **Step 2: 跑测试确认新用例 FAIL（RED）**

Run:

```powershell
$env:PYTHONIOENCODING="utf-8"; D:\Anaconda\envs\shixi\python.exe -m pytest tests/test_orchestrator_paths.py -v
```

Expected: 新增 2 条 **FAIL/ERROR**（`AttributeError: module 'ai.orchestrator' has no attribute '_build_qa_graph'` / `'QAGraphState'`），其余 8 条 **PASS**

- [ ] **Step 3: 改 `ai/orchestrator.py` —— 顶部 import**

在 `import threading` 之前插入：

```python
from typing import TypedDict

from langgraph.graph import END, StateGraph
```

（与既有 import 段合并后形如：`from typing import TypedDict` → 空行 → `from langgraph.graph import END, StateGraph` → 既有 stdlib/三方/本项目 import）

- [ ] **Step 4: 改 `ai/orchestrator.py` —— 替换 `_run_planner_loop`（L236–L283 整段）**

用图壳替换原 `while` 实现（**逐字保留 docstring 语义**）：

```python
def _run_planner_loop(session: dict) -> bool:
    """Planner 主-子闭环（v4 §4.1）——LangGraph 图壳，控制流见 _build_qa_graph()

    :return: True=已在闭环内完成产出；False=Planner 首轮不可用，调用方回退 v3
    """
    final = _QA_GRAPH.invoke({
        "session": session,
        "round_no": 1,
        "max_rounds": 1 if session.get("complexity") == "normal" else MAX_ITERATION,
        "plan": None,
        "note": None,
        "outcome": "",
    })
    return final["outcome"] != "planner_unavailable"
```

- [ ] **Step 5: 改 `ai/orchestrator.py` —— 在「编排辅助」段（`_suggestion_text`）之后新增「Planner 闭环图」段**

```python
# ---------- Planner 闭环图（LangGraph · 控制流声明式化） ----------
# 与手写 while 严格等价：4 节点（plan / execute_tools / review / finalize）+ 2 条条件边
#   （plan 出边 3 分支 end/finalize/execute_tools；review 出边 2 分支 plan/finalize）
# 状态桥接（Phase 1）：只迁控制流，session dict 仍为唯一状态载体（_sessions 不迁、无 checkpointer）
# 引用语义：实测 langgraph 1.1.9 的 invoke 不对入参 state 做 deepcopy——
#           节点内拿到的 session 即 _sessions 中的同一对象；节点显式 return {"session": session} 锁引用

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
    note = session.get("review_notes")
    if note and note.get("passed"):
        return {"session": session, "note": note, "outcome": "passed"}
    if round_no >= state["max_rounds"]:
        session["iteration_limit"] = True
        session["planner_notes"].append(
            f"达到迭代上限（{state['max_rounds']} 轮），强制结束")
        return {"session": session, "note": note, "outcome": "iteration_limit"}
    session["loop_counter"] = round_no + 1
    return {"session": session, "note": note, "round_no": round_no + 1,
            "outcome": "retry"}


def _node_finalize(state: QAGraphState) -> dict:
    """收尾节点：落迭代次数 → 补齐检索节点状态 → 汇总作答"""
    session = state["session"]
    session["iteration_count"] = state["round_no"]
    _flush_retrieval_nodes(session)
    _step(session, "summarize", _step_summarize)
    return {"session": session}


def _route_after_plan(state: QAGraphState) -> str:
    """plan 出边路由：首轮不可用 → END；第 N(>1) 轮规划失败 → finalize；否则 → execute_tools"""
    return {"planner_unavailable": "end", "plan_degraded": "finalize"}.get(
        state["outcome"], "execute_tools")


def _route_after_review(state: QAGraphState) -> str:
    """review 出边路由：retry → plan；否则（无证据/通过/达上限）→ finalize"""
    return "plan" if state["outcome"] == "retry" else "finalize"


def _build_qa_graph():
    """装配并编译 Planner 主-子闭环图（Phase 1 不带 checkpointer）"""
    graph = StateGraph(QAGraphState)
    graph.add_node("plan", _node_plan)
    graph.add_node("execute_tools", _node_execute_tools)
    graph.add_node("review", _node_review)
    graph.add_node("finalize", _node_finalize)
    graph.set_entry_point("plan")
    graph.add_conditional_edges("plan", _route_after_plan,
                                {"end": END, "finalize": "finalize",
                                 "execute_tools": "execute_tools"})
    graph.add_edge("execute_tools", "review")
    graph.add_conditional_edges("review", _route_after_review,
                                {"plan": "plan", "finalize": "finalize"})
    graph.add_edge("finalize", END)
    return graph.compile()


_QA_GRAPH = _build_qa_graph()
```

- [ ] **Step 6: 跑测试确认全部 PASS（GREEN）**

Run:

```powershell
$env:PYTHONIOENCODING="utf-8"; D:\Anaconda\envs\shixi\python.exe -m pytest tests/test_orchestrator_paths.py -v
```

Expected: **10 passed**（8 回归 + 2 结构）

- [ ] **Step 7: 跑全量特征测试**

Run:

```powershell
$env:PYTHONIOENCODING="utf-8"; D:\Anaconda\envs\shixi\python.exe -m pytest tests/ -q
```

Expected: **全绿**（既有 25 项 + 新增 10 项）；无任何 `failed/error`

- [ ] **Step 8: 提交（需用户确认）**

```powershell
git add ai/orchestrator.py tests/test_orchestrator_paths.py requirements.txt pyproject.toml
git commit -m "refactor(qa): Planner 主-子闭环控制流迁移至 LangGraph（4 节点 + 3 条件边，行为等价）"
```

---

### Task 5: Phase 1 全量评测对照（单一变量验收门禁）

**Files:**
- 只读：`eval/run_eval.py`、`eval/cases.json`（如存在）
- 产出：`eval/record/测试记录_<新时间戳>.txt`（`eval/record/` 已被 gitignore，不入库）

**Interfaces:**
- Consumes: Task 4 改造后的 `ai/orchestrator.py`
- Produces: 与基线 `eval/record/测试记录_20261019_111331.txt` 的逐项指标对照结论

- [ ] **Step 1: 跑全量评测**

Run:

```powershell
$env:PYTHONIOENCODING="utf-8"; D:\Anaconda\envs\shixi\python.exe eval/run_eval.py
```

Expected: 生成新测试记录（含自动生成的「测试配置」段）

- [ ] **Step 2: 逐项对照基线**

对照 `eval/record/测试记录_20261019_111331.txt`：

| 指标 | 基线值 | 期望 |
|---|---|---|
| 期望引用命中率 | 14/23 | **一致**（行为等价） |
| 总 token | 90397 | **一致**（±0 为期望） |
| reviewer 通过数 | 2/13 | **一致** |

- [ ] **Step 3: 漂移处置**

若任一指标漂移：**停止后续步骤**，按 §7 风险表定位根因（控制流 vs 存储），确认属实现缺陷则回退 Task 4 修复后重跑；属 LLM 非确定性波动（Q8 已知波动项）则在记录中标注并交由用户裁定是否接受。

- [ ] **Step 4: 汇报结论（不提交评测记录）**

向用户汇报三项指标对照结果，等待用户确认 Phase 1 通过。

---

### Task 6: 登记文档变更 + Phase 1 收尾

**Files:**
- Modify: `doc/项目文档/文档变更记录.md`（追加本次变更条目）

**Interfaces:**
- Consumes: Task 4 的实现、Task 5 的评测结论
- Produces: 文档与代码状态一致

- [ ] **Step 1: 在 `doc/项目文档/文档变更记录.md` 追加条目**

在文末新增一节，登记：变更名称「LangGraph 编排重构（Phase 1 控制流迁移）」、日期、涉及文件（`ai/orchestrator.py` / `tests/test_orchestrator_paths.py` / `requirements.txt` / `pyproject.toml`）、关联设计文档链接、验收结论（四路径 10 用例绿 / pytest 全量绿 / 全量 eval 逐项一致）。

- [ ] **Step 2: 提交（需用户确认）**

```powershell
git add doc/项目文档/文档变更记录.md
git commit -m "docs: 登记 LangGraph 编排重构（Phase 1）变更"
```

---

## Phase 2 · 存储迁移（概要，不在本计划内实施）

Phase 1 绿后**另起 spec/plan**（spec §4.5）：`state schema 化`（TypedDict 对齐 session 键名 + append 语义 reducer）→ `清掉不可序列化项`（`_memory_records` 闭包 lambda 改为直接调用；`datetime` 交默认序列化器）→ `checkpointer`（`MemorySaver` 起步，`thread_id=session_id`）→ `接口层`（`get_steps`/`get_result` 读 `graph.get_state()` 映射回冻结的响应 schema）→ `验收`（四路径绿 + 响应契约快照 + 全量 eval 对照）。本计划**不含**任何 Phase 2 工作。

---

## 自审记录（Self-Review）

**1. Spec 覆盖**：spec §4.1 图拓扑 → Task 4 Step 5（4 节点 + 3 条件边装配，逐条对应路由表）；§4.2 State 与桥接 → Task 4 Step 3/5 + 探针 Task 1（结论 no-deepcopy）；§4.3 节点职责 → Task 4 Step 5（每个节点业务逐字搬运）；§4.4 十二条等价性 → 上文「行为等价性清单」+ Task 2 的 8 用例断言；§5 测试设计 8 用例 → Task 2/4（其中「首轮不可用」= `test_first_round_planner_unavailable`，「fallback」= `test_fallback_path_when_planner_disabled`，「deep 零证据」= `test_deep_path_no_evidence_short_circuit`）；§6 分阶段验收 → Task 5；§7 风险（探针 / 指标漂移 / 无证据短路 / 文案 / 依赖 / Phase 2 序列化）→ Task 1/4/5 + Global Constraints + Phase 2 段；§8 文件布局 → Task 2/3/4/6 的 Files 清单。

**2. 占位符扫描**：全文无 `TBD` / `TODO` / 「稍后补充」/「同类处理」；每个 code step 均给出可整段粘贴的代码；每条 `Run` 均给出完整命令与 Expected。

**3. 类型一致性**：`QAGraphState` 6 字段（`session/round_no/max_rounds/plan/note/outcome`）在结构用例、节点 return、`_run_planner_loop` 组装入参三处一致；`outcome` 取值域 `"" / planned / planner_unavailable / plan_degraded / no_evidence / passed / iteration_limit / retry` 在 `_node_*` 写入与两个路由函数读出处一致；节点返回的 key 全部落在 schema 内（`loop_counter` 直接写 `session`，**不进** state，避免未知 channel）。

**4. 已知取舍**：本改为**行为保持型重构**，Phase 0 的 8 用例是"对现状跑绿"的特征化测试（非红-绿），Task 4 另以 2 条新 API 结构用例提供 RED→GREEN 循环；此取舍已在 Task 2 Step 2 与 Task 4 Step 1/2 明确标注。
