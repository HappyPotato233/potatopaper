# Reviewer 多视角需求落地设计

> 版本：v1.0　创建：2026-10-19
> 定位：把「Reviewer 要求补做多视角分析」从**无效建议**改为**可执行需求**（spec）。
> 关联文档：《Reviewer复审清单自适应设计.md》（本设计取代其 §2.1 目标 2 的后半句）；《多agent编排方案v4.md》§2.2 / §4.1；《详细设计文档.md》§5.1.4；《文档变更记录.md》§26

---

## 1. 背景与问题

### 1.1 用户诉求（2026-10-19）

> 「我认为如果复审 reviewer 要求使用多视角分析，那就应该调用多视角。」

### 1.2 现状：三处断点使该诉求不成立

| # | 断点 | 位置 |
|---|---|---|
| 1 | evidence-only 模板硬性规定「未触发多视角不得据此判 pass=false，只写入 suggestion」 | `ai/reviewer.py` L53 |
| 2 | suggestion 仅在 `pass=false` 触发 retry 时才被 Planner 消费，且**是否真跑仍由 Planner LLM 自行决定** | `ai/orchestrator.py::_suggestion_text`（L513）→ `_node_plan`（L541-545） |
| 3 | `pass=true` 时循环直接结束（outcome=`passed`），suggestion **不产生任何动作** | `ai/orchestrator.py::_node_review`（L583-584） |

实测佐证：`eval/record/测试记录_20261019_204901.txt` 的 Q9（deep 档）——全 run 唯一一次 `run_analysts` 出现在 Q8，Q9 无任何分析师输出，reviewer 走证据档模板后**直接通过**（L1464 `review -> done | 复审通过`），诉求未兑现。

### 1.3 根因

Reviewer 的「补做多视角」诉求**只有 suggestion 这一条单向文本通道**，而下游没有任何环节负责兑现它：
- Planner 只在 retry 轮读到它，且服从与否不可控；
- `pass=true` 时该通道整条链路都不被执行。

换言之：**提出了需求，但系统中没有「执行需求」的角色。**

---

## 2. 目标与非目标

### 2.1 目标

1. Reviewer 能**结构化地**提出「需要补做多视角分析」的需求（哪个/哪些视角）。
2. 该需求由**编排层确定性执行**（不依赖 Planner LLM 服从）——「Reviewer 要求 → 一定调用多视角」。
3. 执行有**明确上界**（单会话最多补跑 1 次），不引入死循环与 token 失控。
4. 前端响应契约（`get_steps` / `get_result` / `review_notes` 结构）**逐字段不变**。

### 2.2 非目标（明确不做）

- 不改 `app/*`、`web/*`、`app/schemas/qa.py` 一行；
- 不新增/删除 8 节点步骤条，不改任何步骤文案；
- 不改 `loop_counter` / `round_no` / `iteration_count` 语义；
- 不改 `MAX_ITERATION=3`、`REVIEW_MAX_TOKENS=1024`、`_salvage` 的既有兜底口径；
- 不改 quick 档与 v3 回退档（两者不经过 Reviewer，不受影响）；
- 不改综述图（`ask_review`，其自身固定调用 `_analyze`）。

---

## 3. 关键决策

| # | 决策点 | 结论 |
|---|---|---|
| D1 | 需求通道 | `ai/reviewer.py::review()` 返回增加**内部键** `need_analysts: list[str]`（取值 `method` / `critic` / `compare`），不进前端契约 |
| D2 | 执行者 | **编排层**执行（`ai/orchestrator.py`），不交回 Planner |
| D3 | 执行方式 | 经既有工具层 `tools_mod.execute("run_analysts", args, session)` 调用（复用权限校验 + trace + `tool_calls` 记录），不新开 LLM 直调通道 |
| D4 | 触发条件 | `need` 非空 **且** 未补跑过 **且** 证据 ≥ 2 篇 **且** 其中至少一个视角当前为空 |
| D5 | 上界 | 新增 `session["analyst_backfill_done"]` 布尔位，单会话仅一次 |
| D6 | 是否重跑 Reviewer | **不重跑**（见 §4.6 偏离说明） |
| D7 | 验证方式 | pytest 纯单测（reviewer 通道 + 编排补跑）+ 端到端实测 + 全量 eval 对照波动带 |

---

## 4. 设计

### 4.1 需求通道：Reviewer 输出新增内部键

**两套模板均增加**输出说明与输出 JSON 字段（`need_analysts` 为空数组即无需求）：

**`_PROMPT_EVIDENCE_ONLY`（无分析师）** 的硬性规则改写为三条：

```
硬性规则：
1. 本轮未触发多视角分析属于 Planner 的调度选择，不得据此判 pass=false；
2. 若你认为本问题确需多视角分析（如综述、流派对比、正反评价类），把它写入 need_analysts 提出补做要求，
   不要写入 defect_list；
3. 仅当证据少于 2 篇时不得提出该要求（分析师无从评起，补做无意义）。

need_analysts 取值：["method"（方法抽取）| "critic"（批判审阅）| "compare"（横向对比）]，
不需要补做时输出空数组。

输出 JSON：{"pass": ..., "defect_list": [...], "suggestion": "...", "need_analysts": [...]}
```

保留规则 1（即原 L53 语义）——**Phase 2 方案 A 防「结构性假缺陷导致首轮判否」的护栏不回退**；新增的是「有需求走 `need_analysts`，不污染 `defect_list`」。

**`_PROMPT_FULL`（有分析师）** 增加一条：④视角完整性判定缺某视角时，可写入 `need_analysts` 提出补做（`defect_list` 口径不变）。

**归一化与校验**（`ai/reviewer.py`）：

```python
_VIEW_KEYS = ("method", "critic", "compare")

def _clean_views(raw) -> list[str]:
    """need_analysts 合法性校验：只认三个视角 key、去重保序（不可信 LLM 输出）"""
    out = []
    for k in raw or []:
        k = str(k).strip()
        if k in _VIEW_KEYS and k not in out:
            out.append(k)
    return out
```

`review()` 返回值由 3 键扩为 4 键：

```python
return {"passed": struct["pass"], "defects": defects,
        "suggestions": [suggestion] if suggestion else [],
        "need_analysts": _clean_views(struct.get("need_analysts"))}
```

`_salvage()`（JSON 被 max_tokens 截断时的宽松提取）同步支持 `need_analysts`：

```python
views = re.search(r'"need_analysts"\s*:\s*\[(.*?)(?:\]|$)', raw, flags=re.S)
if views:
    struct["need_analysts"] = re.findall(r'"([^"]*)"', views.group(1))
```

### 4.2 契约隔离：`_do_review` 白名单重建

`review_notes` 是前端冻结契约（`app/schemas/qa.py::ReviewNotes` / `web/src/api/client.ts::QaReviewNotes`，字段恒为 `passed` / `defects` / `suggestions`）。故内部键必须**在写入会话正文前剥离**：

```python
_REVIEW_NOTE_KEYS = ("passed", "defects", "suggestions")   # 前端冻结契约字段（对齐详细设计 §3.5）

def _do_review(session: dict) -> None:
    res = tools_mod.execute("run_reviewer", {}, session)
    if res.get("ok"):
        data = res.get("data") or {}
        session["review_need_analysts"] = list(data.get("need_analysts") or [])  # 内部通道，不入响应
        session["review_notes"] = {
            "passed": bool(data.get("passed")),
            "defects": [str(d) for d in (data.get("defects") or [])],
            "suggestions": [str(s) for s in (data.get("suggestions") or [])],
        }
        ...  # 步骤条文案与现状逐字一致（通过 / 未通过（N 项缺陷））
    else:
        session["review_notes"] = None
        session["review_need_analysts"] = []
        session["review_skipped"] = True
        ...
```

### 4.3 确定性补跑：`_maybe_backfill_analysts()`

```python
def _maybe_backfill_analysts(session: dict) -> bool:
    """复审要求补做多视角 → 编排层确定性补跑一次（不经 Planner，单会话至多一次）

    触发需同时满足：有需求 / 未补跑过 / 证据 ≥ 2 篇 / 需求视角中至少一个当前为空。
    """
    need = [k for k in (session.get("review_need_analysts") or []) if k]
    if not need or session.get("analyst_backfill_done"):
        return False
    if len(session.get("evidence") or []) < 2:
        session["planner_notes"].append("复审要求补做多视角分析，但证据不足 2 篇，未补做")
        return False
    have = session.get("analyst_outputs") or {}
    missing = [k for k in need if not str(have.get(k) or "").strip()]
    if not missing:
        return False
    session["analyst_backfill_done"] = True          # 置位先于执行，重复触发免疫
    _execute_tool_calls(session, [{"tool": "run_analysts",
                                   "args": {"enable_list": missing}}])
    session["planner_notes"].append(
        f"复审要求补做多视角分析（{'、'.join(missing)}），编排层已确定性补跑")
    return True
```

要点：

- 复用 `_execute_tool_calls()` → 复用 `tools_mod.execute` 的**统一权限校验**、trace 记录（`rec.tool`）、`tool_calls` 追加；前端 `QaToolCall` 字段不变。
- 补跑发生在 `_node_review` **内部**（`_do_review` 之后、outcome 判定之前），`round_no` / `loop_counter` 不变。

`_node_review` 改动（插入一行）：

```python
    _do_review(session)
    _maybe_backfill_analysts(session)   # 复审要求多视角 → 确定性补跑（至多一次）
    note = session.get("review_notes")
```

### 4.4 分析师输出合并语义

`_apply_tool_result()` 现值对 `run_analysts` 为**整体覆盖**：

```python
session["analyst_outputs"] = {k: v for k, v in data.items() if v}
```

补跑场景下会破坏已有视角（如本轮已有 `method`，补跑只带 `critic`/`compare` → `method` 被覆盖丢失，二次复审又会因「方法抽取缺失」再挑）。故改为**合并**：

```python
merged = dict(session.get("analyst_outputs") or {})
merged.update({k: v for k, v in data.items() if v})
session["analyst_outputs"] = merged
```

行为差异**仅在**「同会话多次 `run_analysts` 且视角集合不同」时可见；现状 Planner 路径（单次全量或单次部分调用）下与覆盖语义等价（见 §5 第 7 条）。

### 4.5 会话初始态

`ask()` 的 session 模板新增两个内部键（沿用 `.get` 兜底，历史会话缺失不报错）：

```python
"review_need_analysts": [],   # 复审提出的多视角补做需求（内部，不入 steps 响应）
"analyst_backfill_done": False,  # 补跑一次性标记（防重复）
```

### 4.6 偏离说明：不重跑 Reviewer

AskUserQuestion 选项描述中写的是「补跑 → **再复审一次**」。实现计划采用**不重跑**，理由：

1. 重跑需额外 1 次 reviewer LLM 调用；且 normal 档（`max_rounds=1`）下，若二次复审判否，会立刻命中 `round_no >= max_rounds` → 置 `iteration_limit` → **用户可见答案被前置 `⚠【分析已达到轮次上限…】`**，属可见回归；
2. 补跑产生的分析师输出会在 `finalize` 汇总时被 Summarizer 采用（`_step_summarize` 传 `session["analyst_outputs"]`），「要求即执行」的目的已达成；
3. `pass=false` 的情形本就进入下一轮，下一轮 Reviewer 自然复审，无需在节点内重复。

若你要求保留重跑，实现计划 Task 2 追加一步：补跑后再次 `_do_review(session)`，并新增一条用例锁定「二次复审判否在 normal 档前缀告警」。

### 4.7 不改动项

- `ai/tools.py`：`_run_reviewer` 已原样透传 DTO，不改；
- `ai/planner.py`：不改（补跑不依赖 Planner 服从）；
- `get_steps()` / `get_result()` / `app/schemas/qa.py` / `web/*`：不改；
- 8 节点步骤条与全部文案：不改。

---

## 5. 行为等价性清单（无需求时逐字段等价）

| # | 场景 | 与基线的差异 |
|---|---|---|
| 1 | Reviewer 不输出 `need_analysts`（含输出为空数组） | 无（`_maybe_backfill_analysts` 首判即返回） |
| 2 | 需求视角全部已有非空输出 | 无（missing 为空即返回） |
| 3 | 证据 < 2 篇 | 无（仅多一条 `planner_notes` 文案） |
| 4 | `analyst_backfill_done` 已置位 | 无 |
| 5 | Reviewer 返回 `None`（LLM 不可用/输出非法） | 无（走原 skipped 分支） |
| 6 | quick 档 / v3 回退档 / 综述图 | 无（不经 Reviewer） |
| 7 | `_apply_tool_result` 覆盖 → 合并 | 仅「同会话多次 `run_analysts` 且视角集合不同」时可见；现状不可达 |
| 8 | `get_steps` 响应 | 无（内部键被白名单剥离） |

---

## 6. 测试设计

### 6.1 Reviewer 通道（`tests/test_reviewer_checklist.py`）

| # | 用例 | 断言 |
|---|---|---|
| R1 | 证据态模板含需求说明 | prompt 含 `need_analysts` 与「不得据此判 pass=false」（既有断言不回退）；仍不含「逻辑一致性」「视角完整性」字样 |
| R2 | 全量模板含需求说明 | prompt 含 `need_analysts`；仍不含「不得据此判 pass=false」 |
| R3 | `_clean_views` 边界 | 非法 key（如 `"xxx"`）丢弃；重复去重保序；`None`/`{}` → `[]` |
| R4 | DTO 形状（更新既有用例） | `set(note) == {"passed","defects","suggestions","need_analysts"}`；无需求时 `need_analysts == []` |
| R5 | `_salvage` 截断兜底 | JSON 尾部断在 `need_analysts` 中途 → 仍能提取已闭合的视角 key |

### 6.2 编排补跑（`tests/test_orchestrator_paths.py`）

夹具扩展（加法式，不影响既有用例）：`board` 增 `review_needs`（逐次弹出）、`analyst_calls`（记录 `enable_list`）、`analyst_result`。

| # | 用例 | 断言 |
|---|---|---|
| O1 | 提出需求且无分析师 → 补跑 | `run_analysts` 被调 1 次且 `enable_list` 等于 missing；`tool_calls` 增 1 条；`analyst_backfill_done is True`；`planner_notes` 含补跑文案 |
| O2 | 需求视角已齐备 | `run_analysts` 调用 0 次 |
| O3 | 证据 1 篇 | 0 次调用；`planner_notes` 含「证据不足 2 篇，未补做」 |
| O4 | 补跑后仍提需求（第 2 轮回审） | 累计调用仍为 1 次（一次性标记生效） |
| O5 | 已有 `method` + 需求 `["critic"]` | 补跑后 `analyst_outputs` 同时含 `method` 与 `critic`（合并语义） |
| O6 | 复审异常（`ok=False`） | 0 次调用；`review_skipped is True` |
| O7 | 契约冻结 | 含需求请求的会话，`set(get_steps(...))` 仍为 9 字段；`review_notes` 恒为 3 键、无 `need_analysts` |

### 6.3 既有护栏（不得回退）

- `test_evidence_only_path_can_still_fail_on_insufficient_evidence`（证据不足仍可判否）；
- `test_get_steps_maps_from_get_state`（9 字段契约）；
- Phase 3 的 6 条持久化用例（saver / 索引重建 / 跨实例可读）。

---

## 7. 验收标准

1. **pytest**：`pytest tests/ -q` 全绿（现 48 项；R4 为既有用例断言更新，其余为新增）。
2. **端到端实测**：跑一题 deep 综述/对比类问题，在运行记录中可见链路 `reviewer 输出 need_analysts → tool: run_analysts（由编排层触发）→ 最终 answer 含多视角内容`，且 `finished=true`、`error=null`。
3. **契约零漂移**：`get_steps` 字段集与 `review_notes` 结构与基线逐字段一致（O7 锁定）；前端/接口层代码零改动（`git diff` 不含 `app/`、`web/`）。
4. **全量 eval**（实测题集为 **10 题 / 23 个期望引用**，原写「23 题」有误）：命中率落在历史波动带 **12–15/23**、token 落在 **61659–97479**，或落带外时给出归因说明；全程**零运行异常**（无 traceback）。
5. **文档同步**：《文档变更记录.md》新增 §29；《Reviewer复审清单自适应设计.md》标注被本设计取代/扩展之处。

---

## 8. 风险与对策

| 风险 | 对策 |
|---|---|
| Reviewer 过度提需求 → 无谓 token 消耗 | 单会话 1 次上限 + 证据 ≥ 2 篇门槛 + `missing` 为空即不动作；全量 eval 观测 token 是否越带 |
| 补跑视角与 Planner 下一轮重复调度（浪费一次调用） | 一次性标记只约束编排层补跑；Planner 重复调用仅多花一次 LLM，不影响正确性 |
| 合并语义改变 `analyst_outputs` | 差异仅在多次部分视角调用时可见（现状不可达）；O5 锁定 |
| 内部键污染前端契约 | `_do_review` 白名单重建；O7 锁定 |
| Phase 2 方案 A 的护栏被误伤（重演首轮判否） | 证据态模板**保留**「不得据此判 pass=false」；§6.3 既有护栏用例常驻 |
| 证据态模板新增「视角」字样触发既有断言失败 | R1 明确断言仍不含「视角完整性」「逻辑一致性」；措辞改用「多视角分析」 |
| 不重跑 Reviewer 导致判据滞后 | 记录为 §4.6 偏离；补跑输出在 finalize 被 Summarizer 采用；如需重跑按 §4.6 末段追加 |
