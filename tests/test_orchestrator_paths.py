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
        "reviewer_ok": True,        # run_reviewer 正常返回？False → ok=False（复审异常分支）
        "review_needs": [],         # run_reviewer 依次弹出的 need_analysts 列表（空则无需求）
        "analyst_calls": [],        # run_analysts 每次被调用的 enable_list（补跑断言用）
        "analyst_result": {},       # run_analysts 桩返回的视角-文本字典（空则空 dict）
        "synth_calls": 0,           # summarize 被调用次数（汇总前移后按轮计数）
        "quick_calls": 0,           # rewrite_for_quick 被调用次数（quick 英文关键词兜底）
        "quick_keywords": None,     # 桩返回的英文关键词（None → 改写失败，退回正则）
    }

    def fake_rewrite(query, mode="phrase", enabled=True):
        return {"query": query, "rewritten": False, "keywords": ["attention"],
                "filters": {"year_from": "", "year_to": "", "sources": [], "cats": []}}

    def fake_quick(query):
        """quick 通道 LLM 英文关键词兜底桩：board["quick_keywords"] 非空则视为改写成功"""
        board["quick_calls"] += 1
        kws = board["quick_keywords"]
        return {"keywords": list(kws), "rewritten": True} if kws else None

    def fake_route(question, keywords=None):
        return {"routes": board["routes"], "keywords": keywords or [],
                "confidence": 0.9, "reason": "测试桩", "complexity": board["complexity"]}

    def fake_plan(question, round_no, evidence_summary, review_suggestion=""):
        return board["plans"].pop(0) if board["plans"] else None

    def fake_execute(name, args, ctx):
        if name == "run_reviewer":
            board["review_calls"] += 1
            if not board["reviewer_ok"]:                 # 复审异常：ok=False 分支
                return {"ok": False, "brief": "复审异常", "error": "", "data": None}
            passed = board["reviews"].pop(0) if board["reviews"] else False
            need = board["review_needs"].pop(0) if board["review_needs"] else []
            return {"ok": True, "brief": "复审", "error": "",
                    "data": {"passed": passed, "defects": [], "suggestions": [],
                             "need_analysts": need}}
        if name == "run_analysts":
            board["analyst_calls"].append(list(args.get("enable_list") or []))
            return {"ok": True, "brief": "分析师", "error": "", "data": board["analyst_result"]}
        if name in ("library_search", "web_search", "memory_search"):
            return {"ok": True, "brief": "命中", "error": "", "data": board["evidence"]}
        return {"ok": True, "brief": "其它工具", "error": "", "data": None}

    def fake_synthesize(question, evidence, analyst_outputs=None):
        board["synth_calls"] += 1
        return {"answer": "回答正文", "points": ["要点1"], "citations": []}

    monkeypatch.setattr(orchestrator.rewrite_mod, "rewrite_query", fake_rewrite)
    monkeypatch.setattr(orchestrator.rewrite_mod, "rewrite_for_quick", fake_quick)
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
    assert stub["synth_calls"] == 1
    assert s["review_notes"] is None
    assert s["loop_counter"] == 0 and s["iteration_count"] == 0
    assert s["planner_notes"] == []
    assert s["planner_fallback"] is False
    assert s["finished"] is True and s["error"] is None
    assert s["result"]["answer"] == "回答正文"            # quick 两标志均未置位 → 无 ⚠ 标注


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
    # ask() 的会话不预置这两个键，未触发即键不存在（故用 .get 读，断言其非 True）
    assert s.get("review_skipped") is not True and s.get("iteration_limit") is not True
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
    assert stub["synth_calls"] == 1                       # 单轮：summarize 节点出草稿 1 次
    assert "分析已达到轮次上限" in s["result"]["answer"]   # 迭代告警前缀
    assert _step(s, "summarize")["status"] == "done"


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
    assert stub["synth_calls"] == 3             # 每轮 summarize 各出一次草稿
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
    assert stub["synth_calls"] == 2             # 2 轮各出一次草稿
    assert s.get("iteration_limit") is not True
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
    assert stub["synth_calls"] == 1             # 汇总前移：零证据轮也先出草稿再短路
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
    # fallback 置 review_skipped → ⚠ 标注逐字一致（与改前同）
    assert s["result"]["answer"] == "> ⚠ 【本次回答未经完整质检，仅供参考】\n\n回答正文"


# ---------- ⑧ 首轮 Planner 不可用（并入流水线图：等价断言，spec §5 #3） ----------

def test_first_round_planner_unavailable(stub):
    stub["plans"] = [None]                      # 首轮 plan 返回 None
    s = _make_session()
    orchestrator._run_pipeline(s)

    assert s["error"] is None and s["finished"] is True
    assert s["planner_fallback"] is True
    assert "Planner 不可用（输出解析失败 / 无 function-call 模型），回退 v3 固定流水线" in s["planner_notes"]
    assert _step(s, "plan")["status"] == "skipped"
    assert _step(s, "plan")["detail"] == "回退 v3 固定流水线（未启用 / 不可用 Planner）"
    assert stub["review_calls"] == 0            # 复审从未执行
    assert stub["synth_calls"] == 1             # fallback 内部汇总（原 v3 流水线）
    assert s["iteration_count"] == 0            # 未走 finalize


# ---------- ⑨ normal · 复审异常（_do_review → ok=False 分支，spec §4.4 #7） ----------

def test_review_abnormal_skips_qa(stub):
    stub["plans"] = [_plan()]
    stub["reviewer_ok"] = False                 # 桩件控制板：run_reviewer 返回 ok=False
    s = _make_session()                         # normal 档（max_rounds=1）
    orchestrator._run_pipeline(s)

    assert s["review_notes"] is None
    assert s["review_skipped"] is True
    assert _step(s, "review")["status"] == "failed"
    assert _step(s, "review")["detail"] == "复审异常，跳过质检（未经过完整复审质检）"
    assert s["iteration_limit"] is True
    assert "达到迭代上限（1 轮），强制结束" in s["planner_notes"]
    assert s["iteration_count"] == 1
    assert stub["review_calls"] == 1


# ---------- ⑩ deep · 重试轮规划失败（spec §4.4 #10；改后 plan_degraded → summarize → review） ----------

def test_deep_retry_round_plan_failure(stub):
    stub["complexity"] = "deep"                 # router 判定 deep（max_rounds=3）
    stub["plans"] = [_plan("第1轮决策"), None]  # 第 2 轮 plan 返回 None
    stub["reviews"] = [False]                   # 第 1 轮复审未过 → 触发重试
    s = _make_session(complexity="deep")
    orchestrator._run_pipeline(s)

    assert s["error"] is None and s["finished"] is True
    assert "第 2 轮规划失败，基于现有证据产出" in s["planner_notes"]
    # 改后 plan_degraded 不再直连 finalize（finalize 已不汇总），改经 summarize 出草稿后进 review
    assert stub["synth_calls"] >= 2
    assert s["result"]["answer"].endswith("回答正文")
    # 降级轮仍进 review，轮次由 max_rounds 兜底（3 轮烧满）
    assert s["loop_counter"] == 3 and s["iteration_count"] == 3
    assert s["iteration_limit"] is True
    assert stub["review_calls"] == 3


# ---------- ⑪ 复审多视角需求补跑（Phase 4：Reviewer 提需求 → 编排层确定性补跑） ----------

def test_backfill_runs_analysts_once(stub):
    """O1：提需求且无分析师 → 编排层确定性补跑 1 次，enable_list 等于 missing"""
    stub["plans"] = [_plan()]
    stub["reviews"] = [True]
    stub["review_needs"] = [["method", "critic"]]
    stub["analyst_result"] = {"method": "方法文本", "critic": "批判文本"}
    s = _make_session()
    orchestrator._run_pipeline(s)

    assert len(stub["analyst_calls"]) == 1
    assert stub["analyst_calls"][0] == ["method", "critic"]
    assert [t["tool"] for t in s["tool_calls"]] == ["library_search", "run_analysts"]
    assert s["analyst_backfill_done"] is True
    assert "复审要求补做多视角分析（method、critic），编排层已确定性补跑" in s["planner_notes"]
    assert s["error"] is None


def test_backfill_skips_when_views_present(stub):
    """O2：需求视角已齐备（均为非空文本）→ 不补跑"""
    stub["plans"] = [_plan()]
    stub["reviews"] = [True]
    stub["review_needs"] = [["method", "critic", "compare"]]
    s = _make_session(analyst_outputs={"method": "m", "critic": "c", "compare": "x"})
    orchestrator._run_pipeline(s)

    assert stub["analyst_calls"] == []
    assert not s.get("analyst_backfill_done")


def test_backfill_skips_when_evidence_insufficient(stub):
    """O3：证据仅 1 篇 → 不补跑，仅留一条「证据不足 2 篇，未补做」提示"""
    stub["plans"] = [_plan()]
    stub["reviews"] = [True]
    stub["review_needs"] = [["critic"]]
    stub["evidence"] = [_ev(1)]
    s = _make_session()
    orchestrator._run_pipeline(s)

    assert stub["analyst_calls"] == []
    assert "复审要求补做多视角分析，但证据不足 2 篇，未补做" in s["planner_notes"]
    assert not s.get("analyst_backfill_done")


def test_backfill_runs_at_most_once(stub):
    """O4：补跑后第 2 轮回审仍提需求 → 一次性标记生效，累计仍仅 1 次"""
    stub["complexity"] = "deep"
    stub["plans"] = [_plan("第1轮决策"), _plan("第2轮决策")]
    stub["reviews"] = [False, True]
    stub["review_needs"] = [["critic"], ["critic"]]
    stub["analyst_result"] = {"critic": "批判文本"}
    s = _make_session(complexity="deep")
    orchestrator._run_pipeline(s)

    assert len(stub["analyst_calls"]) == 1
    assert s["analyst_backfill_done"] is True
    assert stub["review_calls"] == 2


def test_backfill_merges_with_existing_outputs(stub):
    """O5：已有 method + 需求 critic → 补跑后合并语义（旧视角不被覆盖）"""
    stub["plans"] = [_plan()]
    stub["reviews"] = [True]
    stub["review_needs"] = [["critic"]]
    stub["analyst_result"] = {"critic": "批判文本"}
    s = _make_session(analyst_outputs={"method": "方法旧文"})
    orchestrator._run_pipeline(s)

    assert stub["analyst_calls"] == [["critic"]]
    assert s["analyst_outputs"] == {"method": "方法旧文", "critic": "批判文本"}


def test_backfill_skipped_on_review_error(stub):
    """O6：复审异常（ok=False）→ 无需求、0 次补跑、review_skipped"""
    stub["plans"] = [_plan()]
    stub["reviewer_ok"] = False
    s = _make_session()
    orchestrator._run_pipeline(s)

    assert stub["analyst_calls"] == []
    assert s["review_skipped"] is True
    assert s["review_need_analysts"] == []


def test_backfill_keeps_steps_and_review_notes_contract(stub):
    """O7：契约冻结——含需求的会话 get_steps 仍 9 字段，review_notes 恒 3 键、无 need_analysts"""
    stub["plans"] = [_plan()]
    stub["reviews"] = [True]
    stub["review_needs"] = [["critic"]]
    stub["analyst_result"] = {"critic": "批判文本"}
    s = _make_session(session_id="backfill-steps")
    orchestrator._sessions["backfill-steps"] = {"user_id": 1, "created_at": s["created_at"],
                                                "mode": "phrase"}
    orchestrator._run_pipeline(s)

    out = orchestrator.get_steps("backfill-steps")
    assert set(out) == {"session_id", "query", "mode", "complexity", "steps",
                        "finished", "loop_counter", "tool_calls", "review_notes"}
    assert set(out["review_notes"]) == {"passed", "defects", "suggestions"}
    assert "need_analysts" not in out["review_notes"]
    assert out["finished"] is True


# ---------- 新图 API（Phase 2 存储迁移；改造前应为红灯） ----------

def test_pipeline_graph_topology_has_eight_nodes():
    """统一流水线图在模块级编译一次，含 8 个业务节点（汇总前移新增 summarize）"""
    g = orchestrator._build_pipeline_graph().get_graph()
    assert {"rewrite", "quick", "plan", "execute_tools", "summarize",
            "review", "finalize", "fallback"} <= set(g.nodes)
    edges = {(e.source, e.target) for e in g.edges}
    assert ("execute_tools", "summarize") in edges
    assert ("summarize", "review") in edges
    assert ("execute_tools", "review") not in edges      # 旧直连已移除
    assert ("finalize", "__end__") in edges


# ---------- 汇总前移（§33：summarize 先于 review，审的是待交付草稿） ----------

def test_finalize_no_longer_summarizes(stub):
    """finalize 只做落迭代次数 + 补齐检索节点 + 标注；单轮路径 summarize 仅被 summarize 节点调用 1 次"""
    stub["plans"] = [_plan()]
    stub["reviews"] = [True]
    s = _make_session()
    orchestrator._run_pipeline(s)

    assert stub["synth_calls"] == 1
    assert _step(s, "summarize")["status"] == "done"
    assert s["iteration_count"] == 1


def test_finalize_alerts_without_result_is_safe(stub):
    """finalize 在无草稿（result 为 None）时不抛错、不产标注（防御兜底）"""
    s = _make_session(session_id="no-draft")
    s["review_skipped"] = True
    out = orchestrator._node_finalize({"session": s, "round_no": 2, "max_rounds": 3,
                                       "plan": None, "note": None, "outcome": "no_evidence"})
    assert out["session"]["result"] is None
    assert out["session"]["iteration_count"] == 2
    assert stub["synth_calls"] == 0


def test_deep_path_summarizes_every_round(stub):
    """deep 烧满 3 轮：每轮 summarize 各出一次草稿（3 次），非仅末轮一次"""
    stub["complexity"] = "deep"
    stub["plans"] = [_plan("第1轮决策"), _plan("第2轮决策"), _plan("第3轮决策")]
    stub["reviews"] = [False, False, False]
    s = _make_session()
    orchestrator._run_pipeline(s)

    assert stub["synth_calls"] == 3
    assert s["result"]["answer"].endswith("回答正文")


def test_backfill_refreshes_draft_before_finalize(stub):
    """补跑分析师后（复审通过收尾）重出草稿：末轮 summarize 被再调用一次，草稿含新视角"""
    stub["plans"] = [_plan()]
    stub["reviews"] = [True]
    stub["review_needs"] = [["critic"]]
    stub["analyst_result"] = {"critic": "批判文本"}
    s = _make_session()
    orchestrator._run_pipeline(s)

    assert stub["analyst_calls"] == [["critic"]]
    assert stub["synth_calls"] == 2             # 首出草稿 + 补跑后刷新
    assert s["analyst_outputs"] == {"critic": "批判文本"}


def test_qagraphstate_fields():
    """QAGraphState 字段集固定为 6 项"""
    assert set(orchestrator.QAGraphState.__annotations__) == {
        "session", "round_no", "max_rounds", "plan", "note", "outcome"}


# ---------- 存储层（Phase 2） ----------

def test_no_lambda_in_session(stub):
    """会话 dict 可被 checkpointer serde 序列化（防闭包回归）"""
    import ormsgpack
    s = _make_session()
    ormsgpack.packb({k: v for k, v in s.items() if k != "created_at"})


def test_session_persisted_in_checkpointer(stub):
    """_run_pipeline 后，可经 checkpointer 读回会话正文"""
    stub["plans"] = [_plan()]
    stub["reviews"] = [True]
    s = _make_session(session_id="persist-1")
    orchestrator._run_pipeline(s)
    back = orchestrator._load_session("persist-1", "phrase")
    assert back is not None
    assert back["iteration_count"] == 1
    assert back["finished"] is True


def test_get_steps_maps_from_get_state(stub):
    """get_steps 字段集与冻结契约一致"""
    stub["plans"] = [_plan()]
    stub["reviews"] = [True]
    s = _make_session(session_id="steps-1")
    orchestrator._sessions["steps-1"] = {"user_id": 1, "created_at": s["created_at"], "mode": "phrase"}
    orchestrator._run_pipeline(s)
    out = orchestrator.get_steps("steps-1")
    assert set(out) == {"session_id", "query", "mode", "complexity", "steps",
                        "finished", "loop_counter", "tool_calls", "review_notes"}
    assert out["finished"] is True and out["loop_counter"] == 1


def test_ttl_cleanup_deletes_thread(stub):
    """过期会话：索引移除 + get_steps 返回 None"""
    import datetime as _d
    s = _make_session(session_id="ttl-1")
    orchestrator._sessions["ttl-1"] = {
        "user_id": 1, "created_at": _d.datetime.now() - orchestrator.SESSION_TTL - _d.timedelta(minutes=1),
        "mode": "phrase"}
    orchestrator._cleanup_expired()
    assert "ttl-1" not in orchestrator._sessions
    assert orchestrator.get_steps("ttl-1") is None


def test_memory_records_from_checkpointer(stub):
    """_memory_records 从 checkpointer 读已完成会话"""
    stub["plans"] = [_plan()]
    stub["reviews"] = [True]
    for sid in ("mem-1", "mem-2"):
        s = _make_session(session_id=sid)
        orchestrator._sessions[sid] = {"user_id": 1, "created_at": s["created_at"], "mode": "phrase"}
        orchestrator._run_pipeline(s)
    recs = orchestrator._memory_records(1, "nobody")
    assert {r["session_id"] for r in recs} >= {"mem-1", "mem-2"}
    assert all({"session_id", "query", "answer", "points"} <= set(r) for r in recs)


# ---------- 持久化（Phase 3） ----------

def test_saver_is_sqlite():
    """模块级 saver 已换为落盘的 SqliteSaver"""
    from langgraph.checkpoint.sqlite import SqliteSaver
    assert isinstance(orchestrator._PIPELINE_SAVER, SqliteSaver)


def test_pytest_uses_memory_db():
    """D6 隔离生效：pytest 下会话库为 :memory:，不得落盘到 data/"""
    assert orchestrator._QA_DB_PATH == ":memory:"


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


# ---------- quick 通道 LLM 英文关键词兜底（§35：quick 正则关键词 → 一次 LLM 英文改写） ----------

def test_quick_llm_fallback_disabled_keeps_rule_keywords(stub, monkeypatch):
    """开关关闭 → 不调 LLM，沿用正则关键词（detail 不追加兜底文案）"""
    monkeypatch.setenv("LITRADAR_QUICK_LLM_FALLBACK", "0")
    stub["complexity"] = "quick"
    stub["quick_keywords"] = ["english", "keywords"]
    s = _make_session()
    orchestrator._run_pipeline(s)

    assert stub["quick_calls"] == 0
    assert s["query_struct"]["keywords"] == ["attention"]
    assert _step(s, "rewrite")["detail"] == "快速通道｜library｜原始问句｜测试桩"
    assert s["finished"] is True and s["error"] is None


def test_quick_llm_fallback_replaces_keywords(stub, monkeypatch):
    """开关开启 + 改写成功 → 检索用英文关键词，rewrite detail 标注兜底个数"""
    stub["complexity"] = "quick"
    stub["quick_keywords"] = ["attention mechanism", "transformer"]
    seen: list[dict] = []

    def capture(struct, **k):
        seen.append(struct)
        return stub["evidence"]

    monkeypatch.setattr(orchestrator.retriever_mod, "search_library", capture)
    s = _make_session()
    orchestrator._run_pipeline(s)

    assert stub["quick_calls"] == 1
    assert seen and seen[0]["keywords"] == ["attention mechanism", "transformer"]
    assert _step(s, "rewrite")["detail"].endswith("｜LLM英文兜底关键词 2 个")
    assert _step(s, "library")["detail"] == "命中 2 条"
    assert s["finished"] is True


def test_quick_llm_fallback_failure_keeps_rule_keywords(stub):
    """改写失败（桩返回 None）→ 保留正则关键词，链路不中断"""
    stub["complexity"] = "quick"
    stub["quick_keywords"] = None
    s = _make_session()
    orchestrator._run_pipeline(s)

    assert stub["quick_calls"] == 1
    assert s["query_struct"]["keywords"] == ["attention"]
    assert _step(s, "rewrite")["detail"].endswith("｜LLM英文兜底失败，保留正则关键词")
    assert s["error"] is None and s["finished"] is True


def test_quick_llm_fallback_keeps_filters_and_query(stub, monkeypatch):
    """只换 keywords：filters / query 逐字保留（保护向量路 _dense_query 与检索层 query 回退）"""
    stub["complexity"] = "quick"
    stub["quick_keywords"] = ["english term"]
    frozen = {"year_from": "2023", "year_to": "", "sources": ["arXiv"], "cats": []}
    monkeypatch.setattr(orchestrator.rewrite_mod, "rewrite_query",
                        lambda query, mode="phrase", enabled=True: {
                            "keywords": ["x"], "query": "原始问句", "rewritten": False,
                            "filters": dict(frozen)})
    s = _make_session()
    orchestrator._run_pipeline(s)

    assert s["query_struct"]["keywords"] == ["english term"]
    assert s["query_struct"]["filters"] == frozen
    assert s["query_struct"]["query"] == "原始问句"


def test_quick_path_relaxes_topk_and_vector_weight(stub, monkeypatch):
    """§42/§43：quick 检索放宽到 14 条、向量路权重 1.8（单轮检索，靠这一步提召回）"""
    stub["complexity"] = "quick"
    stub["quick_keywords"] = ["rag", "retriever"]
    calls: list[dict] = []

    def capture(struct, **k):
        calls.append(k)
        return stub["evidence"]

    monkeypatch.setattr(orchestrator.retriever_mod, "search_library", capture)
    orchestrator._run_pipeline(_make_session())

    assert calls and calls[0]["top_k"] == orchestrator.QUICK_LIBRARY_TOP_K == 14
    assert calls[0]["vector_weight"] == orchestrator.QUICK_VECTOR_WEIGHT == 1.8


def test_fallback_path_keeps_default_retrieval_params(stub, monkeypatch):
    """回退路径不受 §42 影响：仍用 8 条 + 等权，避免改到 normal/deep 的口径"""
    calls: list[dict] = []

    def capture(struct, **k):
        calls.append(k)
        return stub["evidence"]

    monkeypatch.setattr(orchestrator.retriever_mod, "search_library", capture)
    orchestrator._run_pipeline(_make_session(enable_planner=False))

    assert calls and calls[0]["top_k"] == orchestrator.retriever_mod.LIBRARY_TOP_K == 8
    assert calls[0]["vector_weight"] == 1.0


def test_quick_guard_skips_when_already_rewritten(stub, monkeypatch):
    """守卫：本轮已经过 LLM 改写（rewritten=True）→ 不重复改写"""
    stub["complexity"] = "quick"
    stub["quick_keywords"] = ["should not be used"]
    monkeypatch.setattr(orchestrator.rewrite_mod, "rewrite_query",
                        lambda query, mode="phrase", enabled=True: {
                            "keywords": ["llm kw"], "query": "llm kw", "rewritten": True,
                            "filters": {"year_from": "", "year_to": "", "sources": [], "cats": []}})
    s = _make_session(mode="paragraph")
    orchestrator._run_pipeline(s)

    assert stub["quick_calls"] == 0
    assert s["query_struct"]["keywords"] == ["llm kw"]


def test_quick_llm_fallback_isolated_from_other_paths(stub):
    """隔离：normal / fallback 路径不得触发 quick 兜底"""
    stub["quick_keywords"] = ["english", "term"]
    stub["plans"] = [_plan()]
    stub["reviews"] = [True]
    s = _make_session()
    orchestrator._run_pipeline(s)
    assert stub["quick_calls"] == 0
    assert _step(s, "library")["detail"] == "累计证据 2 条"

    fb = _make_session(session_id="fb-1", enable_planner=False)
    orchestrator._run_pipeline(fb)
    assert stub["quick_calls"] == 0
    assert fb["planner_fallback"] is True


def test_rewrite_for_quick_caches_by_query(monkeypatch):
    """真函数级：同一问句只改写一次（模块级缓存命中，不再重复调 LLM）"""
    from ai import rewrite as rewrite_mod
    rewrite_mod._QUICK_CACHE.clear()
    calls: list[str] = []

    def fake_chat_json(agent_key, prompt, **kw):
        calls.append(agent_key)
        return {"keywords": ["cached kw"], "year_from": "", "year_to": "", "sources": [], "cats": []}

    def fake_rewrite(query, mode="phrase", enabled=True):
        return {"query": query, "rewritten": False, "keywords": ["attention"],
                "filters": {"year_from": "", "year_to": "", "sources": [], "cats": []}}

    def fake_route(question, keywords=None):
        return {"routes": ["library"], "keywords": keywords or [], "confidence": 0.9,
                "reason": "测试桩", "complexity": "quick"}

    monkeypatch.setattr(rewrite_mod, "chat_json", fake_chat_json)
    monkeypatch.setattr(orchestrator.rewrite_mod, "rewrite_query", fake_rewrite)
    monkeypatch.setattr(orchestrator.router_agent, "route", fake_route)
    monkeypatch.setattr(orchestrator.retriever_mod, "search_library", lambda *a, **k: [_ev(1), _ev(2)])
    monkeypatch.setattr(orchestrator.summarizer, "synthesize",
                        lambda q, e, a=None: {"answer": "A", "points": [], "citations": []})

    s1 = _make_session(session_id="cache-1")
    orchestrator._run_pipeline(s1)
    s2 = _make_session(session_id="cache-2")
    orchestrator._run_pipeline(s2)

    assert calls == ["qa_router"]                       # 第二次命中缓存，未再调 LLM
    assert s1["query_struct"]["keywords"] == ["cached kw"]
    assert s2["query_struct"]["keywords"] == ["cached kw"]
    rewrite_mod._QUICK_CACHE.clear()


def test_rewrite_for_quick_uses_hardened_prompt(monkeypatch):
    """提示词强化（§35 迭代）：quick 改写提示词须含「多个英文短语」硬约束，且只取 keywords"""
    from ai import rewrite as rewrite_mod
    rewrite_mod._QUICK_CACHE.clear()
    seen: list[str] = []

    def fake_chat_json(agent_key, prompt, **kw):
        seen.append(prompt)
        return {"keywords": ["Retrieval-Augmented Generation", "Large Language Models"],
                "year_from": "", "year_to": "", "sources": [], "cats": []}

    monkeypatch.setattr(rewrite_mod, "chat_json", fake_chat_json)
    out = rewrite_mod.rewrite_for_quick("论文里说的 RAG 是什么？")

    # §43：全称补齐缩写词（RAG / LLM），扩展词并入 keywords 供两路召回共用
    assert out == {"keywords": ["Retrieval-Augmented Generation", "Large Language Models",
                                "RAG", "LLM"], "rewritten": True}
    assert len(seen) == 1 and "硬性要求" in seen[0] and "3-5 个" in seen[0]
    assert "standard <原方法名>" in seen[0] and "综述或分类学" in seen[0]
    rewrite_mod._QUICK_CACHE.clear()
