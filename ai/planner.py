# Planner 主 Agent（编排方案 v4 §2.2 / 详细设计 §5.1.4）：总指挥——拆解任务、输出工具调用计划
# 工作流（v4 §4.1）：router 判定 normal/deep → Planner 拆解 + 工具调用 → 执行 → run_reviewer 质检
#   → pass 或 loop_counter ≥ MAX_LOOP → Summarizer；否则带复审建议进入下一轮
# 失败降级（v4 §5）：LLM 不可用 / 输出非法 / 解析失败 → None，调用方回退 v3 固定流水线
from datetime import datetime

from ai.llm_provider import chat_json

MAX_ITERATION = 3        # 全局迭代上限（v4 §4.2 MAX_LOOP=3，与 §5.1.3 MAX_ITERATION=3 一致）
PLAN_MAX_TOKENS = 768    # 单轮规划 token 上限（每轮一次 LLM 调用，§7 成本控制）
MAX_CALLS_PER_ROUND = 3  # 单轮工具调用上限（§7 硬上限）

# Planner 可选工具（run_reviewer 由编排器在工具阶段后统一执行，不在此列，避免重复质检）
VALID_TOOLS = ("library_search", "web_search", "memory_search", "rewrite_query", "run_analysts")

_SYSTEM = "你是文献问答规划主 Agent（Planner）。只输出 JSON，不输出其他内容。"

_PROMPT = """你是文献问答的规划主 Agent。理解用户问题、拆解为子任务，并输出本轮要调用的工具。

已知事实（据此设定检索条件，勿臆造）：
- 今天是 {today}；本库文献集中在 2025–2026 年。
- library_search 的年份过滤必须落在本库覆盖范围内（2025–2026）；不确定时留空，不要臆造年份。
- library_search 的 keywords 优先使用英文术语（本库以英文文献为主，英文关键词命中率更高）。

可用工具（每轮最多 {max_calls} 个）：
- library_search：本地文献库检索，参数 {{"keywords": ["关键词"], "sources": ["来源"], "year_from": "起始年", "year_to": "结束年"}}
- web_search：网络实时学术检索，参数 {{"keywords": ["关键词"]}}
- memory_search：读取历史会话上下文，参数 {{}}
- rewrite_query：查询改写，参数 {{"user_query": "原始问句"}}
- run_analysts：调用分析师集群，参数 {{"enable_list": ["method"|"critic"|"compare"]}}
  （method=方法抽取 / critic=批判审阅 / compare=横向对比；综述/多视角类问题建议全启用）

约束：
1. 证据已足够回答用户问题时，tool_calls 置空数组，表示结束检索、进入汇总；
2. 不要重复调用已执行过的相同检索；
3. 只输出 JSON，不要解释。

用户问题：{question}
当前轮次：第 {round_no} 轮（最多 {max_iteration} 轮迭代）
当前证据概况：{evidence_summary}
上一轮复审建议：{review_suggestion}

输出 JSON：
{{"thought": "本轮决策依据一句话", "subtasks": ["拆解出的子任务"], "tool_calls": [{{"tool": "工具名", "args": {{}}}}]}}"""


def plan(question: str, round_no: int, evidence_summary: str,
         review_suggestion: str = "") -> dict | None:
    """Planner 规划决策（一次 LLM 调用）

    :param round_no: 当前迭代轮次（1 起）
    :param evidence_summary: 确定性证据概况（各通道命中数 + top 标题，由调用方拼装）
    :param review_suggestion: 上一轮 Reviewer 给出的改进指令（首轮为空）
    :return: {"thought": str, "subtasks": [str], "tool_calls": [{"tool": str, "args": dict}]}
             LLM 不可用 / 输出非法 → None（调用方回退 v3 固定流水线）
    """
    try:
        struct = chat_json("planner", _PROMPT.format(
            max_calls=MAX_CALLS_PER_ROUND, question=question, round_no=round_no,
            max_iteration=MAX_ITERATION,
            today=datetime.now().strftime("%Y-%m-%d"),
            evidence_summary=evidence_summary or "（尚无证据）",
            review_suggestion=review_suggestion or "（无）",
        ), system=_SYSTEM, max_tokens=PLAN_MAX_TOKENS)
    except Exception as e:
        print(f"[planner] 规划失败（回退 v3 固定流水线）: {e}")
        return None
    if not struct or not isinstance(struct, dict):
        return None
    return {
        "thought": str(struct.get("thought") or "")[:200],
        "subtasks": [str(s).strip() for s in (struct.get("subtasks") or []) if str(s).strip()][:6],
        "tool_calls": _clean_calls(struct.get("tool_calls")),
    }


def _clean_calls(raw: object) -> list[dict]:
    """工具调用合法性校验：未知工具 / 非法参数一律丢弃（不可信 LLM 输出）"""
    calls = []
    for c in (raw or [])[:MAX_CALLS_PER_ROUND]:
        if not isinstance(c, dict) or c.get("tool") not in VALID_TOOLS:
            continue
        args = c.get("args")
        calls.append({"tool": c["tool"], "args": args if isinstance(args, dict) else {}})
    return calls
