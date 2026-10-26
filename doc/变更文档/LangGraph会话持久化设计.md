# LangGraph 会话持久化设计（Phase 3）

> 版本：v1.0　创建：2026-10-19
> 定位：把 QA / 综述会话的 checkpointer 由进程内 `InMemorySaver` 换成**落盘的 `SqliteSaver`**，让会话在**进程重启后仍可读**；同时解决「`_sessions` 内存索引不随重启存活 → 重启后 `get_steps` 直接 404」这一 §27.3 遗留。
> 关联文档：《LangGraph会话存储迁移设计.md》（Phase 2，§27.3 为本阶段动机）§2.2；《LangGraph编排重构设计.md》（Phase 1）§1.2；《多agent编排方案v4.md》§4/§5；《详细设计文档.md》§3.5/§5.1

---

## 1. 背景与问题

### 1.1 Phase 2 遗留

Phase 2 把会话正文迁到 LangGraph checkpointer，但选型明确为 **`InMemorySaver`**（Phase 2 spec §2.2：不换 `SqliteSaver`，守单一变量）。因此：

1. **不可持久化**：`InMemorySaver` 进程内有效，**进程重启即丢**；Phase 1 §1.2 所列「跨进程会话恢复 / 按轮回放 / `interrupt()` 人工介入」全部仍无从落地。
2. **索引也不存活**（本阶段必须先解决的真问题）：`_sessions` 是**纯内存索引** `sid → {user_id, created_at, mode}`。重启后索引为空 ⇒ `get_steps` / `get_result` / `get_raw` 在**第一步查索引**时即返回 `None`，**根本走不到 `graph.get_state`**——即使 checkpointer 能持久化，重启后旧会话对外仍是 **404**。
3. **落盘语义升级**：`_safe_update` 在内存 saver 下吞掉异常无实际损失；换成落盘 saver 后，「落盘失败」= **数据真丢失**，风险等级上升。

### 1.2 本阶段问题

把会话存储后端换成**真正落盘的 SQLite**，并让**索引同样在重启后可用**，从而实现「重启后旧会话仍可读 + 可读历史轮次」，**对外契约（前端响应 schema、API 路径、步骤文案、评测口径）一行不改**。

---

## 2. 目标与非目标

### 2.1 目标

1. checkpointer 由 `InMemorySaver` 换为 **`SqliteSaver`**，会话正文落 SQLite 文件。
2. `_sessions` 索引添加**启动时从 checkpointer 重建**的能力，使重启后索引可用。
3. **接口层零改动**：`get_steps` / `get_result` / `get_raw` / `_memory_records` / `_cleanup_expired` 全部保持原样（它们只依赖 `_sessions` 与 `_load_session`，而 `_load_session` 走 `graph.get_state` → 自动改从 SQLite 读）。
4. 既有 42 项测试全绿；全量 `eval/run_eval.py` 落在同码历史波动带内、**运行期零异常**。

### 2.2 非目标（明确不做）

- **不做「断点续跑」自动恢复**：进程重启后不自动重新 `invoke` 未终态会话（会重触发 LLM 调用、产生重复证据与额外 token 消耗，需另行定义「未终态」判据与重跑护栏）。
- **不做 `interrupt()` 人工介入**：需新增恢复接口 + 改前端，破坏「前端契约冻结」，留待 Phase 4。
- **不新增任何对外 API**（不做「按轮回放」的 HTTP 接口；`get_state_history` 仅作为**数据基础**存在，本期不暴露）。
- 不改 LLM 层、检索策略、提示词、reviewer 入参语义。
- 不改 `app/*`（routers / schemas）与前端 `web/`。
- 不改 `eval/run_eval.py` 的对外指标口径。

---

## 3. 已确认的关键决策

**用户裁定（2026-10-19）**

| # | 决策 | 结论 |
|---|---|---|
| D1 | 依赖 | **允许安装官方 `langgraph-checkpoint-sqlite`**（PyPI 最新 `3.1.2`），并同步 `requirements.txt` / `pyproject.toml` |
| D2 | 索引持久化策略 | **启动时从 checkpointer 重建**（不建独立索引表、不做双写，避免两处写不一致） |
| D3 | 交付边界 | **仅持久化基础**：不做断点续跑、不做 `interrupt()` 人工介入 |

**由本设计补充（实现细节，非契约变更）**

| # | 决策 | 结论 |
|---|---|---|
| D4 | DB 文件位置 | **相对路径** `data/qa_checkpoints.sqlite3`（基于 `app.config.PROJECT_ROOT`），目录不存在则创建；`data/` 追加进 `.gitignore` |
| D5 | 连接形态 | **模块级单例**：`sqlite3.connect(DB_PATH, check_same_thread=False)` + `SqliteSaver(conn)`；**不用 `from_conn_string`**（它是 contextmanager，退出即关连接，与「常驻 saver」形态不符，且会让 pytest / eval 这类不跑 FastAPI lifespan 的场景拿不到 saver） |
| D6 | 测试隔离 | 新增环境变量 **`LITRADAR_QA_DB`**（缺省 `data/qa_checkpoints.sqlite3`）；`tests/conftest.py` 顶层设为 `":memory:"`，使 `pytest` **零落盘副作用、进程内隔离** |
| D7 | `_sessions` 去留 | **保留为内存索引**（同 Phase 2 形态），仅新增「启动时重建」；索引仍只用于存在性判定 / 按用户枚举 / TTL 清理 |
| D8 | 两图共用 saver | 沿用 Phase 2：QA 图与综述图**共用同一 saver 实例**，命名空间经 `thread_id` 前缀隔离（`qa:{sid}` / `rev:{sid}`） |

---

## 4. 设计

### 4.1 探针结论（本机实测 · 2026-10-19）

**依赖安装实测**

| 项 | 实测结果 | 影响 |
|---|---|---|
| `langgraph-checkpoint-sqlite` | 安装 `3.1.2` 成功 | 新增 1 个显式依赖 |
| **传递依赖升级** | `langgraph-checkpoint` **4.1.1 → 4.3.0**（被 sqlite 包要求 `<5.0.0,>=4.3.0`） | ⚠️ 非预期副作用，需登记；实测 `import ai.orchestrator` 正常、`pytest tests/ -q` **42 passed** |
| 新增包 | `sqlite-vec 0.1.9` | 随 sqlite 包带入 |
| `langgraph` 主包 | 仍为 `1.1.9`（未动） | Phase 1/2 结论继续有效 |

**`SqliteSaver` 行为实测**（临时脚本在项目目录外，未留痕）

| 探针 | 结论 | 对设计的影响 |
|---|---|---|
| 构造形态 | `SqliteSaver(conn, *, serde=None)`；另有 `from_conn_string()`（contextmanager，内部 `check_same_thread=False`）与 `AsyncSqliteSaver`（需 `aiosqlite`） | 采用 D5 手动建连接 + 单例 |
| 线程安全 | 源码 `__init__` 持 `self.lock = threading.Lock()`，写路径 `with self.lock:`；官方 docstring 明言「`check_same_thread=False` OK，因为实现用了锁」 | **daemon 线程内 `invoke` 与主线程并发写均实测零异常**（5 线程并发写 5 个 thread 无报错） |
| 序列化/反序列化 | `datetime` 可往返；整段 `session` dict 可落盘取回（取回值逐字段一致） | 索引重建可依赖 `session` 内的 `created_at` / `user_id` / `mode` |
| **重启后读取** | 在同一 DB 上**新建 saver / 新连接**（模拟新进程）后，旧 `thread_id` 仍能 `get_state` 取回完整会话 | ✅ **持久化目标成立** |
| 历史枚举 | `graph.get_state_history(cfg)` 返回该 thread 全部 checkpoint（含 `metadata.step` / `source`），本次单节点探针为 3 条（`step=1,0,-1`） | 具备「**按轮回放**」的数据基础（本期不暴露接口，§4.5） |
| 全量枚举 | `saver.list(None)` 可枚举**全部 thread** 的 checkpoint，**按 `checkpoint_id DESC`（新→旧）**；`source=input` 的首帧 `channel_values` **不含 `session`** | 索引重建算法须「每 thread 取首个**含 session** 的 checkpoint」 |
| 删除 | `delete_thread(tid)` 删 `checkpoints` + `writes`（真删持久化数据） | `_cleanup_expired` 语义等价（过期即销毁），行为不变 |
| 建表 | `setup()` 惰性自动调用，无需人工建表 | 初始化零额外步骤 |

### 4.2 持久化后端与 DB 文件（D1 / D4 / D5）

```python
# ai/orchestrator.py（示意）
from langgraph.checkpoint.sqlite import SqliteSaver
import sqlite3, os
from pathlib import Path
from app.config import PROJECT_ROOT

# 会话落盘位置：相对项目根（缺省 data/qa_checkpoints.sqlite3；测试经 LITRADAR_QA_DB 覆盖为 :memory:）
_QA_DB_PATH = os.getenv("LITRADAR_QA_DB") or str(PROJECT_ROOT / "data" / "qa_checkpoints.sqlite3")
if _QA_DB_PATH != ":memory:":
    Path(_QA_DB_PATH).parent.mkdir(parents=True, exist_ok=True)

_PIPELINE_SAVER = SqliteSaver(sqlite3.connect(_QA_DB_PATH, check_same_thread=False))
```

- **连接常驻**：模块级单例，随进程生命周期存活；进程退出由 SQLite 自行收尾（本期**不**挂 FastAPI lifespan 显式 close，守「接口层零改动 / 单一变量」；如需显式关闭留作后续）。
- **路径相对化**：`LITRADAR_QA_DB` 支持绝对路径覆盖，便于部署与测试。

### 4.3 启动时索引重建（D2 / D3 / D7）

```python
def _rebuild_index() -> None:
    """启动时从 checkpointer 重建最小索引（进程重启后旧会话仍可被 get_steps 命中）"""
    seen: set[str] = set()
    for t in _PIPELINE_SAVER.list(None):            # 全局按 checkpoint_id 新→旧
        cfg = t.config.get("configurable") or {}
        tid = cfg.get("thread_id") or ""
        if tid in seen:
            continue
        seen.add(tid)                              # 每 thread 只认首个「含 session」的帧
        prefix, _, sid = tid.partition(":")
        sess = (t.checkpoint.get("channel_values") or {}).get("session")
        if not sid or not isinstance(sess, dict):
            continue
        mode = sess.get("mode") or ("review" if prefix == "rev" else "phrase")
        _sessions[sid] = {"user_id": sess.get("user_id"),
                          "created_at": sess.get("created_at") or datetime.now(),
                          "mode": mode}
```

要点：

1. **只扫一次**，在模块导入时执行（`_PIPELINE_SAVER` 建好、图编译前/后均可，取「编译后」以保证 setup 已触发）。
2. **每 thread 取首个含 `session` 的帧**：`list` 是全局 `checkpoint_id DESC`，同 thread 内 id 单调 ⇒ 首次出现即最新；用 `seen` 跳过后续（旧）帧。`source=input` 首帧无 `session`，用 `isinstance(sess, dict)` 判空跳过。
3. **`mode` 以会话正文为准**（`sess["mode"]`），`thread_id` 前缀仅作兜底 —— 因为 QA 图的 `mode` 取值是 `phrase` / `paragraph` / `strategy`（**不是** thread 前缀的 `qa`）。
4. **成本**：DB 受 `SESSION_TTL`（2h）+ `_cleanup_expired` 约束，规模有界；重建只在启动执行一次。

### 4.4 接口层与 TTL 清理（目标 3：零改动）

现有链路已天然适配，**一行不改**：

| 函数 | 现状 | Phase 3 是否改动 |
|---|---|---|
| `get_steps` / `get_result` / `get_raw` | 先查 `_sessions` 拿 `mode` → `_load_session` → `graph.get_state` | **不改**（重启后索引已重建，链路自然走通） |
| `_load_session` | `graph.get_state(_config(sid, mode))` | **不改**（底层 saver 换成 SQLite 后自动改从落盘读） |
| `_memory_records` | 按 `user_id` 过滤 `_sessions` → 逐个 `_load_session` | **不改**（枚举范围仍来自索引） |
| `_cleanup_expired` | 移除索引 + `graph.checkpointer.delete_thread(...)` | **不改**（`delete_thread` 现在真删落盘数据，语义仍是「过期即销毁」） |
| `ask` / `ask_review` / `_seed_pipeline` / `_seed_review` / `_safe_update` | 写索引 + seed + 落终态 | **不改** |

> 这是 Phase 3 最大的红利：Phase 2 已把接口层与存储实现解耦在 `_load_session` 这一层，换后端只需改 saver 构造 + 索引重建。

### 4.5 「按轮回放」的数据基础（本期不暴露接口）

- `graph.get_state_history(cfg)` 已可枚举该 thread 的逐节点 checkpoint（探针确认，含 `metadata.step` / `source`）。
- **本期不做**：不新增 HTTP 接口、不改前端。理由：新增接口属**对外契约变更**，与 D3「仅持久化基础」冲突，留待 Phase 4 与断点续跑/人工介入一并设计。
- 本期验收仅要求「数据基础存在」（即 testing 中可断言历史帧数，见 §6.2）。

### 4.6 `_safe_update` 落盘失败语义（风险登记，本期不改）

- 现状：`_safe_update` 捕获异常只打一行 stdout，**不写 `session["error"]`**，不阻断流水线（Phase 2 计划明令保留）。
- 风险升级：内存 saver 下「写失败」无实际损失；落盘 saver 下「写失败」= **该次 checkpoint 真丢失**（重启后该轮不可见）。
- **本期处置：保持原样**（守单一变量、不改前端可见字段）。**仅登记风险**：若后续出现落盘失败，表现将是「会话停在旧帧 / `finished` 不翻真」，而不是报错。
- 备选（**不在本期实施**，如需可由用户裁定）：失败时写 `session["error"]`（会改变前端可见的 `error` 字段语义）。

### 4.7 测试隔离（D6）

- 落盘 saver 会**在项目目录产生真实文件**并**跨用例泄漏数据**，与 Phase 2（纯内存、无副作用）不同，必须隔离。
- 方案：`tests/conftest.py` **顶层**（早于测试模块导入）`os.environ.setdefault("LITRADAR_QA_DB", ":memory:")`。
  - `:memory:` + 单连接 ⇒ 进程内可见、进程结束即消失，**零文件产物、零跨进程污染**。
  - `conftest.py` 由 pytest 在收集测试模块**之前**导入，故 `ai.orchestrator` 里读环境变量的时机是被覆盖之后。
- 该改动不触碰现有 MySQL 夹具逻辑（`test_db_name` / `engine` / `_clean` 等一律不动）。

---

## 5. 行为等价性清单（Phase 3 验收标准）

| # | 项 | 期望 |
|---|---|---|
| 1 | 四路径行为 | quick / normal / deep / fallback 既有 12 项集成护栏断言**逐条不变** |
| 2 | 综述路径 | 4 节点行为不变 |
| 3 | 前端响应 schema | `get_steps` 9 字段、`get_result` 14+ 字段**逐字冻结**（含 review 会话的 5 个附加字段） |
| 4 | 步骤文案 | 全部中文文案逐字不变 |
| 5 | 评测口径 | `eval/run_eval.py` 对外指标（命中率 / token / reviewer 判定）**口径不变**；`get_raw` 读源不变 |
| 6 | **重启后可读（本阶段新能力）** | 同一 DB 上**新建 saver/新连接**后，旧 `sid` 的 `get_steps` / `get_result` 仍返回完整会话（不再 404） |
| 7 | **索引重建正确性** | 重建后 `_sessions[sid]` 的 `user_id` / `created_at` / `mode` 与写入时一致；`rev:` 前缀 → `mode="review"` |
| 8 | 按轮历史可得 | `graph.get_state_history` 帧数 ≥ 该次运行的节点数（数据基础存在） |
| 9 | TTL 清理 | 过期会话索引移除且 `get_steps` 返回 `None`（沿用既有用例） |
| 10 | 无落盘异常 | 全量 `pytest` 与全量 eval 均**无 checkpoint 写入失败**日志 |
| 11 | 依赖一致 | `requirements.txt` 与 `pyproject.toml` 同步；`langgraph-checkpoint-sqlite>=3.1.2` |
| 12 | 工作区副作用 | `pytest` 运行**不产生** `data/qa_checkpoints.sqlite3`（D6 生效） |

---

## 6. 测试设计

### 6.1 既有 42 项护栏（必须全绿，不得改断言）

`tests/test_orchestrator_paths.py`（12 路径/结构用例 + 5 存储层用例）、`tests/test_reviewer_checklist.py`、`tests/test_retriever_recall.py`、`tests/test_schema.py` 等。**逐条不改**（这是「行为等价」的硬护栏）。

### 6.2 新增存储/持久化用例（建议加在 `tests/test_orchestrator_paths.py` 末尾）

| # | 用例 | 断言 |
|---|---|---|
| T1 | `test_saver_is_sqlite` | `orchestrator._PIPELINE_SAVER` 是 `SqliteSaver` 实例 |
| T2 | **`test_rebuild_index_from_checkpointer`** | 手写索引缺失 → 调 `_rebuild_index()` → `_sessions` 中重新出现该 sid 且 `user_id/mode/created_at` 正确 |
| T3 | **`test_rebuild_index_skips_review_prefix_mode`** | 综述会话（`rev:` 前缀，`session["mode"]="review"`）重建后 `mode="review"` |
| T4 | **`test_session_readable_across_saver_instances`** | 用 saver A 跑完一条流水线 → 在同 DB 上新建 saver B（`_memory:` 例外，需临时文件）→ `get_state` 仍能取回完整会话（等价「重启后可读」） |
| T5 | `test_history_frames_for_replay` | `graph.get_state_history(cfg)` 帧数 ≥ 节点数（按轮回放数据基础） |
| T6 | `test_pytest_uses_memory_saver` | `orchestrator._QA_DB_PATH == ":memory:"`（D6 隔离生效，防误落盘） |

> T4 需真实文件（`:memory:` 无法跨连接共享），实现时用 `tmp_path` 夹具指向临时 DB 文件；该用例须在结束后清理。设计上不引入全局夹具，遵循「最小改动」。

### 6.3 评测门禁

- `python eval/run_eval.py` 全量：对照 Phase 2 收官记录（`测试记录_20261019_172452`，15/23 · 87648 token · 零异常）与同码历史三次（`111331` 14/23·90397 / `130406` 15/23·97479 / `162604` 12/23·61659）。
- 判据（沿用 Phase 2 裁定结论）：**内容指标落在同码历史波动带内 + 运行期零异常** ⇒ 通过；不设「逐项一致」硬阈值（LLM 非确定性）。

---

## 7. 风险与对策

| 风险 | 等级 | 对策 |
|---|---|---|
| **`langgraph-checkpoint` 传递升级 4.1.1→4.3.0 引入行为漂移** | 中 | 已在探针中实测 import + 全量 pytest（42 passed）；Phase 3 全量 eval 再对照；若出现异常，记录并交用户裁定（可能需回退锁版本） |
| 落盘 saver 在 pytest 中产生真实文件 / 跨用例污染 | 中 | D6：`conftest.py` 置 `LITRADAR_QA_DB=":memory:"`；T6 用例守卫 |
| 索引重建成本（全量反序列化）随时间增长 | 低 | DB 受 `SESSION_TTL` + `_cleanup_expired` 约束；重建仅启动一次；如实测显著，Phase 4 可评估惰性重建（首次 miss 时执行） |
| **重启后「未终态」会话仍显示 `finished=false`**（daemon 线程随进程死亡，终态未落盘） | 中 | **本期接受**（不做自动恢复，§2.2）；`get_steps` 仍可读到中断时的进度；会话将在 2h 后 TTL 淘汰。登记为已知限制（§9） |
| `_safe_update` 吞掉落盘失败 → 静默数据丢失 | 中 | 本期保持原样（守单一变量）；风险已登记（§4.6）；建议后续阶段评估升级为可感知错误 |
| SQLite 并发写锁竞争（daemon 线程 + 主线程） | 低 | 官方实现持进程内 `threading.Lock`，实测 5 线程并发写零异常；daemon 线程写与主线程读混合场景在 eval 中一并覆盖 |
| `delete_thread` 现在真删磁盘数据，误删无法恢复 | 低 | 触发路径不变（仅 TTL 过期）；TTL=2h 不变 |
| DB 文件损坏 / 目录不可写 | 低 | 路径基于 `PROJECT_ROOT`（本地部署）；写失败由 `_safe_update` 兜住不阻断（但会静默丢帧，见上） |

---

## 8. 文件布局

**修改**

| 文件 | 改动 |
|---|---|
| `ai/orchestrator.py` | saver 构造（`SqliteSaver` + `LITRADAR_QA_DB` + 建目录）、新增 `_rebuild_index()` 并在模块级调用；`import` 增补。**其余函数一行不改** |
| `requirements.txt` / `pyproject.toml` | 同步补 `langgraph-checkpoint-sqlite>=3.1.2`（逐字符一致） |
| `tests/conftest.py` | 顶层 `os.environ.setdefault("LITRADAR_QA_DB", ":memory:")` |
| `tests/test_orchestrator_paths.py` | 末尾追加 6 条持久化用例（§6.2） |
| `.gitignore` | 追加 `data/` |
| `doc/项目文档/文档变更记录.md` | 新增 §28（Phase 3） |

**新增**

| 文件 | 说明 |
|---|---|
| `doc/变更文档/LangGraph会话持久化设计.md` | 本文档 |
| `doc/变更文档/LangGraph会话持久化实现计划.md` | 实现计划（另行编写） |

**不改**：`app/*`、`web/*`、`ai/llm_provider.py`、`ai/retriever.py`、`ai/planner.py`、`ai/reviewer.py`、`ai/tools.py`、`eval/run_eval.py`。

---

## 9. 分阶段交付与验收

| 步骤 | 内容 | 验收 |
|---|---|---|
| 1 | 依赖入清单 + saver 换 `SqliteSaver` + `_rebuild_index` | `pytest` 全绿；`_PIPELINE_SAVER` 为 `SqliteSaver`；旧 42 项断言未改 |
| 2 | 追加 6 条持久化用例 | 新增用例 RED→GREEN；全量全绿 |
| 3 | 端到端重启验证 | 起后端 → 发起 QA → **重启进程** → 同 `sid` 的 `/api/qa/{sid}/steps` 仍 200 且字段完整（浏览器/HTTP 实测） |
| 4 | 全量 eval 对照 | 落在同码波动带 + 零异常 |
| 5 | 文档登记 §28 | 变更点/验证记录/遗留三节齐备 |

**已知限制（本期不解决，需登记）**

1. 重启后**未终态**会话仍显示 `finished=false`（不做自动续跑；2h 后 TTL 淘汰）。
2. 无「按轮回放」对外接口，无 `interrupt()` 人工介入。
3. `_safe_update` 落盘失败仍静默（仅 stdout 一行）。
4. 无显式 `close()`：SQLite 连接随进程退出释放（未挂 FastAPI lifespan）。

---

## 10. 与既有文档的偏离说明

| 项 | 原表述 | 本设计 | 说明 |
|---|---|---|---|
| Phase 2 §2.2 | 「不换 `SqliteSaver`，留作 Phase 3」 | 本阶段实施 | 如期兑现 |
| Phase 1 §1.2 | 「断点续跑 / 按轮回放 / `interrupt()` 人工介入」列为本阶段动机 | **本期只交付「持久化 + 重启可读」，不做续跑/介入/新接口** | 三项均涉对外契约或重跑语义，为守「单一变量 + 前端契约冻结」，拆到 Phase 4（D3 用户裁定） |
| Phase 2 §4.2 | saver 为 `InMemorySaver()`，零新依赖 | 换 `SqliteSaver` + 新增显式依赖 | 唯一的依赖变更，副作用（`langgraph-checkpoint` 传递升级）已实测并登记（§4.1/§7） |
| Phase 2 §27.3 | `_safe_update` 静默吞异常「按计划原文保留」 | 本期**仍保留**，仅登记风险升级 | 见 §4.6 |
