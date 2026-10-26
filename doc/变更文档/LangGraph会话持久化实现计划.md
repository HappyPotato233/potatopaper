# LangGraph 会话持久化（Phase 3）· 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 QA / 综述会话的 checkpointer 由进程内 `InMemorySaver` 换成**落盘的 `SqliteSaver`**，并让 `_sessions` 索引在**进程重启后从 checkpointer 重建**，从而实现「重启后旧会话仍可读」；对外契约（前端响应 schema / API 路径 / 步骤文案 / 评测口径）**一行不改**。

**Architecture:** 模块级 saver 单例换成 `SqliteSaver(sqlite3.connect(_QA_DB_PATH, check_same_thread=False))`（`_QA_DB_PATH` = 环境变量 `LITRADAR_QA_DB`，缺省 `data/qa_checkpoints.sqlite3`）；新增 `_rebuild_index()` 在模块导入时扫 `saver.list(None)` 重建 `_sessions`；接口层函数（`get_steps`/`get_result`/`get_raw`/`_memory_records`/`_cleanup_expired`/`_load_session`）**一字不改**。

**Tech Stack:** Python 3.12 / `langgraph>=1.1.9` / **`langgraph-checkpoint-sqlite>=3.1.2`（新增）** / `sqlite3` 标准库 / pytest / 现有 `ai/*` 编排栈（**不引入 `ChatOpenAI`**）

**依据文档：** [LangGraph会话持久化设计.md](file:///d:/实习/my_project/deep_search_pro-master/doc/变更文档/LangGraph会话持久化设计.md)（Phase 3 spec v1.0，用户已裁定 D1/D2/D3）

---

## Global Constraints

以下为 spec 的全局要求，**每个任务的隐含前提**，精确值逐字取自 spec：

- **交付边界（D3）**：**仅持久化基础**。不做断点续跑自动恢复、不做 `interrupt()` 人工介入、**不新增任何对外 API**（`get_state_history` 只作数据基础，不暴露）。
- **契约冻结**：`app/*` 与 `web/*` **一行不改**；`get_steps` / `get_result` 字段名 / 类型 / 缺省值逐字不变（见下文「契约冻结清单」）。
- **接口层零改动**：`get_steps` / `get_result` / `get_raw` / `_memory_records` / `_cleanup_expired` / `_load_session` / `ask` / `ask_review` / `_seed_pipeline` / `_seed_review` / `_safe_update` **一行不改**。
- **两图共用同一 saver**：`thread_id = f"qa:{sid}"` / `f"rev:{sid}"`（沿用 Phase 2，不变）。
- **state 单通道**：`{session: dict, round_no, max_rounds, plan, note, outcome}`，节点 `return {"session": session}`（沿用 Phase 2，不变）。
- **DB 路径（D4）**：相对项目根 `data/qa_checkpoints.sqlite3`；目录不存在则 `mkdir(parents=True, exist_ok=True)`；`data/` 追加 `.gitignore`。
- **连接形态（D5）**：模块级单例 `sqlite3.connect(path, check_same_thread=False)`；**不用 `from_conn_string`**（contextmanager 退出即关连接）。
- **测试隔离（D6）**：`tests/conftest.py` **顶层** `os.environ.setdefault("LITRADAR_QA_DB", ":memory:")`；`pytest` **不得**产生 `data/qa_checkpoints.sqlite3`。
- **`mode` 取值**：QA 会话为 `phrase` / `paragraph` / `strategy`（**不是** thread 前缀的 `qa`）；综述为 `review`。索引重建**以会话正文 `session["mode"]` 为准**，`thread_id` 前缀仅兜底。
- **不改** LLM 层 / 检索策略 / 提示词 / reviewer 入参语义 / `eval/run_eval.py` 对外口径。
- **`_safe_update` 保持原样**（静默吞异常，仅登记风险；spec §4.6）。
- **验收门禁**：`pytest tests/ -q` 全量绿（42 + 新增）→ 端到端重启实测通过 → 全量 `eval/run_eval.py` 落在同码历史波动带内 + 运行期零异常。
- **单一变量**：Phase 3 独立对照，不与其它变更混轮。
- **提交策略**：每阶段验证通过后单独提交，**由用户确认后再提交（不自动提交）**。
- **环境**：解释器 `D:\Anaconda\envs\shixi\python.exe`；工作目录 `d:\实习\my_project\deep_search_pro-master`；shell **PowerShell 5（不支持 `&&` / heredoc）**，多命令用 `;` 或分次；命令前加 `$env:PYTHONIOENCODING="utf-8"`。分支 `refactor/langgraph-persistence`。
- **依赖现状实测**：已装 `langgraph-checkpoint-sqlite 3.1.2`；**传递依赖 `langgraph-checkpoint` 由 4.1.1 升到 4.3.0**、新增 `sqlite-vec 0.1.9`；实测 `import ai.orchestrator` 正常、`pytest tests/ -q` = **42 passed**。

---

## 行为等价性清单（逐字锁定 · spec §5）

| # | 必须保持的语义 | Phase 3 是否触及 |
|---|---|---|
| 1 | 分档：quick→`quick`节点；normal/deep+`enable_planner`→`plan`；否则→`fallback` 节点 | **不触及**（图结构不变） |
| 2 | `max_rounds`：normal=1 / deep=`MAX_ITERATION` | 不触及 |
| 3 | 首轮 Planner 不可用 → 标 failed + note + 走 fallback | 不触及 |
| 4 | 未启用开关 → `planner_fallback=True` + 直接 fallback | 不触及 |
| 5 | retry 轮「先自增 `loop_counter` 再调 plan」 | 不触及 |
| 6 | 零证据短路「无检索证据，跳过复审」+ `review_skipped=True` | 不触及 |
| 7 | 达上限「达到迭代上限（N 轮），强制结束」+ `iteration_limit=True` | 不触及 |
| 8 | 收尾：`iteration_count=round_no` + `_flush_retrieval_nodes` + 质检/迭代告警前缀 | 不触及 |
| 9 | 异常不中断：`error` 落地 + `finished=True` | 不触及（`_safe_update` 语义不变） |
| 10 | 综述 4 节点 route→library→analyze→summarize，文案不变 | 不触及 |
| 11 | `_memory_records` 字段与过滤（finished + 有 result）不变 | 不触及 |
| 12 | **（新增能力）重启后旧会话可读** | **新增**（不改变 1–11） |

---

## 契约冻结清单（spec §4.4 · 一字不改）

**`get_steps` 返回**：`session_id` / `query` / `mode` / `complexity` / `steps`(list[dict]) / `finished` / `loop_counter` / `tool_calls`(list[dict]) / `review_notes`。

**`get_result` 返回**：`session_id` / `query` / `mode` / `complexity` / `rewritten_query` / `rewritten` / `points` / `citations` / `answer` / `planner_notes` / `iteration_count` / `finished` / `error`；`mode=="review"` 时附 `subscription_id` / `direction_name` / `paper_count` / `sections` / `outline`。

字段名 / 类型 / 缺省值逐字取自现 [orchestrator.py](file:///d:/实习/my_project/deep_search_pro-master/ai/orchestrator.py) 的 `get_steps`(L140–159) / `get_result`(L162–192)。**本阶段这两处一行不改。**

---

## Phase 3-a · 依赖与后端替换（最小且必绿）

### Task 1: 依赖入清单 + 测试隔离 + saver 换 `SqliteSaver`

**Files:**
- Modify: `requirements.txt` / `pyproject.toml`（补依赖）
- Modify: `tests/conftest.py`（顶层环境变量隔离）
- Modify: `ai/orchestrator.py`（import 与 saver 构造）
- Modify: `.gitignore`（追加 `data/`）
- Test: `tests/test_orchestrator_paths.py`（追加 T1 / T6）

**Interfaces:**
- Consumes: `app.config.PROJECT_ROOT`（已存在，`Path` 类型）
- Produces: 模块级 `orchestrator._QA_DB_PATH: str`；`orchestrator._PIPELINE_SAVER: SqliteSaver`（QA 图与综述图共用）

- [ ] **Step 1: 依赖入清单**

`requirements.txt` 在 `langgraph>=1.1.9` 之后追加一行：

```
langgraph-checkpoint-sqlite>=3.1.2
```

`pyproject.toml` 的 `dependencies` 同步追加同一字符串（逐字符一致，紧邻 `langgraph>=1.1.9`）。

- [ ] **Step 2: 测试隔离（D6）**

`tests/conftest.py` **顶层**（置于 `import os` 之后、其余 import 之前）新增：

```python
# Phase 3：会话 checkpointer 落盘路径覆盖为进程内内存库（零文件产物、零跨进程污染）
os.environ.setdefault("LITRADAR_QA_DB", ":memory:")
```

> 必须置于**模块顶层**：pytest 在收集测试模块**之前**导入 `conftest.py`，从而早于 `ai.orchestrator` 读取该环境变量。

- [ ] **Step 3: `.gitignore` 追加**

在「Python-generated files」段之后追加：

```
# Phase 3：LangGraph 会话 checkpointer 落盘目录
data/
```

- [ ] **Step 4: `ai/orchestrator.py` 换 saver**

改 import 段：

- **删除** `from langgraph.checkpoint.memory import InMemorySaver`
- **新增**（归入现有 import 区，顺序与文件风格一致）：

```python
import os
import sqlite3
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver

from app.config import PROJECT_ROOT
```

改 L53–56 段：

```python
# 会话最小索引：sid → {user_id, created_at, mode}；会话正文由 checkpointer 承载（Phase 2）
_sessions: dict[str, dict] = {}
_lock = threading.Lock()
_PIPELINE_SAVER = InMemorySaver()          # QA 图与综述图共用同一 saver 实例
```

替换为：

```python
# 会话最小索引：sid → {user_id, created_at, mode}；会话正文由 checkpointer 承载（Phase 3：落盘 SQLite）
_sessions: dict[str, dict] = {}
_lock = threading.Lock()

# 会话落盘位置（缺省相对项目根 data/qa_checkpoints.sqlite3；测试经 LITRADAR_QA_DB 覆盖为 :memory:）
_QA_DB_PATH = os.getenv("LITRADAR_QA_DB") or str(PROJECT_ROOT / "data" / "qa_checkpoints.sqlite3")
if _QA_DB_PATH != ":memory:":
    Path(_QA_DB_PATH).parent.mkdir(parents=True, exist_ok=True)
# QA 图与综述图共用同一 saver 实例（SqliteSaver 内部持 threading.Lock，支持跨 daemon 线程）
_PIPELINE_SAVER = SqliteSaver(sqlite3.connect(_QA_DB_PATH, check_same_thread=False))
```

> `_build_pipeline_graph()` / `_build_review_graph()` 内的 `checkpointer=_PIPELINE_SAVER` 引用**不变**。

- [ ] **Step 5: 追加 T1 / T6 用例（先 RED）**

在 `tests/test_orchestrator_paths.py` 末尾追加：

```python
# ---------- 持久化（Phase 3） ----------

def test_saver_is_sqlite():
    """模块级 saver 已换为落盘的 SqliteSaver"""
    from langgraph.checkpoint.sqlite import SqliteSaver
    assert isinstance(orchestrator._PIPELINE_SAVER, SqliteSaver)


def test_pytest_uses_memory_db():
    """D6 隔离生效：pytest 下会话库为 :memory:，不得落盘到 data/"""
    assert orchestrator._QA_DB_PATH == ":memory:"
```

Run:

```powershell
$env:PYTHONIOENCODING="utf-8"; D:\Anaconda\envs\shixi\python.exe -m pytest tests/test_orchestrator_paths.py -q
```

Expected: **RED**（`ModuleNotFoundError: langgraph.checkpoint.sqlite` 若依赖未装 / 或 `AttributeError: _QA_DB_PATH`）→ 完成 Step 1–4 后 **GREEN**。

- [ ] **Step 6: 全量测试 + 无落盘副作用核对**

Run:

```powershell
$env:PYTHONIOENCODING="utf-8"; D:\Anaconda\envs\shixi\python.exe -m pytest tests/ -q
```

Expected: **44 passed**（42 + T1 + T6）。

Run:

```powershell
Test-Path .\data
```

Expected: `False`（pytest 未产生 `data/` 目录）。

---

## Phase 3-b · 索引重建（核心新能力）

### Task 2: `_rebuild_index()` + 持久化用例 T2–T5

**Files:**
- Modify: `ai/orchestrator.py`（新增 `_rebuild_index()` 并在模块级调用）
- Test: `tests/test_orchestrator_paths.py`（追加 T2 / T3 / T4 / T5）

**Interfaces:**
- Consumes: `_PIPELINE_SAVER.list(None)`（`Iterator[CheckpointTuple]`，全局按 `checkpoint_id DESC`）；`CheckpointTuple.config / .checkpoint`
- Produces: `orchestrator._rebuild_index() -> None`（重建 `_sessions` 索引，幂等）

- [ ] **Step 1: 追加 T2–T5 用例（先 RED）**

在 `tests/test_orchestrator_paths.py` 末尾继续追加：

```python
def test_rebuild_index_from_checkpointer(stub):
    """索引缺失时，_rebuild_index 可从 checkpointer 重建（重启后可读的前提）"""
    stub["plans"] = [_plan()]
    stub["reviews"] = [True]
    s = _make_session(session_id="rb-1")
    orchestrator._sessions["rb-1"] = {"user_id": 1, "created_at": s["created_at"], "mode": "phrase"}
    orchestrator._run_pipeline(s)
    orchestrator._sessions.pop("rb-1")            # 模拟进程重启后索引为空
    orchestrator._rebuild_index()
    meta = orchestrator._sessions.get("rb-1")
    assert meta is not None
    assert meta["user_id"] == 1
    assert meta["mode"] == "phrase"
    assert isinstance(meta["created_at"], _dt.datetime)


def test_rebuild_index_maps_review_mode(stub):
    """综述会话（rev: 前缀）重建后 mode 为 review"""
    s = _make_session(session_id="rb-rev", mode="review")
    s["subscription_id"] = 9
    orchestrator._sessions["rb-rev"] = {"user_id": 1, "created_at": s["created_at"], "mode": "review"}
    orchestrator._seed_review(s)                  # 只落 checkpointer，不启线程
    orchestrator._sessions.pop("rb-rev")
    orchestrator._rebuild_index()
    assert orchestrator._sessions["rb-rev"]["mode"] == "review"


def test_session_readable_across_saver_instances(monkeypatch, tmp_path):
    """同一 DB 上换 saver 实例 + 重编译图（等价新进程），旧会话仍可读"""
    import sqlite3
    from langgraph.checkpoint.sqlite import SqliteSaver

    db = tmp_path / "ck.sqlite3"
    cfg = {"configurable": {"thread_id": "qa:x1"}}
    payload = {"session": {"session_id": "x1", "user_id": 3, "mode": "phrase",
                           "steps": [], "finished": False},
               "round_no": 1, "max_rounds": 0, "plan": None, "note": None, "outcome": ""}

    monkeypatch.setattr(orchestrator, "_PIPELINE_SAVER",
                        SqliteSaver(sqlite3.connect(str(db), check_same_thread=False)))
    g_a = orchestrator._build_pipeline_graph()
    orchestrator._safe_update(g_a, cfg, payload)
    assert g_a.get_state(cfg).values["session"]["user_id"] == 3

    # 模拟进程重启：新连接 + 新 saver + 重编译图
    monkeypatch.setattr(orchestrator, "_PIPELINE_SAVER",
                        SqliteSaver(sqlite3.connect(str(db), check_same_thread=False)))
    g_b = orchestrator._build_pipeline_graph()
    back = g_b.get_state(cfg).values
    assert back["session"]["user_id"] == 3
    assert back["session"]["session_id"] == "x1"


def test_history_frames_available_for_replay(stub):
    """按轮回放的数据基础：历史帧数 ≥ 本次运行的节点数"""
    stub["plans"] = [_plan()]
    stub["reviews"] = [True]
    s = _make_session(session_id="hist-1")
    orchestrator._run_pipeline(s)
    frames = list(orchestrator._PIPELINE_GRAPH.get_state_history(
        orchestrator._config("hist-1", "phrase")))
    assert len(frames) >= 6                       # seed/rewrite/plan/execute_tools/review/finalize/终态
```

Run:

```powershell
$env:PYTHONIOENCODING="utf-8"; D:\Anaconda\envs\shixi\python.exe -m pytest tests/test_orchestrator_paths.py -q
```

Expected: **RED**（T2/T3 因 `_rebuild_index` 不存在而 `AttributeError`）。

- [ ] **Step 2: 实现 `_rebuild_index()`**

在 `ai/orchestrator.py` 的「存储辅助」段（`# ---------- 存储辅助 ... ----------` 内，`_load_session` 之后）新增：

```python
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
```

并在**文件末尾**（两图已编译、所有函数已定义之后）新增一行调用：

```python
_rebuild_index()   # 启动时重建索引（Phase 3：进程重启后旧会话仍可读）
```

> `_rebuild_index()` 在模块导入时执行一次，是幂等的（重复调用结果相同）。

- [ ] **Step 3: 全量测试**

Run:

```powershell
$env:PYTHONIOENCODING="utf-8"; D:\Anaconda\envs\shixi\python.exe -m pytest tests/ -q
```

Expected: **48 passed**（44 + T2/T3/T4/T5）。

- [ ] **Step 4: 核对 `main.py` 无需改动**

检查 `ai/orchestrator.py` 的 diff 只含：import 段、`_sessions`/saver 构造段、`_rebuild_index()` 新增段、文件末尾一行调用。**其余一律零 diff**：

Run:

```powershell
git diff --stat -- ai/orchestrator.py
```

Expected: 仅 `ai/orchestrator.py` 一处，改动行数 < 40；`app/` 与 `web/` 零改动。

---

## Phase 3-c · 端到端重启验证

### Task 3: 起后端 → 发起问答 → 重启进程 → 旧会话仍可读

**Files:** 无代码改动（验证任务）

**Interfaces:**
- Consumes: `POST /api/qa/ask`、`GET /api/qa/{sid}/steps`、`GET /api/qa/{sid}/result`
- Produces: 重启前后同 `sid` 的响应一致性与字段完整性证据

- [ ] **Step 1: 起后端（进程 A）**

Run（后台）：

```powershell
$env:PYTHONIOENCODING="utf-8"; D:\Anaconda\envs\shixi\python.exe main.py
```

Expected: `[main] 文献雷达后端已就绪: http://127.0.0.1:8000`

- [ ] **Step 2: 登录 + 发起一次问答，记录 `sid`**

用浏览器/HTTP：登录取 token → `POST /api/qa/ask`（`mode=phrase`，`enable_planner=true`）→ 轮询 `GET /api/qa/{sid}/steps` 至 `finished=true` → 记录 `sid` 与完整响应体（字段快照）。

- [ ] **Step 3: 杀掉进程 A 并确认落盘文件存在**

Run:

```powershell
Get-Item .\data\qa_checkpoints.sqlite3 | Select-Object FullName, Length
```

Expected: 文件存在且 `Length > 0`。

- [ ] **Step 4: 重启后端（进程 B）**

再次 `python main.py`。

- [ ] **Step 5: 用同一 `sid` 再查（**关键验收**）**

- `GET /api/qa/{sid}/steps` → **HTTP 200**（重启前为 200，重启后**同为 200，不得 404**）
- 字段集与 Step 2 快照**逐字一致**，`finished=true`
- `GET /api/qa/{sid}/result` → 含 `answer` / `points` / `citations`

Expected: 全部通过。**任一项 404 / 字段缺失即为本阶段失败信号**，停下上报。

- [ ] **Step 6: 记录证据**

把 Step 5 的两次响应体与状态码写入 Task 5 的验证记录（§28.2）。

---

## Phase 3-d · 全量评测与收尾

### Task 4: 全量评测对照（单一变量验收门禁）

**Files:** 无代码改动（`eval/record/` 因 gitignore 不入库）

- [ ] **Step 1: 全量跑评测**

Run:

```powershell
$env:PYTHONIOENCODING="utf-8"; D:\Anaconda\envs\shixi\python.exe eval/run_eval.py
```

- [ ] **Step 2: 对照既有记录**

对照 Phase 2 收官记录 `eval/record/测试记录_20261019_172452.txt`（15/23 · 87648 token · 零异常）与同码历史三次（`111331` 14/23 · 90397 / `130406` 15/23 · 97479 / `162604` 12/23 · 61659）。

| 指标 | 判据 |
|---|---|
| 期望引用命中率 | 落在同码历史波动带内 |
| 总 token | 落在同码历史波动带内 |
| reviewer 判定 | 允许 LLM 调度波动 |
| **运行期异常** | **必须为零**（无 checkpoint 写入失败 / 流水线异常） |

- [ ] **Step 3: 若超带或出现异常**

记录原始数据并**上报用户裁定**（不自行放宽判据、不改代码追指标）。

---

### Task 5: 登记文档变更记录 §28 + 收尾

**Files:**
- Modify: `doc/项目文档/文档变更记录.md`（新增 §28）
- Modify: `doc/变更文档/LangGraph会话持久化设计.md`（如实测与本设计有偏离，追加「偏离说明」）

- [ ] **Step 1: 写 §28（沿用 §26/§27 三段式）**

`## 28. LangGraph 会话持久化（Phase 3）（2026-10-19）` 下三节：

- `### 28.1 本次变更点清单`：**修改文件**（`ai/orchestrator.py` saver/索引重建、`requirements.txt`、`pyproject.toml`、`tests/conftest.py`、`tests/test_orchestrator_paths.py`、`.gitignore`）/ **新增文件**（设计文档、实现计划）
- `### 28.2 验证记录`：pytest 计数、端到端重启实测（Task 3 证据）、全量 eval 对照表
- `### 28.3 要点备忘与遗留`：迁移边界、**传递依赖升级（`langgraph-checkpoint` 4.1.1→4.3.0）**、已知限制四条（§9）、提交状态

- [ ] **Step 2: 更新 §27.3 的遗留指向**

把 §27.3 末尾「仍是内存态…留作 Phase 3」一句，改为指向 §28（已落地）。

- [ ] **Step 3: 请许可提交**

向用户汇报 diff 摘要（`git status` / `git diff --stat`）并**等待授权**；未经许可**不提交**。

---

## 自审记录（Self-Review）

- **spec 覆盖**：D1（Task 1 Step 1）/ D2（Task 2）/ D3（Global Constraints + §2.2 非目标）/ D4（Task 1 Step 3+4）/ D5（Task 1 Step 4）/ D6（Task 1 Step 2 + T6）/ D7（Task 2 Step 2，索引保留仅新增重建）/ D8（沿用，未触及）——**全部有落点**。
- **等价性**：清单 1–11 全部「不触及」（仅改 saver 构造与索引重建），清单 12 为新增能力。既有 42 项断言**零修改**。
- **占位符**：无。所有新增代码均为可运行片段；参考行号取自本计划编写时的 [orchestrator.py](file:///d:/实习/my_project/deep_search_pro-master/ai/orchestrator.py) 现状（L53–56 / L140–159 / L162–192 / L306–312 存储辅助段），实施时以文件实际内容为准。
- **类型一致性**：`_QA_DB_PATH: str`；`_rebuild_index() -> None`；`_sessions[sid]` 值形态 `{user_id, created_at, mode}` 与 Phase 2 一致；`_PIPELINE_SAVER: SqliteSaver`。
- **风险前置**：传递依赖升级已在 Task 1 Step 6 用全量 pytest 兜住；`_safe_update` 静默吞异常按 spec §4.6 保持原样并在 Task 4 Step 2 用「零异常」判据观测。
- **已知测试覆盖缺口（如实登记）**：T4 用 `monkeypatch` 换 saver + 重编译图来模拟「新进程」，**未真正跨进程**；真正的跨进程验证由 Task 3 端到端承担。两者互补。
