# Reviewer 复审清单按输入自适应设计

> ⚠️ **已被取代（2026-10-19）**：本文档「按输入自适应切换双模板」的结论，已由《Reviewer多视角需求落地设计.md》承接并**扩展**——在双模板之外新增 **`need_analysts` 需求通道** 与 **编排层确定性补跑**（不经 Planner、单会话至多一次）。本文档仅作历史依据保留，**实现以新设计为准**（落地记录见《文档变更记录.md》§29）。
>
> 版本：v1.0　创建：2026-10-19
> 定位：复审质检环节修复设计（spec）。只改 Reviewer 的检查清单选取逻辑，不改编排流程、不改前端、不改接口契约。
> 关联文档：《多agent编排方案v4.md》§2.2（Reviewer 职责）§3（输入）§4.1（流程图）；《详细设计文档.md》§5.1.4；《文档变更记录.md》§26

---

## 1. 背景与问题

### 1.1 现象

2026-10-19 全量评测（`eval/record/测试记录_20261019_103353.txt`）：**12 次 reviewer 调用，0 次 `pass=true`，10 次判否**，缺陷列表首条几乎全是「分析师输出缺失 / 无结论可审 / 未输出任何分析内容，属于实质空缺」一类表述（见记录 L395 / L711 / L872 / L1059 / L1418 / L1549 / L1909 / L2179 / L2276）。

### 1.2 数据流事实（先纠正一个常见误判）

Reviewer 的实际输入**不是** Planner 的输出。调用链：

- `ai/orchestrator.py::_run_planner_loop()` 内调 `_do_review()`，后者 `tools_mod.execute("run_reviewer", {}, session)`；
- `ai/tools.py::_run_reviewer()` 取值 `user_query` / `context_evidence` / `analyst_outputs`。

Planner 的 `plan`（含 `thought` / `subtasks` / `tool_calls`）**从不进入 reviewer**。记录 L1392–L1418 可证：reviewer 的 prompt 由「用户问题 + 文献证据[1..N] + 分析师输出」构成。

### 1.3 根因

`analyst_outputs` 被 `_apply_tool_result()` 唯一地在 `run_analysts` 执行时才填充。若 Planner 本轮未规划 `run_analysts`（首轮通常「先检索、后分析」），reviewer 收到的 `analyst_outputs` 即为空。此时：

- 检查清单 ③逻辑一致性、④视角完整性**必须先有分析师结论才可评**，输入为空 → 该项不可评；
- 模板栏位写作「分析师输出（**可能部分缺失**）」，`_build_analyst_block()` 在空输入时回填「（本问题未触发多视角分析）」，两处措辞都在诱导模型把「**尚未执行**」判成「**实质空缺**」的致命缺陷。

于是 reviewer 实际审的是 **Planner 的调度决策**（有没有调分析师），而不是待交付内容的质量。

### 1.4 后果

| 档位 | 后果 |
|---|---|
| deep（`max_rounds=3`） | 首轮必判否 → 白烧一轮，且把「补做多视角分析」混在 `defect_list` 中，掩盖了真正的证据问题 |
| normal（`max_rounds=1`） | 首轮判否即满足 `round_no >= max_rounds` → `iteration_limit` 直接收尾，无补救机会（Q4/Q6/Q7 即此形态） |

---

## 2. 目标与非目标

### 2.1 目标

1. Reviewer **只评「手上有材料可评的项」**：无分析师输出时不评 ③④。
2. 「本轮未触发多视角分析」**不再是判否依据**，降级为 `suggestion` 提示；Planner 仍可经「上一轮复审建议」通道在下一轮自行决定是否补做（**不把分析师硬约束化**，保持既有偏好）。
3. 「证据不足」仍是合法判否理由，保证无证据时的诚实降级不回退。

### 2.2 非目标（明确不做）

- 不改 `REVIEW_MAX_TOKENS` 与 `_salvage()` 截断补救逻辑；
- 不改 Planner 提示词，不把「综述类建议全启用分析师」改为硬约束；
- 不引入「分析师必须调用」的编排约束；
- **不挪动 review 位置**（不把 review 放到 summarize 之后改审草稿答案）——那与 v4 §4.1 流程图冲突，另案处理；
- 不改 `review_notes` 返回结构与前端契约。

---

## 3. 已确认的关键决策

| # | 决策 | 结论 |
|---|---|---|
| D1 | 修复位置 | 只改 `ai/reviewer.py`；`ai/tools.py`、`ai/orchestrator.py` 预期不动（验证后如需再定） |
| D2 | 判定方法 | 新增 `_analysts_present(analyst_outputs) -> bool`：任一视角有非空文本即为 True |
| D3 | 清单选取 | 双模板：有分析师走 `_PROMPT_FULL`（5 项全查），无分析师走 `_PROMPT_EVIDENCE_ONLY`（只查 ①②⑤） |
| D4 | 「未触发多视角分析」定位 | 从 `defect_list` 移到 `suggestion`，并在模板中显式禁止据此判否 |
| D5 | 验证方式 | pytest 纯单测（monkeypatch `ai.reviewer.chat` 截获 prompt / 回灌 JSON），先红后绿；再跑受影响题目做端到端对照 |

---

## 4. 设计

### 4.1 判定：`_analysts_present()`

```python
def _analysts_present(analyst_outputs: dict | None) -> bool:
    """本轮是否产出过分析师结论（任一视角有非空文本即为真）"""
    return any(str(v).strip() for v in (analyst_outputs or {}).values())
```

判据取「值非空」而非「键存在」，与 `_build_analyst_block()` 现有的 `if analyst_outputs.get(k)` 过滤口径一致——只挂了键但文本为空的视角不算「有内容可评」。

### 4.2 双模板

**`_PROMPT_FULL`（有分析师）**：即现行 5 项清单模板，内容不变（幻觉校验 / 证据充足度 / 逻辑一致性 / 视角完整性 / 置信标记）。

**`_PROMPT_EVIDENCE_ONLY`（无分析师）**：只保留可评项，并新增一条硬性规则。

```
你是独立复审质检 Agent，只做校验、不生成也不修改任何业务内容。

本轮未执行多视角分析（Planner 本轮未调用分析师），因此视角完整性、逻辑一致性两项无输入可评，不得列入检查范围。

本轮检查清单：
1. 幻觉校验：结论中引用的文献编号必须真实存在于下方证据；
2. 证据充足度：现有文献是否足以回答用户问题；
3. 置信标记：不确定结论是否标注【存疑，建议人工核验】。

用户问题：{question}

文献证据（编号为引用标识）：
{evidence}

硬性规则：未触发多视角分析属于 Planner 的调度选择，不得据此判 pass=false；如认为需要补做多视角分析，只写入 suggestion，不要写入 defect_list。

输出 JSON（defect_list 最多 3 条、每条不超过 20 字；suggestion 一句话不超过 40 字，务必输出完整闭合的 JSON）：
{"pass": true|false, "defect_list": ["缺陷描述，无缺陷则空数组"], "suggestion": "给 Planner 的改进指令，一句话"}
```

要点：

- **不再渲染**「分析师输出（可能部分缺失）」栏——该栏在空输入时是误导来源；
- 判否只能来自 ①②③（证据类），`defect_list` 只承载证据/幻觉/置信标记类缺陷；
- 「补做多视角分析」走 `suggestion` 单通道，`review_notes.suggestions` 结构不变，Planner 侧 `_suggestion_text()` 无需改动。

### 4.3 调用点

```python
def review(question, evidence, analyst_outputs=None):
    if _analysts_present(analyst_outputs):
        prompt = _PROMPT_FULL.format(question=..., evidence=..., analysts=...)
    else:
        prompt = _PROMPT_EVIDENCE_ONLY.format(question=..., evidence=...)
    ...
```

其余（`chat("reviewer", ...)`、`parse_json_loose` → `_salvage` 兜底、`defects[:3]`、返回 DTO 归一化）**保持不变**。

### 4.4 不改动项

- `_build_analyst_block()` 保留（`_PROMPT_FULL` 仍用），仅新增 `_analysts_present()`；
- `ai/tools.py::_run_reviewer` 已正确透传 `analyst_outputs`，不改；
- `ai/orchestrator.py::_do_review` 与闭环判定（`note.get("passed")`）不改。

---

## 5. 测试设计

新增 `tests/test_reviewer_checklist.py`（纯单测，不依赖数据库；`monkeypatch.setattr(reviewer, "chat", fake)` 截获 prompt 并回灌 JSON）：

| # | 用例 | 断言 |
|---|---|---|
| T1 | 无分析师时走证据态模板 | 截获的 prompt 含「不得据此判 pass=false」；**不含**「视角完整性」「逻辑一致性」 |
| T2 | 有分析师时走全量模板 | prompt 含「视角完整性」「逻辑一致性」；含分析师输出文本 |
| T3 | 无分析师且仅证据不足 | 回灌 `pass=false` 且 defect 为证据类 → 正常返回 `passed=False`（诚实降级保持） |
| T4 | `_analysts_present` 边界 | `{}` / `{"method": ""}` → False；`{"method": "x"}` → True |
| T5 | 结构不变 | 返回仍为 `{"passed","defects","suggestions"}`，`defects` 截断至 3 条 |

---

## 6. 验收标准

1. `pytest tests/ -q`：既有 20 项 + 新增 5 项全绿。
2. 端到端对照：`python eval/run_eval.py --only Q4,Q6,Q7,Q9`，与 `测试记录_20261019_103353.txt` 对比——
   - reviewer 判否原因不再以「分析缺失」为唯一依据；
   - normal 档不再因结构性假缺陷被 `iteration_limit` 锁死（判否需由证据类缺陷支撑）。
3. 无回归：quick 档行为不变（不经过 reviewer）；`_salvage` 截断兜底与 `REVIEW_MAX_TOKENS` 行为不变。

---

## 7. 风险与对策

| 风险 | 对策 |
|---|---|
| 放宽后 deep 问题可能不再触发分析师，答质下降 | 「补做多视角分析」仍经 suggestion 通道逐轮提示 Planner；端到端对照时重点看 Q9/Q10 的分析师调用次数是否归零 |
| 双模板造成维护分叉，两处清单描述漂移 | 两份模板共用同一段输入构造与输出 JSON 约束；测试 T1/T2 锁定差异边界 |
| 模型仍可能无视硬性规则、继续以「未分析」判否 | 规则句写在模板末段（近因位置）；T3 之外在端到端对照中人工核对判否理由文本 |
