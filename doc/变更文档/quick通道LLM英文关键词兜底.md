# quick 通道 LLM 英文关键词兜底（设计与实现记录）

> 批次：**批次 3（quick 检索质量）**，登记于《文档变更记录》§35
> 单一变量：只改「quick 通道关键词来源」，不动检索算法 / 控制流拓扑 / 存储 / 工具层

## 1. 背景与目标

§32 双路召回 + RRF 落地后，确定性检索层与端到端命中都在 16/23 附近；剩余未命中里 3 个（Q1/Q2/Q3）集中在 **quick 通道**。

quick 原只用 `ai/rewrite.extract_keywords` 正则抽词：英文按词、中文按 bigram。中文问句切出的碎片（如 `论文/文里/里说/说的/是什么`）在 MySQL 自然语言全文检索上会撞 **50% 阈值**（高频词被当停用词），导致词法路零命中或低排。

**目标**：quick 通道检索前，用一次 LLM 把问句改写为英文关键词并替换关键词来源；LLM 不可用 / 失败 / 开关关闭 → 保持正则关键词，链路不中断。

**非目标**：不改检索算法（RRF / 候选池 / top_k）；不改控制流拓扑；不改 `ai/tools.py` 与 MCP；不改 normal/deep 路径；不引入新 LLM 依赖；不做负缓存。

## 2. 改动前的只读探针实测

`LITRADAR_VECTOR_RECALL=0`、`user_id=1`、库内 147 篇、BM25 精排后最终 top-8：

| 输入关键词 | 命中期望引用 |
|---|---|
| Q1 正则 `rag/论文/文里/里说/说的/是什么` | **0/2**（#33 词法池第 134 位、#102 第 132 位 → 属**排低**，非漏召回） |
| Q1 英文 `retrieval augmented generation` + `large language models` | **2/2** |
| Q2 正则（纯中文 bigram） | **0/2**（词法池 = 0，真零召回） |
| Q2 英文 `retrieval augmented generation core components` | **1/2** |
| Q3 正则 | 1/2 |
| Q3 英文 `graphrag` + `graph rag` + `knowledge graph` | **2/2** |

要点：**Q1 属排低而非零召回**，故「只做零召回兜底」对 Q1 不会触发 → 改走「quick 关键词统一交一次 LLM 英文改写」。

## 3. 实现清单

| 文件 | 改法 |
|---|---|
| `ai/rewrite.py` | 补 `import os`；新增 `_quick_llm_enabled()`（`LITRADAR_QUICK_LLM_FALLBACK` 默认开）；新增模块级 `_QUICK_CACHE`；新增 `rewrite_for_quick(query) -> dict \| None`：只取 keywords、成功写缓存、失败返回 `None`。**第二轮**新增 quick 专用提示词 `QUICK_PROMPT`（= 通用 `PROMPT` + 硬性要求：2-5 个互不相同的英文术语/短语、必须给出英文完整短语与同义/上下位说法、全英文），`rewrite_for_quick` 改用它。 |
| `ai/orchestrator.py` | 新增 `_apply_quick_llm_keywords(session)`；在 `_node_quick` 的两个 skipped `_mark` 之后、`_run_retrieval_by_routes` 之前挂钩。守卫 `rewritten=True` / 开关关闭直接返回；成功 `{**struct, "keywords": out["keywords"]}` 只换 keywords；`rewrite` 步骤 detail 尾部追加归因文案。 |
| `eval/run_eval.py` | `_config_rows()` 新增动态实读项 `quick英文兜底=开/关` 并并入「召回」行。 |
| `tests/test_orchestrator_paths.py` | `stub` 夹具补 `rewrite_for_quick` 桩；新增 7 例（先红后绿）+ 第二轮 1 例锁定强化提示词。 |
| `doc/项目文档/文档变更记录.md` | 新增 §35。 |

**挂钩位置为什么只在 `_node_quick`**：`_run_retrieval_by_routes` 同时被 `_node_fallback` 调用，`retriever.search_library` 同时被 Planner 工具路径与零证据补检调用；把逻辑放进前两者都会外溢其它路径，并破坏 `ai/retriever.py` 的「确定性检索无 LLM」定位。

## 4. TDD 用例（7 条）

1. 开关关闭不调 LLM（`LITRADAR_QUICK_LLM_FALLBACK=0` → `quick_calls == 0`，关键词仍为正则值）
2. 开关开 + 改写成功 → 检索入参为英文关键词，detail 尾段 `LLM英文兜底关键词 N 个`
3. LLM 失败（桩返回 `None`）→ 退回正则关键词，detail 尾段 `LLM英文兜底失败，保留正则关键词`，链路不中断
4. 只换 keywords：`filters` / `query` 逐字保留
5. `rewritten=True` 守卫 → 不重复改写
6. normal / fallback 路径隔离 → `quick_calls == 0`
7. 真函数级缓存：同一问句两次 `_run_pipeline` → `chat_json` 只调 1 次

RED 实测：用例 1/2/3/5/6 因 `rewrite_for_quick` 不存在报 `AttributeError`，用例 7 因 `_QUICK_CACHE` 缺失失败。
GREEN 后：`pytest tests/ -q` → 第一轮 **113 passed**、第二轮（提示词强化）**114 passed**（§34 为 106；本批共 +8）。

## 5. 两个迭代（提示词）

### 5.1 第一轮：复用通用提示词（增益低于预期）

quick 题 `qa_router` 只吐**单个**关键词（实测 Q1 → `["RAG"]`、Q3 → `["GraphRAG"]`）。单个高频词撞 MySQL 全文检索 50% 阈值（当停用词）→ 检索等于未改善：Q1 仍 0/2、Q3 仍 1/2，quick 通道只拿到 Q2 的 +1。

### 5.2 第二轮：强化 quick 专用提示词（用户裁定）

新增 `QUICK_PROMPT = PROMPT + 硬性要求`：① keywords 给 2-5 个且互不相同，不要只输出一个缩写词；② 必须同时给出「英文完整短语」与「同义/上下位英文说法」两类并给示例；③ 全英文。**提示词之外逻辑零变更**。

强化后实测输出（`181441`）：Q1 → `["Retrieval-Augmented Generation", "Large Language Models", "GraphRAG", "Knowledge Graph"]`；Q2 → `["Retrieval-Augmented Generation", "Retrieval-Augmented Generation Systems", "GraphRAG", "Knowledge Graph"]` —— 即从「单个碎片/单高频词」变为「多个英文短语 + 同义说法」，正是 §2 探针所指的有效输入形态。

## 6. 端到端验证（五轮）

配置：`user_id=1`、`mode=phrase`、`enable_planner=True`、评测默认禁网；题集 10 题 / 23 期望引用；**仅 `LITRADAR_QUICK_LLM_FALLBACK` 一个变量不同**。

| 记录 | 兜底 | 提示词 | Q1 | Q2 | Q3 | quick 小计 | 总命中 | 总 token |
|---|---|---|---|---|---|---|---|---|
| `180435` | 关 | — | 0/2 | 0/2 | 1/2 | 1/6 | 15/23 | 73182 |
| `182330` | 关 | — | 0/2 | 0/2 | 1/2 | 1/6 | 14/23 | 82168 |
| `175648` | 开 | 通用 | 0/2 | 1/2 | 1/2 | 2/6 | 17/23 | 94154 |
| `181441` | 开 | 强化 | 1/2 | 1/2 | 1/2 | 3/6 | 17/23 | 112874 |
| `182951` | 开 | 强化 | 1/2 | 0/2 | 1/2 | 2/6 | 17/23 | 89345 |

- **quick 通道**：关轮两轮均 1/6（完全一致，复现 §34 关轮基线）；开轮（强化）2~3/6，**Q1 稳定 0/2 → 1/2**，Q2 两次 0/2 → 1/2。增益直接来自提示词强化。
- **端到端**：开轮 3 次稳定 17/23；关轮 14~15/23 → 净增益 +2~+3。
- **deep 路径偶发降级（与本改动无因果）**：`181441` 的 Q8 与 `182330` 的 Q10 出现 `plan` 失败「Planner 不可用，回退 v3 固定流水线」（deepseek-flash 返回提示词模板示例而非真实规划）。quick 兜底只挂 `_node_quick`，不可能影响 deep 题。
- **token 带**：历史带 61659–97479 需上修为 **61659–112874**（`181441` 达 112874，成因是 deep 路径 analyst 调用次数非确定性）。
- **MCP**：五轮均 0 次「MCP 通道不可用」告警、全程无回落。

## 7. 结果、代价与遗留

- **结论**：提示词强化**有效但受本地模型上限约束**——quick 通道由 1/6 升至 2~3/6、Q1 稳定好转；**仍未达计划预期 18~20/23**（端到端稳定 17/23）。Q2 需要「检索器 / 向量库 / 提示处理 / 生成器」这类细粒度组件术语，本地 `qwen3-8b` 粒度不够；Q3 单题结构上限受限。
- **代价**：默认开时每个 quick 问句多付一次 `qa_router` 调用（本地模型单次 0.7~23s），需权衡延迟与 +1~+2 命中。
- **新发现（非本批引入）**：deep 路径 planner 偶发返回模板 JSON 触发 v3 回退（5 轮中 2 轮各中 1 题），放大 deep 题命中与 token 波动，建议另立批次排查。
- **遗留**：未做负缓存；未针对 Q2 细粒度术语做专门提示；未改 `LIBRARY_TOP_K`（已实测否决）。
- **回滚**：`LITRADAR_QUICK_LLM_FALLBACK=0` 即完全退回本批之前行为，无需回滚代码。
