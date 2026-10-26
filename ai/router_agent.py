# 检索路由 Agent：决定问题走哪一路（或组合）检索通道 + 复杂度分级（对齐 AI 方案 §2.4 / 编排方案 v3 §3）
# 规则优先（确定性、零成本）→ 置信度不足时 LLM 兜底（qa_router 绑定模型，一次调用同出路由+档位）
# 通道：library（文献库全文）/ web（实时网络）/ memory（历史会话记忆）
# 档位：quick（快速通道，跳过多视角）/ normal（标准 5 步）/ deep（主 agent 编排）
import re

from ai.llm_provider import chat_json

ROUTES = ("library", "web", "memory")
COMPLEXITIES = ("quick", "normal", "deep")

# 规则表：正则 → (通道, 置信度)
_RULES: list[tuple[str, str, float]] = [
    # 指代历史会话 → 记忆
    (r"(刚才|之前|上次|前面|我们(说|聊|讨论)过|你(刚才|之前)?(说|提|讲)过|上文|上一(个)?问题)", "memory", 0.9),
    # 时效性诉求 → 实时网络
    (r"(最新|最近|今日|本月|上周|刚刚|新闻|热点|预印本|抢先看|arXiv 今日)", "web", 0.85),
    # 明确文献语境 → 文献库
    (r"(文献|论文|期刊|这篇|那篇|摘要|方法|实验|数据集|引用|参考文献|综述|方向|订阅)", "library", 0.8),
]

# ---------- 复杂度分级：规则信号表（零成本，优先；编排方案 v3 §3.1） ----------

# 跨会话指代 / 综述型动作 / 约束词（用于信号打分）
_RE_CROSS_REF = re.compile(r"(刚才|上次|前面|我们(说|聊|讨论)过|上文|上一(个)?问题)")
_RE_NEAR_REF = re.compile(r"(该|此|其)[一这那篇个方向]")
_RE_DEEP_ACTION = re.compile(r"(综述|批判|多视角|方案设计|对比分析|系统梳理|深度分析)")
_RE_NORMAL_ACTION = re.compile(r"(为什么|如何|区别|差异|原理|优劣|比较)")
_RE_QUICK_ACTION = re.compile(r"(是什么|有哪些|是什么意思|定义|指的?是)")
_RE_CONSTRAINT = re.compile(r"(年内|近[一两三五]|最[新近]|来源|期刊|arXiv|限定|范围内|不超过|以上以下)")


def _complexity_by_rules(question: str) -> tuple[str, float]:
    """五维信号投票 → (档位, 规则置信度)

    信号维度（§3.1）：问句长度 / 指代范围 / 动作类型 / 约束条件 / 上下文依赖；
    多数信号落档即取该档；信号一致性与置信度挂钩（全一致 0.9，4/5 一致 0.8，更低返回 <0.8 走 LLM 兜底）。
    """
    q = question or ""
    votes: list[str] = []
    # ① 问句长度 + 子问数（>120 字或 2+ 子问 → deep）
    n_sub = q.count("？") + q.count("?")
    if len(q) <= 30 and n_sub <= 1:
        votes.append("quick")
    elif len(q) > 120 or n_sub >= 2:
        votes.append("deep")
    else:
        votes.append("normal")
    # ② 指代范围
    if _RE_CROSS_REF.search(q):
        votes.append("deep")
    elif _RE_NEAR_REF.search(q):
        votes.append("normal")
    else:
        votes.append("quick")
    # ③ 动作类型
    if _RE_DEEP_ACTION.search(q):
        votes.append("deep")
    elif _RE_NORMAL_ACTION.search(q):
        votes.append("normal")
    elif _RE_QUICK_ACTION.search(q):
        votes.append("quick")
    else:
        votes.append("normal")  # 动作不明 → 中性票
    # ④ 约束条件数
    n_constraints = len(set(_RE_CONSTRAINT.findall(q)))
    votes.append("deep" if n_constraints >= 3 else ("normal" if n_constraints >= 1 else "quick"))
    # ⑤ 上下文依赖（跨会话指代强依赖；近距离指代=依赖当前会话少量上下文 → normal）
    if _RE_CROSS_REF.search(q):
        votes.append("deep")
    elif _RE_NEAR_REF.search(q):
        votes.append("normal")
    else:
        votes.append("quick")

    counts = {c: votes.count(c) for c in COMPLEXITIES}
    top = max(counts, key=counts.get)
    agreement = counts[top] / len(votes)
    if agreement == 1.0:
        return top, 0.9
    if agreement >= 0.8:
        return top, 0.8
    # 平票 / 信号冲突 → 交给 LLM 兜底（置信度不足 0.8）
    return "normal", 0.5


_LLM_SYSTEM = "你是检索路由器。只输出 JSON，不输出其他内容。"

_LLM_PROMPT = """把用户问题路由到检索通道，并判定问题复杂度档位。可用通道：
- library：本系统文献库（已入库的订阅文献，全文检索）
- web：实时网络检索（外部数据源实时拉取，适合最新/库内没有的内容）
- memory：历史会话记忆（用户之前问过/聊过的内容）

复杂度档位（编排深度）：
- quick：简短事实查询（单一意图、无约束、无上下文依赖）
- normal：常规解释/比较类问题（中等长度、1-2 个约束）
- deep：综述/批判/多子问题/多约束/强上下文依赖（需要多视角深度分析）

输出 JSON：
{{"routes": ["选择 1-2 个通道"], "keywords": ["检索关键词 2-5 个"], "complexity": "quick|normal|deep"}}

用户问题：{query}"""


def route(question: str, keywords: list[str] | None = None) -> dict:
    """路由决策入口（编排方案 v3 §3.2：一次调用同时输出路由与档位）

    :return: {routes: list[str], keywords: list[str], confidence: float,
              reason: str, complexity: str}
    """
    # ① 复杂度规则判定（零成本优先）：置信度 ≥0.8 时规则档确定，无需 LLM
    cx, cx_conf = _complexity_by_rules(question)
    rule_cx_decided = cx_conf >= 0.8

    # ② 通道规则命中 + 复杂度规则确定 → 零 LLM 直接返回
    if rule_cx_decided:
        for pattern, route_name, conf in _RULES:
            if re.search(pattern, question or ""):
                return {"routes": [route_name], "keywords": keywords or [],
                        "confidence": conf, "reason": f"规则命中({route_name})",
                        "complexity": cx}

    # ③ LLM 兜底（复杂度信号冲突 或 通道规则未覆盖）：一次调用同出路由 + 档位（不新增调用成本）
    try:
        struct = chat_json("qa_router", _LLM_PROMPT.format(query=question),
                           system=_LLM_SYSTEM, max_tokens=256)
        if struct and isinstance(struct.get("routes"), list):
            routes = [r for r in struct["routes"] if r in ROUTES]
            if routes:
                llm_kw = [k for k in (struct.get("keywords") or []) if isinstance(k, str) and k.strip()]
                complexity = struct.get("complexity")
                complexity = complexity if complexity in COMPLEXITIES else "normal"
                return {"routes": routes[:2], "keywords": keywords or llm_kw,
                        "confidence": 0.7, "reason": f"LLM 判定({complexity})", "complexity": complexity}
    except Exception as e:  # LLM 不可用/上游失败 → 降级默认通道与默认档（失败回退原则）
        print(f"[router] LLM 路由失败，降级规则默认: {e}")

    # ④ 终极兜底：文献库（默认通道）+ 已确定的规则档或 normal（默认档）
    return {"routes": ["library"], "keywords": keywords or [],
            "confidence": 0.3, "reason": "默认通道",
            "complexity": cx if rule_cx_decided else "normal"}
