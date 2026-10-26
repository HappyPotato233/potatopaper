# Reviewer 多视角需求落地实现计划

> 版本：v1.0　创建：2026-10-19
> 上游 spec：《Reviewer多视角需求落地设计.md》（v1.0）
> 执行方式：SDD 子代理逐 Task 执行；**每个 Task 完成不自动提交**，须汇报并征得许可。

---

## 0. Global Constraints（全 Task 恒定约束）

1. **单一变量**：只做「Reviewer 提需求 → 编排层补跑多视角」这一件事，不顺手改提示词口径、不动 token 预算、不动 `MAX_ITERATION`。
2. **契约冻结**：`app/*`、`web/*` 一行不改；`get_steps` 9 字段集与 `review_notes` 3 键结构不变；8 节点步骤条与全部文案不变。
3. **不改动清单**：`ai/tools.py`、`ai/planner.py`、`ai/analysts.py`、`ai/summarizer.py`、`ai/retriever.py` 均不改。
4. **护栏不回退**：Phase 2 方案 A 的证据态硬性规则 1（不得据「未触发多视角」判 `pass=false`）必须保留。
5. **测试隔离**：沿用 `tests/conftest.py` 顶层 `LITRADAR_QA_DB=":memory:"`，pytest 不得产生 `data/qa_checkpoints.sqlite3`。
6. **不提交**：每个 Task 结束后停下汇报，等许可。
7. **冲突上报**：实现中发现与 spec 不符（尤其 §4.6 偏离说明），先上报裁定，不自行决定。

---

## Task 1：Reviewer 需求通道（`ai/reviewer.py`）

**改动**

1. 新增 `_VIEW_KEYS = ("method", "critic", "compare")` 与 `_clean_views(raw) -> list[str]`（spec §4.1）。
2. `_PROMPT_EVIDENCE_ONLY` 硬性规则由 1 条改 3 条（新增 `need_analysts` 提出渠道；保留规则 1）；输出 JSON 增 `need_analysts` 字段（spec §4.1）。措辞避开「视角完整性」「逻辑一致性」字样，防既有断言破裂。
3. `_PROMPT_FULL` 增一条「④判定缺视角时可写入 need_analysts」；输出 JSON 增 `need_analysts` 字段。
4. `review()` 返回值由 3 键扩为 4 键（新增 `need_analysts`，经 `_clean_views` 校验）。
5. `_salvage()` 增 `need_analysts` 宽松提取（spec §4.1）。

**测试**：`tests/test_reviewer_checklist.py` 新增 R1/R2/R3/R5，并更新既有 `test_review_dto_shape_and_defect_truncation`（R4：断言 4 键 + 无需求时空数组）。

**验证命令**

```powershell
python -m pytest tests/test_reviewer_checklist.py -q
```

**通过判据**：该文件全绿；`test_evidence_only_template_when_no_analysts` 与 `test_full_template_when_analysts_present` 的既有断言**未修改**仍通过。

---

## Task 2：编排层确定性补跑（`ai/orchestrator.py`）

**改动**

1. `ask()` session 模板新增 `"review_need_analysts": []` 与 `"analyst_backfill_done": False`（spec §4.5）。
2. 新增模块常量 `_REVIEW_NOTE_KEYS = ("passed", "defects", "suggestions")`。
3. `_do_review()`：由「整体赋值 `res["data"]`」改为「`review_need_analysts` 收内部键 + `review_notes` 白名单重建」（spec §4.2）；异常分支同步置 `review_need_analysts = []`。步骤条文案逐字不变。
4. 新增 `_maybe_backfill_analysts(session) -> bool`（spec §4.3），复用 `_execute_tool_calls()`。
5. `_node_review()` 在 `_do_review(session)` 之后插入 `_maybe_backfill_analysts(session)`（spec §4.3）。
6. `_apply_tool_result()` 的 `run_analysts` 分支由整体覆盖改为合并（spec §4.4）。

**测试**：`tests/test_orchestrator_paths.py` 夹具加法式扩展（`review_needs` / `analyst_calls` / `analyst_result`），新增 O1–O7。

**验证命令**

```powershell
python -m pytest tests/ -q
Test-Path data/qa_checkpoints.sqlite3
```

**通过判据**：既有 48 项 + 本 Task 新增全绿；`Test-Path` 为 `False`；`git diff --stat` 不含 `app/`、`web/`。

---

## Task 3：端到端实测（真实链路，非桩件）

**做法**

1. 起后端，用实测账号提问一题 **deep 档综述/对比类**问题（如 Q9 原题），轮询 `/steps` 直至 `finished=true`。
2. 从运行记录（`eval/run_eval.py --only <Qn>` 产出的测试记录）核验三件事：
   - reviewer 的 prompt 中出现证据态模板的 `need_analysts` 说明，且其输出含非空 `need_analysts`；
   - 同题 `tool_calls` 中出现一条**非 Planner 规划**的 `run_analysts`（由编排层补跑触发）；
   - 最终 `answer` 含多视角内容，`points` / `citations` 正常，`error=null`。
3. 另跑一题 **normal 档**，确认未被误触发（`run_analysts` 调用 0 次）或触发后行为符合 spec。

**通过判据**：链路可见「Reviewer 提出 → 编排层执行」；无 traceback；`get_steps` 响应中无 `need_analysts` 键。

---

## Task 4：全量 eval 对照波动带

**做法**

```powershell
python eval/run_eval.py
```

与历史波动带对照：命中率 **12–15/23**、token **61659–97479**；统计口径沿用既有的 Mann-Whitney U 与「零运行异常」判据。

**新增观察项（Phase 4 专用，用于判断自适应是否退化成 B1 一刀切）**：统计本批 **10 题（23 个期望引用）**中实际触发补跑的题数
（判据：trace 中出现 `run_analysts` 调用 或 会话末 `analyst_backfill_done=True`；阈值按 10 题集等比换算自原 23 题口径）。

- 触发 **1–4 题** → 按内容自适应，符合 B2 预期（原 3–8/23 ≈ 13%–35%）；
- 触发 **≥ 9 题** → 事实等同「无论如何都跑」，需回到 `need_analysts` 判据收紧后再评估（见设计 §9）；
- 触发 **0 题** → reviewer 不肯提需求，B2 链路未被激活，同样需上报。

触发题数须一并写入 Task 5 的 §29.2 验证记录。

**通过判据**：落带内 或 落带外但有可归因说明；全程无 traceback；触发题数落在上述区间或已上报。落带外且无法归因 → 上报裁定（追加采样 / 清库重跑 / A-B）。

---

## Task 5：文档登记与收尾

**改动**

1. `doc/项目文档/文档变更记录.md` 新增 §29（三段式：29.1 变更点清单 / 29.2 验证记录 / 29.3 要点备忘与遗留）。
2. `doc/变更文档/Reviewer复审清单自适应设计.md` 在 §2.1 目标 2 与 §4.2 处标注**被《Reviewer多视角需求落地设计.md》取代/扩展**（原文保留，加注指向）。
3. 更新《多agent编排方案v4.md》§4.1 流程说明中「补做多视角走 suggestion」的描述（如存在）。

**通过判据**：三处文档与实现一致；`git status` 干净（仅待提交文件）。

---

## 回滚

单变量改动集中在 `ai/reviewer.py` 与 `ai/orchestrator.py` 两个文件；回滚即 `git checkout` 这两个文件（测试与文档同步回退）。
