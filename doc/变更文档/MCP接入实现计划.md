# 工具层 MCP 接入 · 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 3 个无状态工具（`library_search` / `web_search` / `rewrite_query`）迁到自建 stdio MCP server，编排层工具层变**混合层**（MCP 优先 → 失败回落进程内 `_DISPATCH`）；**控制流零改动**，前端契约零变更。

**Architecture:** 新增 `ai/mcp_server.py`（FastMCP，复用 `ai.retriever` / `ai.rewrite`，**不 import `ai.tools`**）+ `ai/mcp_client.py`（专用 loop 线程桥接 async 会话，模块级单例，失败即标记不可用）；`ai/tools.py` 的 `execute()` 入口分流：3 个 MCP 工具先走 client，成功按进程内口径**逐字重建** `{ok,brief,data}`，失败/未启用回落 `_DISPATCH`；其余 3 个工具永远进程内。

**Tech Stack:** Python 3.11 / `mcp>=1.27.0`（FastMCP + `ClientSession` + `stdio_client`）/ pytest / 现有 `ai/*` 栈（**不引入 `langchain-mcp-adapters`**）

**依据文档：** [MCP接入设计.md](file:///d:/实习/my_project/deep_search_pro-master/doc/变更文档/MCP接入设计.md)（spec，已评审通过）

---

## Global Constraints

- **单一变量原则**：本批次**只动工具层**（新增 2 模块 + 改 `ai/tools.py` + 补依赖）；`ai/orchestrator.py` 的控制流（`rewrite/plan/execute_tools/summarize/review/finalize/quick/fallback`）**一行不改**。
- **`app/*`、`web/*` 一行不改**；`review_notes` 三字段契约零变更。
- **业务逻辑零重写**：server 端直接调 `retriever.search_library` / `retriever.search_web` / `rewrite.rewrite_query`，不复制逻辑、不改这些函数。
- **`brief` 文案逐字一致**：MCP 成功路径的 `brief` 必须与进程内实现**逐字相同**（进前端面板与评测记录，改字即变更）：
  - `library_search` → `f"命中 {len(evidence)} 篇（user_id={uid} 隔离）"`
  - `web_search` → `f"网络命中 {len(evidence)} 条"`
  - `rewrite_query` → `f"{'已改写' if struct.get('rewritten') else '回退原始问句'}：{' '.join(struct.get('keywords') or [])[:60]}"`
- **权限口径不变**：`user_id` 作 MCP **入参显式传入**，server 不做鉴权；`_apply_filters` 的 user_id 子查询规则不变；缺 `user_id` 时 `library_search` 在**client 侧前置拒绝**（不发起 MCP）。
- **降级**：`LITRADAR_MCP_ENABLED` 默认开（`!= "0"`）；`mcp` 缺失 / 子进程起不来 / 握手失败 / 调用超时 → 回落进程内，链路不中断。
- **依赖下限**：补 `mcp>=1.27.0` 到 `requirements.txt` **与** `pyproject.toml` 的 `dependencies`（两处必须一致，文件头注释既有约定）。
- **验收门禁**：`pytest tests/ -q` 全绿（既有 **96** + 新增）→ 全量 `eval/run_eval.py` 对照基线 `eval/record/测试记录_20261020_124326.txt`（命中 **16/23**、token **74257**、复审 **7/7 pass**、运行期异常 **0**）→ 另跑一遍 `LITRADAR_MCP_ENABLED=0` 验证回落链路。
- **提交策略**：改动留工作区待审，**由用户确认后再提交（不自动提交）**。
- **环境**：解释器 `D:\Anaconda\envs\shixi\python.exe`；工作目录 `d:\实习\my_project\deep_search_pro-master`；PowerShell 5（**不支持 `&&`**，用 `;`）；命令前加 `$env:PYTHONIOENCODING="utf-8"`；临时脚本放 `C:\Users\Liangchangzhen\AppData\Local\Temp\`。

---

## Phase 1 · RED：先落失败测试

### Task 1: 混合工具层单测（`tests/test_mcp_tools.py`）

**Files:**
- Create: `tests/test_mcp_tools.py`

**Interfaces:**
- Consumes: `ai.tools.execute(name, args, ctx)`、`ai.tools._MCP_TOOLS`、`ai.mcp_client.enabled/call_tool`、`ai.retriever.search_library/search_web`、`ai.rewrite.rewrite_query`
- Produces: 断言集覆盖「开关语义 / MCP 成功路径 / 失败回落 / 未启用走进程内 / 越权前置拒绝 / 其余 3 工具永不走 MCP」

- [x] **Step 1: 写用例（monkeypatch 风格对齐 `tests/test_retriever_recall.py`）**

用例清单：
1. `test_enabled_default_on_and_off`：`LITRADAR_MCP_ENABLED` 缺省 → `enabled() is True`；设 `"0"` → `False`。
2. `test_library_search_via_mcp`：mock `mcp_client.call_tool` 记录入参并返回 `[{"id":1,"title":"t"}]`；`ctx={"user_id":7,"query_struct":{"keywords":["a"],"filters":{"year_from":"2025"}}}` → `res["ok"] is True`、`res["brief"]=="命中 1 篇（user_id=7 隔离）"`、`res["data"]` 长度 1、mock 入参含 `user_id==7` 与 `year_from=="2025"`。
3. `test_web_search_via_mcp`：mock 返回 `[]` → `res["brief"]=="网络命中 0 条"`。
4. `test_rewrite_query_via_mcp`：mock 返回 `{"rewritten":False,"keywords":["x"]}` → `res["brief"]` 以 `"回退原始问句"` 开头。
5. `test_mcp_failure_falls_back`：`call_tool` 返回 `None`；mock `retriever_mod.search_library` 返回 `[{"id":9}]` → `execute("library_search", {}, {"user_id":3})` 仍 `ok` 且 brief 为 `命中 1 篇（user_id=3 隔离）`。
6. `test_disabled_uses_inprocess`：`LITRADAR_MCP_ENABLED=0`；mock `call_tool` 记录「是否被调用」→ 断言**未被调用**，且回落 `search_library` 生效。
7. `test_library_search_without_user_id_rejects_before_mcp`：`ctx={}`（无 user_id）→ `ok is False`、`"越权" in brief`，且 `call_tool` **未被调用**。
8. `test_inprocess_only_tools_never_mcp`：`set(tools._MCP_TOOLS) == {"library_search","web_search","rewrite_query"}`，且 `memory_search` 不在其中。

- [x] **Step 2: 跑测试 → 确认 RED**

```powershell
$env:PYTHONIOENCODING="utf-8"; D:\Anaconda\envs\shixi\python.exe -m pytest tests/test_mcp_tools.py -q
```

期望：**收集失败/大量 FAIL**（`_MCP_TOOLS`、`ai.mcp_client` 尚不存在）。

### Task 2: MCP stdio 集成冒烟（`tests/test_mcp_server_integration.py`）

**Files:**
- Create: `tests/test_mcp_server_integration.py`

**Interfaces:**
- Consumes: `ai.mcp_client.list_tools()`、`ai.mcp_client.call_tool(name, arguments)`
- Produces: 真实子进程 stdio 链路可用性证据（`list_tools` 含 3 工具 + `rewrite_query` phrase 模式可调用）

- [x] **Step 1: 写用例**

`test_stdio_smoke`：`pytest.importorskip("mcp")`；`LITRADAR_MCP_ENABLED=1`；`names = mcp_client.list_tools()`；`names is None` → `pytest.skip("MCP server 子进程不可用")`；断言 `set(names) == {"library_search","web_search","rewrite_query"}`；再 `call_tool("rewrite_query", {"user_query":"对比两种方法","mode":"phrase","enabled":False})` → 返回值是含 `keywords` 的 dict（phrase 模式确定性，零 LLM 零库）。

- [x] **Step 2: 跑测试 → 确认 RED**

```powershell
$env:PYTHONIOENCODING="utf-8"; D:\Anaconda\envs\shixi\python.exe -m pytest tests/test_mcp_server_integration.py -q
```

期望：FAIL（`mcp_client` 不存在）。

---

## Phase 2 · GREEN：实现到全绿

### Task 3: `ai/mcp_client.py`（stdio 会话单例 + 同步桥接 + 失败标记）

**Files:**
- Create: `ai/mcp_client.py`

**Interfaces:**
- Consumes: `mcp` SDK（惰性 import）、`ai/mcp_server.py`（子进程入口）
- Produces:
  - `enabled() -> bool`
  - `list_tools() -> list[str] | None`（不可用 → `None`）
  - `call_tool(name: str, arguments: dict) -> list | dict | None`（不可用/失败 → `None`）

实现要点：
- 模块级：`_lock`、`_loop`、`_loop_thread`、`_state{session}`、`_unavailable`（失败标记）。
- 专用 daemon 线程跑 `loop.run_forever()`；`_boot()` 协程以 `stdio_client` + `ClientSession` 打开会话并 `initialize()`，**持有两个 async CM 引用**避免被 GC 关闭。
- `StdioServerParameters(command=sys.executable, args=["-m","ai.mcp_server"], cwd=PROJECT_ROOT, env={**os.environ, "PYTHONPATH": PROJECT_ROOT, "PYTHONIOENCODING":"utf-8"})`。
- 同步入口：`asyncio.run_coroutine_threadsafe(coro, _loop).result(timeout=_timeout())`；`LITRADAR_MCP_TIMEOUT` 默认 30s。
- `_parse(res)`：优先 `res.structuredContent`（若形如 `{"result": …}` 则取内层），回落取 `content` 首个 `text` 块 `json.loads`；均无 → `None`。
- 任一失败 → 打印一次告警 + 置 `_unavailable=True`，后续 `call_tool` 直接返回 `None`（不反复 spawn）。

- [x] **Step 1: 写实现** → **Step 2: 跑 Task 2 集成测试**（此时应 GREEN 或 skip）

### Task 4: `ai/mcp_server.py`（FastMCP 暴露 3 个无状态工具）

**Files:**
- Create: `ai/mcp_server.py`

**Interfaces:**
- Produces: `library_search(...) / web_search(...) / rewrite_query(...)`（`@mcp.tool()`，返回 JSON 文本）+ `main()`

实现要点：
- `from mcp.server.fastmcp import FastMCP`；`mcp = FastMCP("litradar-tools")`。
- 工具入参显式：`library_search(keywords, year_from, year_to, sources, cats, user_id)`；`web_search(keywords, year_from, year_to, sources, cats)`；`rewrite_query(user_query, mode, enabled)`。
- 内部用私有 `_struct(...)` 组装检索式（filters 四字段 `year_from/year_to/sources/cats` 全传，避免丢过滤维度），调既有函数，`json.dumps(..., ensure_ascii=False)` 返回。
- `main()` → `mcp.run(transport="stdio")`；`__main__` 调 `main()`。
- **不 import `ai.tools`**。
- **必须预热（实现中发现的阻塞项，详见《文档变更记录》§34.2）**：`main()` 内先 `import sentence_transformers` 再 `mcp.run()`。若该重依赖的首次导入发生在工具调用阶段，会与 stdio 事件循环的并发 import 争抢 **Windows 加载器锁**而整体死锁（`library_search` 永久挂起、30/120/180s 均超时）。

- [x] **Step 1: 写实现** → **Step 2: 手动冒烟**

```powershell
$env:PYTHONIOENCODING="utf-8"; D:\Anaconda\envs\shixi\python.exe -m pytest tests/test_mcp_server_integration.py -q
```

### Task 5: `ai/tools.py` 混合层 + 依赖声明

**Files:**
- Modify: `ai/tools.py`
- Modify: `requirements.txt`、`pyproject.toml`

**Interfaces:**
- Consumes: `ai.mcp_client.call_tool`（惰性 import）
- Produces: `execute()` 分流 + `_MCP_TOOLS` + `_mcp_params` + `_try_mcp` + `_mcp_result`

实现要点：
- 模块头注释补「混合工具层」说明。
- `_MCP_TOOLS = ("library_search","web_search","rewrite_query")`。
- `_mcp_params(name, args, ctx)`：`library_search` 无 `user_id` → 返回 `None`（**前置拒绝，不发 MCP**）；否则用既有 `_merge_struct` 合并条件并展开为显式入参（含 `cats`）。
- `execute(name, args, ctx)`：`name ∈ _MCP_TOOLS` → `_try_mcp` 非 `None` 则返回；否则原逻辑走 `_DISPATCH`。
- `_try_mcp`：惰性 `from ai import mcp_client`；`params is None` → `None`；调 `call_tool`；异常 → 打印 + `None`；`data is None` → `None`；否则 `_mcp_result`。
- `_mcp_result(name, args, ctx, data)`：按 Global Constraints 的 brief 模板**逐字重建** `_ok(brief, data)`。
- `_DISPATCH` 与 `_run_*/_library_search/...` 实现体**不动**（作为回落实现保留）。
- `requirements.txt` / `pyproject.toml` 加 `mcp>=1.27.0`。

- [x] **Step 1: 写实现** → **Step 2: 跑 Task 1 单测 → GREEN**

### Task 6: 全量单测绿（门禁 1）

- [x] **Step 1:**

```powershell
$env:PYTHONIOENCODING="utf-8"; D:\Anaconda\envs\shixi\python.exe -m pytest tests/ -q
```

期望：既有 96 + 新增全绿。

---

## Phase 3 · 端到端对照（门禁 2）

### Task 7: 全量 23 题对照（MCP 开 / 关各一轮）

**Files:**
- Create: `eval/record/测试记录_<时间>.txt`（gitignore，不入库）
- Modify: `eval/run_eval.py`（`_STRATEGY_FLAGS` 同步登记 `LITRADAR_MCP_ENABLED`）

- [x] **Step 1:** MCP 开启（默认）跑全量，对照 16/23、74257、7/7、异常 0。
- [x] **Step 2:** `LITRADAR_MCP_ENABLED=0` 再跑一轮，确认落在同一波动带。
- [x] **Step 3:** 测试记录「测试配置」段登记 MCP 开关状态（变更项标「（已修改）」）。

**实际结果：** MCP 开 → `测试记录_20261020_164133.txt` **16/23、77029、7/7 pass、异常 0**（运行日志 13 次 `CallToolRequest`、**0 次**「MCP 通道不可用」告警，即全程未回落）；MCP 关 → `测试记录_20261020_170036.txt` **15/23、71038、7/7 pass、异常 0**。唯一逐题差异为 **Q5**（开 3/3、关 2/3），源于 planner 关键词由 LLM 生成的非确定性，与 MCP 通道无因果关系。另：记录文件头的 `MCP开关` 行系本次改动新增，两轮记录生成时尚无该行，其开/关归属以《文档变更记录》§34.3 表格为准。

---

## Phase 4 · 文档登记

### Task 8: 《文档变更记录》§34

- [x] **Step 1:** 登记 §34：改动范围、决策（D3/D4/D7）、两次对照结果、回落链路证据、未纳入项（`memory_search`/`run_analysts`/`run_reviewer` 仍进程内）。

**实际结果：** 已完成，见《文档变更记录》**§34**（含 §34.2 的「Windows 加载器锁死锁」根因与 `_warmup()` 修复）。
