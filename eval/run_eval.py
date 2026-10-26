#!/usr/bin/env python
# 多 agent 编排离线评测 runner（单文件入口）
#   用法：python eval/run_eval.py                 # 跑全部题目
#         python eval/run_eval.py --only Q1,Q8     # 只跑指定题目
#         python eval/run_eval.py --allow-web      # 允许 Planner 调用网络检索（默认禁用）
#   作用：逐题发起问答 → 轮询至完成 → 控制台打印已选题目与过程指标
#         → 全量追踪（LLM 调用/工具/步骤时间戳/token）写入 eval/record/测试记录_<时间>.txt
#   说明：进程内直接调用 orchestrator（不依赖后端 HTTP），只读数据库；LLM 未配置时自动降级也能跑通
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
ROOT = EVAL_DIR.parent
sys.path.insert(0, str(ROOT))

from ai import orchestrator, trace  # noqa: E402
from ai.llm_provider import AGENT_DEFS, get_agent_model  # noqa: E402

CASES_FILE = EVAL_DIR / "cases.json"
RECORD_DIR = EVAL_DIR / "record"
SCORE_FILE = EVAL_DIR / "score_sheet.csv"
POLL_INTERVAL = 0.5     # 轮询间隔（秒）
CASE_TIMEOUT = 1200     # 单题最长等待（秒）
PROMPT_CAP = 2000       # 记录文件中 prompt 截断长度
RESPONSE_CAP = 4000     # 记录文件中 LLM 输出截断长度
WEB_ENABLED = False     # 网络检索是否可用（--allow-web 时置真；供记录头如实标注）

STEP_LABEL = {"rewrite": "查询改写", "plan": "规划调度", "decompose": "拆解问题",
              "library": "检索文献库", "web": "检索网络", "memory": "引用历史",
              "review": "复审校验", "summarize": "汇总答案"}

_EVENTS: list[dict] = []   # 本次运行的全部事件（指向 Recorder.events，随记录实时增长）


# ---------- 载入与准备 ----------

def load_cases() -> dict:
    if not CASES_FILE.exists():
        sys.exit(f"[eval] 未找到题目集：{CASES_FILE}")
    return json.loads(CASES_FILE.read_text(encoding="utf-8"))


def agent_model_map() -> dict:
    """采集各 agent 当前绑定的模型（记录进文件头）"""
    return {k: (get_agent_model(k) or {}).get("model_id", "（未绑定）") for k in AGENT_DEFS}


def ensure_score_sheet(cases: list[dict]) -> None:
    """首次运行生成 L2 人工打分表（已存在则不覆盖，避免冲掉已填分数）"""
    if SCORE_FILE.exists():
        return
    head = ("case_id,difficulty,query,准确性(1-5),引用可信度(1-5),完整性(1-5),"
            "表达质量(1-5),应存疑(y/n),应拒答(y/n),备注\n")
    rows = [",".join([c["id"], c["difficulty"], '"%s"' % c["query"],
                      "", "", "", "", "", "", ""]) for c in cases]
    SCORE_FILE.write_text(head + "\n".join(rows) + "\n", encoding="utf-8")


def disable_web() -> None:
    """按评测范围禁用网络检索工具（零产品改动：替换进程内分发表项 + 摘出 MCP 工具集）

    注意：MCP 通道不走 _DISPATCH，若只替换分发表项，web_search 会经 MCP server 触碰真实网络，
    故同时把 web_search 从 _MCP_TOOLS 摘出，使其回落上面的进程内桩。
    """
    from ai import tools as tools_mod
    tools_mod._DISPATCH["web_search"] = lambda args, ctx: {
        "ok": True, "brief": "评测设置：跳过网络检索", "data": [], "error": ""}
    tools_mod._MCP_TOOLS = tuple(t for t in tools_mod._MCP_TOOLS if t != "web_search")


# ---------- 单题执行 ----------

def run_case(case: dict, cfg: dict) -> dict:
    """发起一次问答并轮询至完成

    :return: {sid, steps, result, routes, evidence, error}
    """
    sid = orchestrator.ask(cfg["user_id"], case["query"], mode=cfg.get("mode", "phrase"),
                           rewrite=cfg.get("rewrite", True),
                           enable_planner=cfg.get("enable_planner", True))
    deadline = time.time() + CASE_TIMEOUT
    while time.time() < deadline:
        st = orchestrator.get_steps(sid)
        if st is None or st.get("finished"):
            break
        time.sleep(POLL_INTERVAL)
    # 进程内直读会话原始态（routes/evidence 不经 HTTP 暴露，评测工具直读）
    raw = orchestrator.get_raw(sid) or {}
    return {"sid": sid, "steps": orchestrator.get_steps(sid) or {},
            "result": orchestrator.get_result(sid) or {},
            "routes": raw.get("routes") or [], "evidence": raw.get("evidence") or [],
            "error": raw.get("error")}


def hit_stats(case: dict, evidence: list[dict]) -> tuple[list, list, float]:
    """预期引用命中统计：返回 (命中, 未命中, 命中率)"""
    expect = [int(i) for i in case.get("expect_citations") or []]
    ev_ids = {str(e.get("id", "")) for e in evidence if e.get("origin") == "library"}
    hit = [i for i in expect if str(i) in ev_ids]
    miss = [i for i in expect if str(i) not in ev_ids]
    rate = (len(hit) / len(expect)) if expect else 0.0
    return hit, miss, rate


def case_tokens(events: list[dict]) -> int:
    """单题 token 合计（事件区间内所有 LLM 调用的 total_tokens 之和）"""
    return sum((e.get("usage") or {}).get("total_tokens") or 0
               for e in events if e.get("kind") == "llm")


def console_line(case: dict, out: dict, tokens: int) -> str:
    """控制台单题过程指标（ASCII 安全符号）"""
    st, res = out["steps"], out["result"]
    note = st.get("review_notes")
    review = "pass" if (note and note.get("passed")) else ("skip" if not note else "fail")
    hit, _miss, rate = hit_stats(case, out["evidence"])
    cx_ok = "OK" if st.get("complexity") == case["expect_complexity"] else "!!"
    return (f"  -> complexity={st.get('complexity')}({cx_ok}) routes={out.get('routes')} "
            f"iter={res.get('iteration_count', 0)} review={review} "
            f"evidence={len(out['evidence'])} hit={len(hit)}/{len(case.get('expect_citations') or [])}"
            f"({rate:.0%}) citations={len(res.get('citations') or [])} tokens={tokens}")


# ---------- 事件渲染 ----------

def _trunc(text: str, cap: int) -> str:
    text = text or ""
    return text if len(text) <= cap else text[:cap] + f"\n…（已截断，原文 {len(text)} 字）"


def _indent(text: str, pad: str = "      ") -> str:
    return "\n".join(pad + ln for ln in (text or "").splitlines())


def _render_event(ev: dict) -> str:
    kind, ts = ev.get("kind"), ev.get("ts", "")
    if kind == "session":
        return (f"[{ts}] [会话] query={ev.get('query')} | mode={ev.get('mode')} | "
                f"rewrite={ev.get('rewrite')} | enable_planner={ev.get('enable_planner')}")
    if kind == "step":
        label = STEP_LABEL.get(ev.get("key"), ev.get("key"))
        tail = f" | {ev.get('detail')}" if ev.get("detail") else ""
        return f"[{ts}] [步骤] {ev.get('key')}({label}) -> {ev.get('status')}{tail}"
    if kind == "tool":
        args = json.dumps(ev.get("args") or {}, ensure_ascii=False)
        flag = "ok" if ev.get("ok") else "FAIL"
        return (f"[{ts}] [工具] {ev.get('name')} -> {flag} 用时={ev.get('duration_ms')}ms\n"
                f"   args={args}\n   brief={ev.get('brief')}")
    if kind == "llm":
        u = ev.get("usage") or {}
        tok = (f"in {u.get('prompt_tokens')} / out {u.get('completion_tokens')} / "
               f"total {u.get('total_tokens')}")
        head = (f"[{ts}] [LLM] agent={ev.get('agent_key')} model={ev.get('model_id')} "
                f"用时={ev.get('duration_ms')}ms tokens({tok})")
        if ev.get("error"):
            return head + f"\n   ERROR：{ev['error']}"
        body = (f"   ├─ prompt：\n{_indent(_trunc(ev.get('prompt', ''), PROMPT_CAP))}\n"
                f"   └─ response：\n{_indent(_trunc(ev.get('response', ''), RESPONSE_CAP))}")
        return head + "\n" + body
    return f"[{ts}] [{kind}] {ev}"


def _group_events(events: list[dict]) -> list[tuple[dict, list[dict]]]:
    """按 case_start 事件切分单题区间 → [(marker, [events]), ...]"""
    groups, cur = [], None
    for ev in events:
        if ev.get("kind") == "case_start":
            cur = (ev, [])
            groups.append(cur)
        elif cur is not None:
            cur[1].append(ev)
    return groups


def _agent_token_rows(events: list[dict]) -> list[tuple[str, int, int, int, int]]:
    """各 agent 的 LLM 调用次数与 token 聚合：(agent, 次数, in, out, total)"""
    agg: dict[str, list[int]] = {}
    for ev in events:
        if ev.get("kind") != "llm":
            continue
        row = agg.setdefault(ev["agent_key"], [0, 0, 0, 0])
        u = ev.get("usage") or {}
        row[0] += 1
        row[1] += u.get("prompt_tokens") or 0
        row[2] += u.get("completion_tokens") or 0
        row[3] += u.get("total_tokens") or 0
    return [(k, *v) for k, v in sorted(agg.items(), key=lambda x: -x[1][3])]


# agent → 承载步骤的映射（用于把 LLM 的 token 归到对应步骤）
_AGENT_STEP = {"qa_router": "rewrite(查询改写)", "planner": "plan(规划调度)",
               "reviewer": "review(复审校验)", "main": "summarize(汇总答案)",
               "analyst-method": "analysts(多视角-方法)", "analyst-critic": "analysts(多视角-批判)",
               "analyst-compare": "analysts(多视角-对比)"}


def _step_token_rows(events: list[dict]) -> list[tuple[str, int]]:
    """各步骤 token（按 agent 归属聚合）→ [(步骤, token), ...]"""
    agg: dict[str, int] = {}
    for ev in events:
        if ev.get("kind") != "llm":
            continue
        step = _AGENT_STEP.get(ev["agent_key"], ev["agent_key"])
        agg[step] = agg.get(step, 0) + ((ev.get("usage") or {}).get("total_tokens") or 0)
    return sorted(agg.items(), key=lambda x: -x[1])


# ---------- 测试配置段（写入记录文件头，标注相较上一份记录的变更） ----------

# 策略性开关（由实现结构决定，非数值常量）：改动实现时须同步本表
_STRATEGY_FLAGS = {
    "过滤放宽链": "开(原filters→去年份→再去来源)",
    "LIKE兜底": "title/abstract",
    "RERANK": "BM25Okapi(零LLM)",
    "精排查询源": "本轮检索关键词",
    "截断补救": "_salvage",
    "汇总时机": "先汇总后复审(审草稿)",
    "库内年份事实注入": "开(2025-2026)",
    "keywords英文优先": "开",
    "工具通道": "MCP优先+进程内回落",
}
# 参数=值：值内不含空格与全角括号，便于跨记录解析比对
_PARAM_RE = re.compile(r"([A-Za-z_\u4e00-\u9fff][A-Za-z0-9_\u4e00-\u9fff]*)=([^\s（]+)")


def _param_map(lines: list[str]) -> dict[str, str]:
    """从「测试配置」段抽取 参数=值"""
    found: dict[str, str] = {}
    for ln in lines:
        found.update(_PARAM_RE.findall(ln))
    return found


def _prev_params() -> dict[str, str]:
    """最近一份已有记录的「测试配置」参数（用于标注「已修改」；无则空表）"""
    for f in sorted(RECORD_DIR.glob("测试记录_*.txt"), reverse=True):
        text = f.read_text(encoding="utf-8")
        if "—— 测试配置" in text:
            return _param_map(text.splitlines())
    return {}


def _config_rows() -> list[str]:
    """本次运行的关键参数行：数值取代码常量（单一事实来源），与上一份记录不同者标注「已修改」"""
    from ai import mcp_client as mcp_client_mod
    from ai import planner as planner_mod
    from ai import retriever as retriever_mod
    from ai import reviewer as reviewer_mod
    from ai import rewrite as rewrite_mod
    prev = _prev_params()
    models = agent_model_map()
    values: dict[str, object] = {
        "主链路": models.get("main", "（未绑定）"),
        "qa_router": models.get("qa_router", "（未绑定）"),
        "LIBRARY_TOP_K": retriever_mod.LIBRARY_TOP_K,
        "LIBRARY_CANDIDATES": retriever_mod.LIBRARY_CANDIDATES,
        "WEB_TOP_K": retriever_mod.WEB_TOP_K,
        "MEMORY_TOP_K": retriever_mod.MEMORY_TOP_K,
        "ABSTRACT_SNIPPET": retriever_mod.ABSTRACT_SNIPPET,
        "双路召回": "开" if retriever_mod._vector_recall_enabled() else "关",
        "quick英文兜底": "开" if rewrite_mod._quick_llm_enabled() else "关",
        "RRF_K": retriever_mod._rrf_k(),
        "VECTOR_CANDIDATES": retriever_mod.VECTOR_CANDIDATES,
        "向量模型": retriever_mod.vector_mod.MODEL_DIR.name,
        "MAX_ITERATION": planner_mod.MAX_ITERATION,
        "PLAN_MAX_TOKENS": planner_mod.PLAN_MAX_TOKENS,
        "MAX_CALLS_PER_ROUND": planner_mod.MAX_CALLS_PER_ROUND,
        "VALID_TOOLS": len(planner_mod.VALID_TOOLS),
        "REVIEW_MAX_TOKENS": reviewer_mod.REVIEW_MAX_TOKENS,
        "网络检索": "开" if WEB_ENABLED else "关",
        "MCP开关": "开" if mcp_client_mod.enabled() else "关",
        "单入口": "eval/run_eval.py",
        **_STRATEGY_FLAGS,
    }

    def p(key: str) -> str:
        mark = "（已修改）" if key in prev and prev[key] != str(values[key]) else ""
        return f"{key}={values[key]}{mark}"

    return [
        "  模型  " + p("主链路") + "  " + p("qa_router"),
        "  检索  " + "  ".join(p(k) for k in ("LIBRARY_TOP_K", "LIBRARY_CANDIDATES",
                                             "WEB_TOP_K", "MEMORY_TOP_K", "ABSTRACT_SNIPPET")),
        "  召回  " + "  ".join(p(k) for k in ("过滤放宽链", "LIKE兜底", "quick英文兜底")),
        "  向量  " + "  ".join(p(k) for k in ("双路召回", "RRF_K", "VECTOR_CANDIDATES", "向量模型")),
        "  精排  " + "  ".join(p(k) for k in ("RERANK", "精排查询源")),
        "  规划  " + "  ".join(p(k) for k in ("MAX_ITERATION", "PLAN_MAX_TOKENS",
                                             "MAX_CALLS_PER_ROUND", "VALID_TOOLS")),
        "  复审  " + "  ".join(p(k) for k in ("REVIEW_MAX_TOKENS", "截断补救", "汇总时机")),
        "  提示词  " + "  ".join(p(k) for k in ("库内年份事实注入", "keywords英文优先")),
        "  评测  " + "  ".join(p(k) for k in ("工具通道", "MCP开关", "网络检索", "单入口")),
    ]


# ---------- 记录文件 ----------

def write_record(cases: list[dict], outs: dict, events: list[dict],
                 started: datetime, cfg: dict) -> Path:
    """写评测记录 txt：文件头 + 逐题（事件流/过程指标/回答/GT/打分位）+ 汇总"""
    RECORD_DIR.mkdir(parents=True, exist_ok=True)
    path = RECORD_DIR / f"测试记录_{started:%Y%m%d_%H%M%S}.txt"
    sep = "=" * 78
    L: list[str] = [sep, "文献雷达 · 多 agent 编排评测记录",
                    f"测试时间：{started:%Y-%m-%d %H:%M:%S}    题目数：{len(cases)}"
                    f"（简单 {_cnt(cases, '简单')} / 普通 {_cnt(cases, '普通')} / 困难 {_cnt(cases, '困难')}）",
                    f"会话参数：user_id={cfg['user_id']} mode={cfg.get('mode')} "
                    f"rewrite={cfg.get('rewrite')} enable_planner={cfg.get('enable_planner')}",
                    "agent 绑定的 LLM：" + "，".join(f"{k}={v}" for k, v in agent_model_map().items()),
                    sep, "",
                    "—— 测试配置（标「已修改」者为相较上一份记录发生的变更）——",
                    *_config_rows()]

    groups = _group_events(events)
    for case, (marker, evs) in zip(cases, groups):
        out = outs[case["id"]]
        st, res = out["steps"], out["result"]
        note = st.get("review_notes")
        hit, miss, rate = hit_stats(case, out["evidence"])
        cx = st.get("complexity")
        L += ["", "─" * 78,
              f"【{case['id']}】难度={case['difficulty']}   期望档位={case['expect_complexity']}   "
              f"期望路由={case['expect_routes']}",
              f"问句：{case['query']}",
              f"session_id：{out['sid']}   末事件时间：{evs[-1]['ts'] if evs else ''}",
              "─" * 78,
              "—— 事件流（时间戳 / 步骤 / LLM / 工具）——"]
        L += [_render_event(ev) for ev in evs]
        L += ["",
              "—— 过程指标（L1）——",
              f"档位判定：{cx}（期望 {case['expect_complexity']}）"
              f" {'一致' if cx == case['expect_complexity'] else '不一致'}",
              f"路由通道：{out.get('routes')}   迭代轮次：{res.get('iteration_count', 0)}",
              f"复审：{'通过' if (note and note.get('passed')) else ('跳过/不可用' if not note else '未通过')}"
              f"   证据条数：{len(out['evidence'])}   引用条数：{len(res.get('citations') or [])}",
              f"检索命中率：{len(hit)}/{len(case.get('expect_citations') or [])} ({rate:.0%})   "
              f"命中 id={hit}   未命中 id={miss}",
              f"本题目 token：{case_tokens(evs)}"]
        for step, tk in _step_token_rows(evs):
            L.append(f"  · 步骤 {step}：{tk} tokens")
        if out.get("error"):
            L.append(f"⚠ 会话错误：{out['error']}")
        L += ["", "—— 最终回答（供人工打分）——", res.get("answer") or "（无回答）", "",
              "—— Ground truth（以库内 abstract 原文为准）——",
              f"gt_core：{case.get('gt_core', '')}"]
        L += [f"  - {p}" for p in (case.get("gt_points") or [])]
        if case.get("gt_numbers"):
            L.append("量化锚点：" + "；".join(case["gt_numbers"]))
        L += ["", "—— L2 人工打分（请回填到 eval/score_sheet.csv）——",
              "准确性(1-5)=__  引用可信度(1-5)=__  完整性(1-5)=__  表达质量(1-5)=__  "
              "应存疑(y/n)=__  应拒答(y/n)=__  备注：__"]

    L += ["", sep, "汇总", sep,
          f"{'题目':<6}{'档位':<8}{'迭代':<6}{'复审':<7}{'证据':<6}{'命中率':<8}{'token':<8}"]
    for case, (_m, evs) in zip(cases, groups):
        out, st, res = outs[case["id"]], outs[case["id"]]["steps"], outs[case["id"]]["result"]
        note = st.get("review_notes")
        hit, _x, rate = hit_stats(case, out["evidence"])
        rev = "pass" if (note and note.get("passed")) else ("skip" if not note else "fail")
        L.append(f"{case['id']:<6}{st.get('complexity', ''):<8}{res.get('iteration_count', 0):<6}"
                 f"{rev:<7}"
                 f"{len(out['evidence']):<6}{f'{rate:.0%}':<8}{case_tokens(evs):<8}")
    rows = _agent_token_rows(events)
    L += ["", "各 agent LLM 调用与 token："]
    L += [f"  {a:<16} 调用 {n} 次   in {i} / out {o} / total {t}" for a, n, i, o, t in rows]
    L += [f"\n总 token：{sum(r[4] for r in rows)}",
          f"\nL2 打分表：{SCORE_FILE}", f"记录文件：{path}"]
    path.write_text("\n".join(L) + "\n", encoding="utf-8")
    return path


def _cnt(cases: list[dict], diff: str) -> int:
    return sum(1 for c in cases if c["difficulty"] == diff)


# ---------- 主流程 ----------

def main() -> None:
    global _EVENTS, WEB_ENABLED
    ap = argparse.ArgumentParser(description="多 agent 编排离线评测 runner")
    ap.add_argument("--only", default="", help="只跑指定题目，逗号分隔（如 Q1,Q8）")
    ap.add_argument("--allow-web", action="store_true",
                    help="允许 Planner 调用网络检索（默认关闭，满足\"先不测试网络搜索\"）")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    # 评测必须真实跑链路：问答缓存会让同一题第二次运行直接回放旧答案，命中率/token 全部失真
    os.environ["LITRADAR_QA_CACHE"] = "0"
    if not args.allow_web:
        disable_web()
        print("[eval] 已禁用网络检索（如需开启请加 --allow-web）")
    else:
        WEB_ENABLED = True
        print("[eval] 已允许网络检索（--allow-web）")

    data = load_cases()
    cfg = {k: data.get(k) for k in ("user_id", "mode", "rewrite", "enable_planner")}
    cases = data["cases"]
    if args.only:
        want = {x.strip() for x in args.only.split(",") if x.strip()}
        cases = [c for c in cases if c["id"] in want]
    ensure_score_sheet(data["cases"])

    print("=" * 70)
    print(f"多 agent 编排评测：共 {len(cases)} 题")
    for c in cases:
        print(f"  [{c['id']}] {c['difficulty']:<4} 期望={c['expect_complexity']:<7} 问句：{c['query'][:46]}")
    print("=" * 70)

    rec = trace.Recorder()
    trace.set_active(rec)
    _EVENTS = rec.events          # 事件随记录实时增长，控制台与文件共用同一份
    started = datetime.now()
    outs: dict[str, dict] = {}
    try:
        for c in cases:
            mark = len(rec.events)
            rec.add("case_start", case_id=c["id"])
            print(f"\n[{c['id']}] {c['difficulty']} · {c['query']}")
            t0 = time.time()
            out = run_case(c, cfg)
            outs[c["id"]] = out
            evs = rec.events[mark:]
            print(console_line(c, out, case_tokens(evs)) + f" 用时={time.time() - t0:.1f}s")
            print(f"   answer={((out['result'].get('answer') or '')[:80]).replace(chr(10), ' ')}…")
    finally:
        trace.clear()

    path = write_record(cases, outs, rec.events, started, cfg)
    print("\n" + "=" * 70)
    print(f"记录已写入：{path}")
    print(f"L2 打分表：{SCORE_FILE}")


if __name__ == "__main__":
    main()
