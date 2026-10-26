# Summarizer 前移至 Reviewer 之前（方案 B：给 reviewer 草稿正文）设计

> 状态：**已实现并验收**（见《文档变更记录》§33；代码在分支 `feat/summarize-before-review`，未提交）
> 分支：`feat/summarize-before-review`
> 关联：`doc/变更文档/Reviewer复审清单自适应设计.md` §2.2、`doc/变更文档/LangGraph编排重构设计.md` §2.2（两处均把本项列为「另案处理」）

---

## 1. 背景与问题

### 1.1 现状流程

主 QA 图（`ai/orchestrator.py` 的 `_build_pipeline_graph()`）：

```
rewrite ─┬─ quick ────────────────────────────────────────────→ END
         └─ plan → execute_tools → review ─┬─ retry ─→ plan
                                        └─ ────────→ finalize（汇总 + ⚠ 标注）
```

`_node_finalize()` 承担三件事：落迭代次数 → 补齐检索节点状态 → 汇总作答（`_step_summarize`）。

### 1.2 根因

Reviewer 的入参是 `review(question, evidence, analyst_outputs)`——**没有任何待交付正文**（`ai/reviewer.py` §review 签名）。因此它只能审「手上有材料可评的东西」：证据、分析师结论。

后果（`Reviewer复审清单自适应设计.md` §1.3 原文已指出）：

> 于是 reviewer 实际审的是 **Planner 的调度决策**（有没有调分析师），而不是待交付内容的质量。

即：质检环节校验的是**过程**，不是**产物**。幻觉校验一项形同虚设——因为没有正文可核。

### 1.3 为什么现在做

`Reviewer复审清单自适应设计.md` 已把「未触发多视角分析」从判否依据降级为 suggestion，暂时压住了症状；但只要 reviewer 手上没有正文，「审产物」这件事就做不到。本次把汇总**前移**到 review 之前，从结构上解决。

---

## 2. 目标与非目标

### 2.1 目标

1. Reviewer 的判定对象从**证据/过程**改为**待交付草稿**：新增「草稿正文」入参，检查清单第 ① 项（幻觉校验）改为**对草稿核验引用真实性**。
2. Reviewer 驳回时**带 suggestion 打回 Planner**（沿用既有 retry 通道，不新造闭环）。
3. `review_notes` 三字段（`passed` / `defects` / `suggestions`）与前端契约零变更。
4. `app/*`、`web/*` 一行不改。

### 2.2 非目标（明确不做）

- 不改 `REVIEW_MAX_TOKENS` 与 `_salvage()` 截断补救逻辑；
- 不改 `SUMMARIZER` 的 system prompt 与写作规则（§31 刚定稿）；
- 不改 `review_notes` 返回结构与前端契约字段；
- **不改 quick 通道 / v3 回退通道的流程**（二者本就跳过 review，维持现状）；
- 不改 Planner 提示词、分析师调度逻辑；
- 本次**不接 MCP**（属批次 2，另案；见 §9）。

---

## 3. 已确认的关键决策

| # | 决策 | 结论 |
|---|---|---|
| D1 | 改动性质 | 控制流改动（agent 结构重排），**不含工具层改动**（单一变量原则） |
| D2 | 图结构 | 新增 `summarize` 节点；`execute_tools → summarize → review → …` |
| D3 | 驳回处置 | 带 suggestion **打回 Planner**（复用既有 `retry → plan`），不新增闭环形态 |
| D4 | 汇总成本 | **接受每轮生成草稿**带来的汇总调用翻倍；验收时重设 token 基准带 |
| D5 | `finalize` 职责 | 收窄为「落迭代次数 + 补齐检索节点状态 + ⚠ 标注」，**不再汇总** |
| D6 | reviewer 入参 | `review(question, evidence, analyst_outputs, draft=None)`，`draft` 为可选（缺省行为与现状一致） |
| D7 | git 工作方式 | 开工作分支，**不提交**（改动留工作区待审） |

---

## 4. 图变更

### 4.1 改后结构

```
rewrite ─┬─ quick ──────────────────────────────────────────────────────→ END
         │                                                        （内部：汇总 → ⚠ 标注）
         ├─ fallback ─────────────────────────────────────────────────→ END
         │                                                        （内部：汇总 → ⚠ 标注）
         └─ plan → execute_tools → summarize → review ─┬─ retry ─→ plan
                                                      └─ ────────→ finalize（仅 ⚠ 标注）
```

### 4.2 节点与边

| 项 | 现状 | 改后 |
|---|---|---|
| 节点数 | 7（rewrite/quick/plan/execute_tools/review/finalize/fallback） | 8（新增 `summarize`） |
| 边 | `execute_tools → review` | `execute_tools → summarize`、`summarize → review` |
| `_route_after_review` | `retry → plan`，否则 `finalize` | **不变** |
| `_route_after_plan` | `plan_degraded → finalize` | `plan_degraded → **summarize**`（否则本轮回合无汇总产出） |
| `quick` / `fallback` | 节点内自行汇总 → END | **不变**（节点内维持「汇总 + ⚠ 标注」） |

---

## 5. 改动点清单

### 5.1 `ai/orchestrator.py`

| 位置 | 改动 |
|---|---|
| `_step_summarize()` | **摘出 ⚠ 标注逻辑**，降为纯汇总：只生成 `session["result"]` 并返回 detail 字符串 |
| 新增 `_apply_alerts(session)` | 承载 ⚠ 标注：按 `review_skipped` / `iteration_limit` 前置标注到 `result["answer"]` |
| 新增 `_node_summarize(state)` | `_step(session, "summarize", _step_summarize)` → `{"session": session}` |
| `_node_finalize()` | 去掉 `_step_summarize` 调用，改为调 `_apply_alerts(session)`；保留 `iteration_count` 与 `_flush_retrieval_nodes` |
| `_node_quick()` | 节点内 `_step_summarize` 后**补调 `_apply_alerts`**（等价性：quick 路径两标志均未置位 → 不产生标注，行为不变） |
| `_node_fallback()` | 同上；fallback 置 `review_skipped=True` → 保留既有 ⚠ 标注，行为不变 |
| `_node_review()` | **实现补充（设计时遗漏）**：`_maybe_backfill_analysts()` 补跑成功且本轮回合**收尾**（复审通过 / 达轮次上限）→ 重跑 `_step_summarize` 刷新草稿。否则末轮草稿产出于复审之前，会丢失复审刚补跑出来的分析师内容（§30 多视角需求通道空转） |
| `_route_after_plan()` | `plan_degraded` 目标由 `finalize` 改为 `summarize` |
| `_build_pipeline_graph()` | `add_node("summarize", _node_summarize)`；边改为 `execute_tools → summarize → review` |

### 5.2 `ai/reviewer.py`

| 位置 | 改动 |
|---|---|
| `_PROMPT_FULL` / `_PROMPT_EVIDENCE_ONLY` | 新增 `{draft}` 占位块「待交付草稿（编号为引用标识）」；检查清单第 ① 项由「结论中引用…」改为「**草稿正文**中引用的文献编号必须真实存在于下方证据」 |
| `review()` | 签名加 `draft: str \| None = None`；`draft` 为空串时回填「（本轮未产出草稿）」占位（**不判否**） |
| `_salvage()` / `_clean_views()` | 不改 |

### 5.3 `ai/tools.py`

| 位置 | 改动 |
|---|---|
| `_run_reviewer()` | 从 `ctx` 取草稿正文（`ctx.get("result", {}).get("answer", "")`）传入 `reviewer_mod.review(...)`；输入非空校验维持「question + evidence」口径（**不把草稿为空判为失败**，因汇总可能降级为模板） |

### 5.4 测试

| 文件 | 改动 |
|---|---|
| `tests/test_reviewer_*.py` | 新增：`draft` 入参进入 prompt；`draft` 缺省时 prompt 回填占位且不影响 pass 判定 |
| `tests/test_orchestrator_paths.py` | **必改**：四路径集成测试的节点顺序断言（`summarize` 前移、`finalize` 不再汇总） |
| 「汇总前移」用例组（**实现偏差**：并入上文件，未另起新文件） | 新用例需复用该文件的 `stub` 夹具，另起文件须复制整套 patch 装配（违反 DRY），故合并。断言：图拓扑（`execute_tools → summarize → review`、旧直连移除）；deep 路径每轮生成草稿；`finalize` 不再汇总；无草稿时 finalize 安全；补跑后刷新草稿；`plan_degraded` 仍产出答案；quick/fallback 的 ⚠ 标注行为与改前逐字一致 |

---

## 6. 前端契约影响（需知悉）

| 项 | 影响 |
|---|---|
| `steps` 节点**顺序** | `…→ review → summarize` 变为 `…→ summarize → review`。字段集合不变，仅顺序变化 |
| `steps` 节点**数量** | planner 路径两节点均保留，总数不变 |
| `review_notes` | 零变更（字段与语义不变） |
| `result` | 零变更（`answer` / `points` / `citations` 结构不变） |
| ⚠ 标注位置 | 改后由 `finalize` 追加在 `result["answer"]` 前，**最终输出与改前一致** |

---

## 7. 验收方案

1. **单测**：`pytest tests/ -q` 全绿；新增用例先红后绿（TDD）。
2. **端到端对照**：`eval/run_eval.py` 跑全量 10 题，与 §32 有效记录 `测试记录_20261020_104228.txt`（命中 16/23、token 86712、复审 7/7 pass）对照。
3. **命中数**：必须仍落在历史波动带 **11–15/23** 的既有区间语义内（本次不引入检索层改动，命中数**不应**发生实质漂移）。
4. **token**：**基准带重设**（D4）——**实测修正**：deep 题本轮全部 1 轮收敛（`MAX_ITERATION=3` 未触顶），改后 74257 反而低于改前 86712，仍落在历史带 61659–97479 内，**无需重设**；已在测试记录「测试配置」段登记 `汇总时机=先汇总后复审(审草稿)（已修改）`。
5. **运行期异常为 0**；复审 `pass` 数不低于改前。
6. **文档同步**：登记《文档变更记录》新章节（§33）。

---

## 8. 风险与代价

| 风险 | 说明 | 处置 |
|---|---|---|
| **token 随轮次上升** | deep 题最多 3 轮，每轮一次 `main` 汇总（max_tokens 4096） | 已确认接受；**实测修正**：1 轮收敛时与改前近似（74257 < 86712），未触发基准带重设 |
| **reviewer 双重职责** | 既审证据充足度、又审草稿质量，prompt 变长可能挤压 JSON 输出预算 | 保持 `REVIEW_MAX_TOKENS=1024` 不变，先实测；若 `_salvage` 触发率上升再另案调整 |
| **`plan_degraded` 环路** | 改后 `plan_degraded → summarize → review` 可能触发 retry 再次重规划 | 由 `_node_review` 的 `round_no` 自增 + `max_rounds` 上限兜底，轮次有界 |
| **草稿降级为模板时被判定** | LLM 不可用时 `synthesize` 走 `_fallback_answer`，草稿是文献清单而非论述 | reviewer 对模板草稿的合理反应是「证据类」判否（合法），不视为缺陷 |

---

## 9. 批次 2（另案，本次不做）：MCP 接入

已确认方案（待批次 1 验收通过后启动）：

- **方向**：自建 MCP server（官方 `mcp` SDK）暴露无状态 3 工具 —— `library_search` / `web_search` / `rewrite_query`；编排层改 MCP client 调用。
- **不接的 3 个**：`memory_search`（吃 `session_id` + 会话历史）、`run_analysts`（吃 `ctx["evidence"]` 对象引用）、`run_reviewer`（吃 `evidence + analyst_outputs`）——过协议需全量序列化，收益不抵成本，保持进程内。
- **工具层形态**：`ai/tools.py` 变混合层（MCP client + 进程内 `_DISPATCH`）。
- **降级**：MCP server 不可用 → 回落进程内实现，链路不中断。
- **依赖**：`requirements.txt` 补 `mcp`（环境已装 1.27.0）与 `langchain-mcp-adapters`（已装 0.2.2）——**当前未声明**，需补。
- **权限隔离**：`user_id` 必须作为 MCP 入参显式传入，服务端不做鉴权（维持「客户端已校验」口径），`_apply_filters` 的 user_id 子查询规则不变。

---

## 10. 未提交状态说明

工作区在本分支之前已积压 §31 / §32 的未提交改动（`ai/summarizer.py`、`ai/retriever.py`、`ai/vector_recall.py`、`ai/rerank.py`、`ai/llm_provider.py`、`eval/run_eval.py`、`tests/*`、`doc/*`、`.gitignore`、`requirements.txt`、`pyproject.toml`）。开分支时这些改动已一并带入 `feat/summarize-before-review`，**本次改动与它们的归属需在提交时分开处理**（§31 / §32 / §33 分属三次变更）。
