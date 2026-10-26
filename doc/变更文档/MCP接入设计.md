# 工具层 MCP 接入设计（自建 MCP server + 混合工具层）

> 状态：**设计待评审**（尚未实现；本轮先出文档，经确认后按 TDD 落地）
> 分支：`feat/summarize-before-review`（沿用当前工作分支）
> 关联：`doc/变更文档/Summarizer前移审草稿设计.md` §9（本项来源）、`doc/变更文档/LangGraph编排重构设计.md` §5.1.2（工具层权限校验口径）
> 前序：批次 1（§33 Summarizer 前移）已实现、验收、提交，本批次**不再改动控制流**。

---

## 1. 背景与问题

### 1.1 现状

工具层是**单一进程内入口**（`ai/tools.py`）：

```
Planner 产出 tool_calls → orchestrator._execute_tool_calls → tools_mod.execute(name, args, ctx) → _DISPATCH[name]
```

6 个工具全部以 Python 函数形式进程内执行：

| 工具 | 实现 | 依赖 |
|---|---|---|
| `library_search` | `retriever.search_library(query_struct, user_id=…)` | `ctx["user_id"]`、`ctx["query_struct"]` |
| `web_search` | `retriever.search_web(query_struct)` | `ctx["query_struct"]` |
| `memory_search` | `retriever.search_memory(records, query, keywords)` | `ctx["session_id"]` + 会话历史回调 |
| `rewrite_query` | `rewrite.rewrite_query(user_query, mode, enabled)` | `ctx["mode"]` / `ctx["rewrite"]` |
| `run_analysts` | `analysts.run_analysts(evidence, keys=…)` | `ctx["evidence"]`（**对象引用**） |
| `run_reviewer` | `reviewer.review(question, evidence, analysts, draft=…)` | `ctx["evidence"]` + `ctx["analyst_outputs"]` + `ctx["result"]` |

### 1.2 为什么接 MCP

现状下"工具"只是内部函数，**没有协议边界**：

- 工具无法被外部（其他 Agent / 进程 / 语言）复用；
- 工具契约（入参 schema、返回结构）没有机器可读的声明，仅靠 docstring；
- 无法把"检索能力"作为独立服务对外暴露。

MCP（Model Context Protocol）提供标准化工具契约：`list_tools` 返回 JSON Schema、`call_tool` 以显式入参调用。本批次把**无状态、纯入参**的 3 个工具迁到 MCP，验证协议边界，同时不触碰控制流。

### 1.3 为什么只迁 3 个

`memory_search` / `run_analysts` / `run_reviewer` **不适合过协议**：

- `memory_search` 吃 `session_id` + 会话历史（服务端需回连编排层的会话存储）；
- `run_analysts` 吃 `ctx["evidence"]` 的**对象引用**（过协议需全量序列化证据集，收益不抵成本）；
- `run_reviewer` 吃 `evidence + analyst_outputs + draft`（同上，且复审语义强绑定编排层上下文）。

结论：**只把 `library_search` / `web_search` / `rewrite_query` 三个无状态工具迁到 MCP**，其余 3 个保持进程内。

---

## 2. 目标与非目标

### 2.1 目标

1. 自建 MCP server，以**机器可读 schema**暴露 3 个无状态工具。
2. 编排层工具层改**混合层**：3 个 MCP 工具走 MCP client，其余 3 个仍走进程内 `_DISPATCH`。
3. **MCP 不可用时自动回落进程内实现**，链路不中断、行为与现状一致。
4. `user_id` 作为 MCP **入参显式传入**，服务端不做鉴权（维持"客户端已校验"口径），`_apply_filters` 的 user_id 子查询规则**不变**。
5. `app/*`、`web/*` **一行不改**；`review_notes` 三字段契约零变更。

### 2.2 非目标（明确不做）

- **不改控制流**：`rewrite / plan / execute_tools / summarize / review / finalize / quick / fallback` 图结构与节点逻辑一律不动（单一变量原则）；
- 不改 `_apply_tool_result`、`_execute_tool_calls` 的调用口径；
- 不改 `_apply_filters` 的用户隔离规则；
- 不迁 `memory_search` / `run_analysts` / `run_reviewer`；
- 不改 Planner 提示词、不改各工具的业务实现（server 端复用既有函数，不重写逻辑）；
- 不引入 `langchain-mcp-adapters`（见 §3 D4）。

---

## 3. 已确认的关键决策

| # | 决策 | 结论 |
|---|---|---|
| D1 | 改动性质 | **工具层改动**（不含控制流），单一变量原则 |
| D2 | 方向 | **自建 MCP server**（官方 `mcp` SDK）+ 编排层改 MCP client |
| D3 | 传输与进程模型 | **stdio 子进程**：编排层以 `StdioServerParameters` 拉起 `python -m ai.mcp_server`；无端口、可离线；server 端就近访问 MySQL / 向量库 |
| D4 | 客户端选型 | **官方 `mcp` SDK 直调**（`ClientSession.call_tool`），不引入 LangChain Tool 抽象；**`langchain-mcp-adapters` 不声明、不使用**（修正《Summarizer前移审草稿设计》§9 的"两个都补"表述） |
| D5 | 迁移范围 | 仅 3 个无状态工具（`library_search` / `web_search` / `rewrite_query`）；其余 3 个保持进程内 |
| D6 | 工具层形态 | 混合层：MCP client 优先 → 失败回落进程内 `_DISPATCH` |
| D7 | 降级开关 | 环境变量 `LITRADAR_MCP_ENABLED`，**默认开**（`!= "0"` 即启用），沿用项目 `LITRADAR_*` 命名；连接/调用失败**自动回落**，不依赖开关 |
| D8 | 权限隔离 | `user_id` 作为 MCP 入参显式传入，服务端**不做鉴权**；`_apply_filters` 子查询规则不变 |
| D9 | 测试策略 | 多数用例 **mock MCP 会话**（覆盖"调用成功"与"连接失败回落"两条分支）+ **1 例真实 stdio 集成**冒烟 |
| D10 | 依赖声明 | `requirements.txt` 补 `mcp>=1.27.0`（环境已装 1.27.0） |
| D11 | git 工作方式 | 沿用当前工作分支，改动留工作区待审，**未经许可不提交** |

---

## 4. 架构与数据流

### 4.1 改后结构

```
┌─────────────────────── 编排层进程（orchestrator） ───────────────────────┐
│  _execute_tool_calls → tools_mod.execute(name, args, ctx)                │
│         │                                                                │
│         ├── name ∈ {library_search, web_search, rewrite_query}           │
│         │        └─→ ai/mcp_client.call_tool(name, params)  ── stdio ──┐  │
│         │                成功 → 重建 {ok,brief,data,error}             │  │
│         │                失败/未启用 → 回落 _DISPATCH[name]  ──────┐   │  │
│         │                                                          │   │  │
│         └── name ∈ {memory_search, run_analysts, run_reviewer}     │   │  │
│                  └─→ _DISPATCH[name]（进程内，不变） ──────────────┘   │  │
└───────────────────────────────────────────────────────────────────┼──┘
                                                                     │
                          ┌──────────────────────────────────────────┘
                          ▼
         ┌──────── MCP server 子进程（python -m ai.mcp_server）────────┐
         │  FastMCP 暴露 3 个工具 → 直接复用 ai.retriever / ai.rewrite │
         │  library_search(keywords, year_from, year_to, sources,     │
         │                 user_id) → retriever.search_library(...)   │
         │  web_search(keywords, year_from, year_to, sources)         │
         │                 → retriever.search_web(...)                │
         │  rewrite_query(user_query, mode, enabled)                  │
         │                 → rewrite.rewrite_query(...)               │
         └────────────────────────────────────────────────────────────┘
```

### 4.2 关键点

- **server 端不 import `ai.tools`**（避免与 client 形成循环依赖），直接调 `ai.retriever` / `ai.rewrite` 的既有函数——**核心业务逻辑零重写**；
- **client 端保留 `_merge_struct` 与 `_ok/_fail` 语义**：MCP 只传"已合并的检索条件"，`brief` 文案与 `data` 结构由 client 侧按进程内实现的口径**原样重建**，确保 `_apply_tool_result` 与前端契约不受影响；
- **异步桥接**：`mcp` SDK 的会话是 async 的，而编排层在后台线程内同步调用。client 内起一个**常驻 daemon 线程跑专用 event loop**，同步入口用 `asyncio.run_coroutine_threadsafe(...).result(timeout=…)` 取值；
- **会话复用**：会话为**模块级单例**，进程内首次调用时惰性建立，之后复用（3 个工具均无状态，复用安全）；
- **失败处置**：任一传输/协议失败 → 关闭会话并标记不可用，本进程后续直接走进程内回落（不反复 spawn 子进程）。

---

## 5. 新增模块

### 5.1 `ai/mcp_server.py`（新增，MCP server）

- 用 `mcp.server.fastmcp.FastMCP("litradar-tools")` 注册 3 个工具（`@mcp.tool()`）；
- 每个工具函数**只做入参适配 + 调既有实现 + 返回 JSON 可序列化结果**，不复制业务逻辑；
- 模块头注释 + 中文 docstring，沿用项目风格；
- 入口：`if __name__ == "__main__": mcp.run(transport="stdio")`，即 `python -m ai.mcp_server`。

工具签名（机器可读 schema 的公开契约）：

```python
# ai/mcp_server.py（示意）
@mcp.tool()
def library_search(keywords: list[str], year_from: str = "", year_to: str = "",
                   sources: list[str] | None = None, user_id: int | None = None) -> list[dict]:
    """库内检索：词法路 + 向量路双路召回（复用 ai.retriever.search_library）"""
    struct = {"keywords": keywords or [], "filters": {
        "year_from": year_from or "", "year_to": year_to or "",
        "sources": sources or [], "cats": []}}
    return retriever_mod.search_library(struct, user_id=user_id)

@mcp.tool()
def web_search(keywords: list[str], year_from: str = "", year_to: str = "",
               sources: list[str] | None = None) -> list[dict]:
    """网络实时学术检索（复用 ai.retriever.search_web）"""

@mcp.tool()
def rewrite_query(user_query: str, mode: str = "phrase", enabled: bool = True) -> dict:
    """查询改写（复用 ai.rewrite.rewrite_query），失败回退原始问句"""
```

> 说明：server 收到的是 client 侧 `_merge_struct` 后的**已合并检索条件**，因此 `cats` 由 client 一并传递（上面示意中 `cats` 从 filters 缺省补齐，实际以 client 传参为准，见 §6.1）。

### 5.2 `ai/mcp_client.py`（新增，MCP client）

职责：**开关判定 + 惰性建立 stdio 会话 + 同步 `call_tool` + 失败标记**。

```python
# ai/mcp_client.py（示意，非最终代码）
def enabled() -> bool:
    """LITRADAR_MCP_ENABLED 默认开（!= '0' 即启用）"""

def call_tool(name: str, arguments: dict) -> list | dict | None:
    """同步调用 MCP 工具；返回解析后的结构化结果；未启用/失败 → 返回 None（由工具层回落）"""
```

- 惰性建立：首次 `call_tool` 时在专用 loop 线程内 `StdioServerParameters(command=sys.executable, args=["-m", "ai.mcp_server"], cwd=PROJECT_ROOT, env=…PYTHONPATH=PROJECT_ROOT…)` + `stdio_client()` + `ClientSession()`；
- 超时：`LITRADAR_MCP_TIMEOUT`（默认 30s），超时/异常 → 判失败；
- 结果解析：FastMCP 工具返回 JSON，client 侧解析为 Python 对象（`structuredContent` 优先，回落解析文本块 JSON）；
- 失败标记：置 `_UNAVAILABLE=True`，后续 `call_tool` 直接返回 None（避免每轮重连）。

---

## 6. 改动点清单

### 6.1 `ai/tools.py`（混合层）

| 位置 | 改动 |
|---|---|
| 模块头注释 | 补充"混合工具层：3 个 MCP 工具 + 3 个进程内工具"说明 |
| 新增 `_MCP_TOOLS` | `("library_search", "web_search", "rewrite_query")` |
| 新增 `_mcp_params(name, args, ctx)` | 复用既有 `_merge_struct`，把会话检索式 + 工具入参**合并**为 MCP 显式入参（含 `user_id`），保证"已合并条件"语义与进程内一致 |
| `execute()` | 入口分流：`name ∈ _MCP_TOOLS` 且 `mcp_client.enabled()` → 先走 `mcp_client.call_tool`；成功则重建 `{ok,brief,data,error}`；否则（未启用/返回 None/异常）**回落** `_DISPATCH[name]`。其余工具维持原样 |
| `_library_search` / `_web_search` / `_rewrite_query` | **保留为回落实现**（不动实现体），供 MCP 不可用时使用 |
| `_memory_search` / `_run_analysts` / `_run_reviewer` | `_DISPATCH` 内不变，永远进程内 |

**brief 文案一致性**：MCP 成功路径的 `brief` 由 client 侧按与进程内实现**逐字相同**的模板生成（如 `f"命中 {len(evidence)} 篇（user_id={uid} 隔离）"`），确保前端思考面板与评测记录的展示零漂移。

> 风险点（实现时注意）：`_merge_struct` 目前对 `filters` 的 `cats` 不做显式透传（缺省补 `[]`）。MCP 入参需把 `cats` 一并向 server 传递，避免丢过滤维度；实现时以"合并后 filters 全字段传递"为准。

### 6.2 `requirements.txt` / `pyproject.toml`

- 补 `mcp>=1.27.0`（与 `pyproject.toml` dependencies 保持同步——文件头注释既有约定）；
- **不**加入 `langchain-mcp-adapters`（D4）。

### 6.3 测试

| 文件 | 改动 |
|---|---|
| `tests/test_mcp_tools.py`（新增） | ① `enabled()` 开关语义（默认开 / `=0` 关）；② mock MCP 会话调用成功 → `execute` 走 MCP 且 `{ok,brief,data}` 与进程内口径一致；③ mock MCP 连接失败/返回 None → `execute` 回落进程内，结果与纯进程内一致；④ `user_id` 出现在 MCP 入参；⑤ 未启用时 3 个工具全走进程内；⑥ `memory_search/run_analysts/run_reviewer` 永不走 MCP |
| `tests/test_mcp_server_integration.py`（新增，1 例） | 真实 `python -m ai.mcp_server` stdio 冒烟：`list_tools` 含 3 个工具、逐个 `call_tool` 至少 1 次成功返回（检索类允许空结果但须为合法返回） |
| 既有 96 例 | **不得回归**（`pytest tests/ -q` 全绿） |

> mock 方式：monkeypatch `ai.mcp_client.call_tool` / `enabled`，或注入一个假的 `ClientSession`；真实集成用例通过 stdio 拉起子进程，需能被 CI/本地 pytest 跳过（如无 `mcp` 依赖或子进程不可用时 `skip`）。

---

## 7. 权限与隔离

- `user_id` 由 client 侧从 `ctx["user_id"]` 取出，作为 MCP 显式入参传入 server；
- **server 不做鉴权**（维持"客户端已校验"口径）：服务端拿到的 `user_id` 直接透传给 `search_library`，由 `_apply_filters` 执行 `id IN (SELECT … WHERE s.user_id = %s)` 子查询；
- `_apply_filters` 的放宽链（去年份 → 再去来源）**永不放宽 user_id 隔离**——本批次不改该规则；
- 客户端侧仍保留"缺 `user_id` 即 `_fail('越权…')`"的前置校验（`_library_search` 回落路径与 MCP 入参构造路径**都要**该校验）。

---

## 8. 降级与可观测

| 场景 | 行为 |
|---|---|
| `LITRADAR_MCP_ENABLED=0` | 3 个工具全走进程内 `_DISPATCH`，与现状逐字一致 |
| `mcp` 包缺失 / 导入失败 | 惰性导入捕获异常 → 判不可用 → 回落进程内（不影响链路） |
| 子进程启动失败 / 握手失败 | 判不可用 → 回落进程内 + 打印一次告警 |
| `call_tool` 超时 / 协议错误 | 判本次失败 → 回落进程内；标记不可用，后续不再重连 |
| 回落路径 | 复用 `_DISPATCH`，`tool_calls` 与 `trace.rec.tool(...)` 记录口径**不变** |

> 可观测：回落后**仍写入 `session["tool_calls"]`**（由 `_execute_tool_calls` 统一落盘），前端"工具调用记录"与评测 trace 不受影响。

---

## 9. 前端契约影响

| 项 | 影响 |
|---|---|
| `steps` | 零变更（控制流不动） |
| `tool_calls` | 零变更（工具名/入参/摘要口径一致，仅执行通道可能不同） |
| `review_notes` | 零变更 |
| `result` | 零变更（`answer`/`points`/`citations` 结构不变） |
| 首次调用延迟 | 首个 MCP 工具调用需 spawn 子进程并握手，引入**一次性**额外延迟（后续复用会话） |

---

## 10. 验收方案

1. **单测**：`pytest tests/ -q` 全绿（既有 96 + 新增用例），新增用例**先红后绿**（TDD）。
2. **端到端对照**：`eval/run_eval.py` 跑全量 23 题，与批次 1 基线 `eval/record/测试记录_20261020_124326.txt`（命中 **16/23**、token **74257**、复审 **7/7 pass**、运行期异常 **0**）对照。
3. **等价性判据**：MCP 路径与进程内路径**调用同一底层函数**，命中数不应实质漂移（落在历史波动带内）；token 落在历史带内；异常 0。
4. **降级验证**：额外跑一遍 `LITRADAR_MCP_ENABLED=0`，结果应与启用 MCP 时落在同一波动带（证明回落链路可用）。
5. **文档同步**：登记《文档变更记录》新章节（§34）。

---

## 11. 风险与代价

| 风险 | 说明 | 处置 |
|---|---|---|
| **子进程生命周期** | stdio 子进程随编排层进程存活；异常退出后本进程内不再重连 | 会话单例 + 失败标记；单次会话不重启（避免频繁 spawn）；如需重连另案 |
| **异步桥接复杂度** | 同步编排 → async SDK，需要专用 loop 线程 + `run_coroutine_threadsafe` | 封装在 `mcp_client` 内，工具层仅见同步 `call_tool` |
| **首调延迟** | 首次调用 spawn + 握手 | 模块级单例复用；评测时该延迟计入首个工具调用耗时 |
| **循环依赖** | client 在 `ai.tools`，server 亦在 `ai` 包 | server **不 import `ai.tools`**；client 惰性 import `mcp` SDK |
| **依赖缺失** | `mcp` 未装时 `ai.tools` 导入告警 | 惰性导入 + 判不可用回落，不阻断启动 |
| **评测口径漂移** | 走 MCP 后执行通道变了，可能影响耗时 | 命中/token 为主判据，耗时仅记录 |

---

## 12. 与《Summarizer前移审草稿设计》§9 的差异

| 项 | §9 原表述 | 本设计 | 原因 |
|---|---|---|---|
| 依赖 | 补 `mcp` **与** `langchain-mcp-adapters` | **只补 `mcp`** | D4 选定官方 SDK 直调，不引入 LangChain Tool 抽象 |
| 传输 | 未定 | **stdio 子进程** | D3 |
| 降级开关 | "MCP 不可用 → 回落"（未定开关） | `LITRADAR_MCP_ENABLED` 默认开 + 自动回落 | D7 |
| 测试 | 未定 | mock 会话 + 1 例真实 stdio 集成 | D9 |

---

## 13. 未提交状态说明

工作区在本分支上仍有 §31 / §32 的未提交改动（`ai/summarizer.py`、`ai/retriever.py`、`ai/vector_recall.py`、`ai/rerank.py`、`ai/llm_provider.py`、`eval/run_eval.py`、`tests/*`、`doc/*`、`.gitignore`、`requirements.txt`、`pyproject.toml`）——**注意：截至本设计撰写时，`git diff --numstat HEAD` 为空**，这些文件在 `git status` 中仅表现为 EOL（LF↔CRLF）噪声，无内容差异。本次 MCP 改动（新增 `ai/mcp_server.py`、`ai/mcp_client.py`，改 `ai/tools.py`、`requirements.txt`、测试）与既有改动的**归属须在提交时分开处理**（§33 / §34 分属不同变更）。
