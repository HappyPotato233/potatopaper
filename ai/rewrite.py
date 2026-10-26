# 查询改写：自由问句 → 结构化检索式（对齐 AI 方案 §2.5）
# 三形态适配：phrase（短语直接命中）/ paragraph（拆句并发改写）/ strategy（自定义检索式跳过改写）
# 失败回退：LLM 不可用 / 解析失败 / 校验不通过 → 使用原始 query，不阻断检索
import os
import re

from ai.llm_provider import LlmUnavailableError, chat_json

# 过滤标签前缀（strategy 模式解析依据）：year: / source: / cat:
_FILTER_RE = re.compile(r"(\w+):([^\s]+)")

SYSTEM = (
    "你是科研文献检索助手，负责把用户的自然语言问题改写为结构化检索式。"
    "只输出 JSON，不输出其他内容。"
)

PROMPT = """把下面的{src_desc}改写为文献检索用 JSON：
{{
  "keywords": ["提取 2-6 个核心检索关键词，优先输出英文术语（本库以英文文献为主，英文命中率更高），去掉无实义停用词"],
  "year_from": "起始年份字符串，如 '2023'，未提及则空串",
  "year_to": "结束年份字符串，未提及则空串",
  "sources": ["提及的来源，仅限 arXiv/Semantic Scholar/万方 之一或多个，未提及则空数组"],
  "cats": ["学科分类如 cs.AI，未提及则空数组"]
}}

用户输入：{query}"""

# quick 通道专用提示词：在通用提示词上追加「按提问类型取具体对象名」的硬约束。
#   背景（§35 第二轮）：早先版本把示例术语（Retrieval-Augmented Generation / GraphRAG 等）
#   写死在提示词里，本地 8B 模型会把示例当答案逐字复读——三个 quick 题输出同一组词，
#   「核心组件」类问句的期望文献因此稳定落榜（文档变更记录 §39.1）。
#   故此处**不出现任何领域具体术语**，只描述取词规则；示例一律不写，避免再次被复读。
#   无花括号，可安全参与 .format(src_desc=…, query=…)。
QUICK_PROMPT = PROMPT + """

硬性要求（必须遵守）：
1. keywords 给 3-5 个，互不相同，不要只输出一个缩写词；
2. 第 1 个关键词是问句所问对象的英文规范全称；
3. 其余关键词必须是回答该问句所必需的具体对象名，按问句的提问类型取词：
   - 问「有哪些 / 包含什么 / 由什么组成 / 分几类」→ 取该对象的各个组成部分名称；
   - 问「是什么 / 原理 / 怎么工作」→ 取该机制的关键环节名称或同类方法名称；
   - 问「区别 / 对比」→ 取被比较双方的名称；
   - 问「效果 / 指标 / 成本」→ 取评价维度或指标名称；
4. 只依据用户问句取词；禁止把本提示词中的任何字句当作关键词；
5. 全部用英文，不要输出中文关键词。
6. 若问句问的是某个方法的定义或原理，且该方法名是由另一方法名派生而来（即它是那个方法的变体），
   必须再补一条「原方法的常规做法」作对照，写成 standard <原方法名> 的形式；
7. 若问句问的是「有哪些 / 组成 / 分几类」，额外补一个综述或分类学视角的英文术语。"""


def rewrite_query(query: str, mode: str = "phrase", enabled: bool = True) -> dict:
    """改写入口（对齐 AI 方案 §2.5 伪代码）

    :param query: 用户原始问句
    :param mode: phrase / paragraph / strategy
    :param enabled: 是否走 LLM 改写（strategy 模式始终跳过）
    :return: {query, rewritten, keywords, filters:{year_from, year_to, sources, cats}}
    """
    query = (query or "").strip()
    # strategy：用户自定义检索式，跳过改写，仅解析过滤标签
    if mode == "strategy":
        return _parse_strategy(query)
    # phrase：短语直接命中，不走 LLM；仅做确定性关键词抽取
    if mode == "phrase" or not enabled:
        return _fallback(query)
    # paragraph：整段问句走 LLM 改写（长段落内部分句由提示词自然覆盖，避免多次调用）
    try:
        struct = chat_json("qa_router", PROMPT.format(src_desc="问题段落", query=query),
                           system=SYSTEM, max_tokens=512)
        if _validate(struct):
            keywords = [k for k in struct["keywords"] if k and k.strip()]
            return {
                "query": " ".join(keywords) or query,
                "rewritten": True,
                "keywords": keywords,
                "filters": {
                    "year_from": str(struct.get("year_from") or ""),
                    "year_to": str(struct.get("year_to") or ""),
                    "sources": list(struct.get("sources") or []),
                    "cats": list(struct.get("cats") or []),
                },
            }
    except Exception as e:  # LLM 不可用/上游失败 → 回退原始 query（失败回退原则）
        print(f"[rewrite] LLM 改写失败，回退原始 query: {e}")
    return _fallback(query)


def _validate(struct: dict | None) -> bool:
    """结构化检索式校验：必须是非空 keywords 的 dict"""
    return bool(struct and isinstance(struct.get("keywords"), list)
                and any(k.strip() for k in struct["keywords"] if isinstance(k, str)))


def _fallback(query: str) -> dict:
    """失败回退：原始问句 + 规则关键词抽取（确定性，零 LLM）"""
    return {"query": query, "rewritten": False, "keywords": extract_keywords(query),
            "filters": {"year_from": "", "year_to": "", "sources": [], "cats": []}}


def _parse_strategy(query: str) -> dict:
    """strategy 模式：解析 '关键词 year:2023 source:arXiv' 形式的自定义检索式"""
    filters = {"year_from": "", "year_to": "", "sources": [], "cats": []}
    keywords: list[str] = []
    year_vals: list[str] = []
    for token in query.split():
        m = _FILTER_RE.fullmatch(token)
        if not m:
            keywords.append(token)
            continue
        key, val = m.group(1).lower(), m.group(2)
        if key == "year":
            if "-" in val:  # year:2023-2025
                lo, _, hi = val.partition("-")
                filters["year_from"], filters["year_to"] = lo, hi
            else:
                year_vals.append(val)
        elif key == "source":
            filters["sources"].append(val)
        elif key == "cat":
            filters["cats"].append(val)
        else:
            keywords.append(token)
    if year_vals:
        filters["year_from"] = filters["year_to"] = year_vals[0]
    return {"query": " ".join(keywords) or query, "rewritten": False,
            "keywords": keywords, "filters": filters}


# ---------- quick 通道关键词兜底（规则 bigram → 一次 LLM 英文改写） ----------
# 背景：quick 路径原仅用正则抽词，中文问句切出的碎片词会撞上 MySQL 全文检索 50% 阈值，
#   导致词法零命中（见文档变更记录 §32.3）。此处复用 paragraph 提示词，把 quick 的关键词
#   来源换成 LLM 英文术语，命中率显著提升；失败则回退规则关键词，不阻断检索。
# 开关：LITRADAR_QUICK_LLM_FALLBACK（默认开，置 0 关闭）
_QUICK_CACHE: dict[str, list[str]] = {}   # query → 英文关键词（同问句只改一次）

# 复读护栏（§39）：旧提示词把示例术语写死在文本里，模型曾把示例当答案原样输出。
#   提示词已删除示例，此处保留一道确定性校验：若输出词全部来自旧示例集合、
#   且问句本身根本没提到这些词，则判定为「复读提示词」并带纠偏提示重试一次。
_ECHO_TERMS = frozenset({
    "retrieval-augmented generation", "large language models",
    "graphrag", "knowledge graph",
})
_ECHO_PROBE = ("rag", "graphrag", "knowledge graph", "large language model")
_ECHO_RETRY_NOTE = (
    "\n注意：上一次输出与提示词示例雷同、且只给了泛化主题词，未回答问句所问的具体方面。"
    "请严格依据用户问句重新取词，补充该问句所需的具体对象名（组成部分 / 关键环节 / 同类方法），"
    "不要复用任何示例词。"
)


def _quick_llm_enabled() -> bool:
    """quick 通道 LLM 关键词兜底开关（默认开）"""
    return os.getenv("LITRADAR_QUICK_LLM_FALLBACK", "1") != "0"


def _is_prompt_echo(keywords: list[str], query: str) -> bool:
    """复读判定：输出词全部落在旧示例集合内（≥2 个），且问句未提到该集合中任何词

    问句豁免：Q1「…RAG 是什么？」、Q3「什么是 GraphRAG 方法？」的问句本身含 rag /
    graphrag，属**合法主题词**，不判复读（保持两题现状不改）；Q2 问句只有中文
    「检索增强生成」，不构成豁免 → 命中护栏。
    """
    norm = {k.strip().lower() for k in keywords if isinstance(k, str) and k.strip()}
    if len(norm) < 2 or not norm <= _ECHO_TERMS:
        return False
    q = (query or "").lower()
    return not any(p in q for p in _ECHO_PROBE)


def _quick_keywords(query: str, prompt: str) -> list[str] | None:
    """调一次 qa_router 取 keywords；LLM 不可用 / 校验不通过 → None

    温度固定为 0：quick 关键词是「问句 → 词」的确定性映射，不需要发挥。
    实测同一问句在默认温度下会换词，个别轮次换成 retrieval mechanism / generated content
    这类泛词，把期望文献压出前 8（Q1 五轮 2/2、2/2、1/2、0/2、2/2）。温度 0 后同一问句
    每次输出完全相同，Q1 稳定 2/2（§40）。

    解析失败重试一次（§43）：deepseek-flash 即使 temperature=0 也有约两成概率返回非 JSON
    文本（Q3 实测五次里一次），直接返回 None 会让整轮退回正则碎片词。模型不可用/上游报错
    不重试（重试也会失败、只是白等），仅「返回了内容但解析不出 keywords」时补一次。
    """
    for _ in range(2):
        try:
            struct = chat_json("qa_router", prompt.format(src_desc="问题", query=query),
                               system=SYSTEM, max_tokens=512, temperature=0)
        except Exception as e:  # LLM 不可用/上游失败 → 交调用方回退规则关键词
            print(f"[rewrite] quick 英文关键词兜底失败，保留规则关键词: {e}")
            return None
        if _validate(struct):
            return [k.strip() for k in struct["keywords"] if isinstance(k, str) and k.strip()] or None
    return None


# ---------- 施事名词派生（§39 续：第二组召回关键词） ----------
# 背景：本地 8B 把「核心组件」写成归类标签（Retrieval Component / Generation Component），
#   而文献摘要用的是具体部件名（retrievers、generator models）。此处按**纯英语构词规则**
#   把标签还原成施事名词，不含任何领域词表；派生出的词集作为第二组关键词各召回一次，
#   两组结果由检索层做 RRF 融合（见 ai/retriever._fuse_keyword_sets）。
# 规则：-ion → -or（generation→generator）、-al → -er（retrieval→retriever）、
#       -ing → -er（indexing→indexer、ranking→ranker）。
_LABEL_TAIL_RE = re.compile(r"\s+(?:components?|modules?|parts?)$", re.IGNORECASE)


def _agent_noun(base: str) -> str | None:
    """由动作名词派生施事名词（best-effort）；无法派生返回 None"""
    w = re.sub(r"[^a-z]", "", (base or "").lower())
    if len(w) < 4:
        return None
    if w.endswith("ion"):
        return w[:-3] + "or"
    if w.endswith("ing"):
        return w[:-3] + "er"
    if w.endswith("al"):
        return w[:-2] + "er"
    return None


def _derive_alt_keywords(keywords: list[str]) -> list[str] | None:
    """把归类标签式关键词还原为「对象词 + 施事名词」的第二组检索词

    :return: 第二组关键词；输出里没有 X Component/Module/Part 形式时不派生，返回 None
    """
    labels, object_kw = [], None
    for k in keywords:
        if _LABEL_TAIL_RE.search(k):
            labels.append(_LABEL_TAIL_RE.sub("", k))
        elif object_kw is None:
            object_kw = k
    if not labels:
        return None
    derived = [_agent_noun(b) for b in labels]
    derived = [d for d in derived if d]
    if not derived:
        return None
    out = [object_kw or labels[0]]
    for d in derived:
        if d.lower() not in {x.lower() for x in out}:
            out.append(d)
    return out if len(out) >= 2 else None


# ---------- 检索词确定性扩展（§43：复合词拆形 + 缩写派生） ----------
# 背景：Q2 的期望文献只有与 taxonomy / survey 这类分类学词共现时才排进前列；
#   Q3 的对照文献（标准 RAG）只有检索词里出现 standard RAG 字面时才进前 12。
#   这两类词由提示词第 6/7 条让模型产出，本层只负责把模型给的写法补齐成语料里
#   实际出现的形态：
#   ① 复合词拆形：语料摘要写 Graph RAG，而 MySQL 分词把 GraphRAG 当单 token，
#      词法路两端对不上；拆成 Graph RAG 后能被 LIKE/FULLTEXT 命中。
#   ② 缩写派生：语料高频用 RAG / LLM 指代全称，长全称在 BM25 里与摘要词面不一致；
#      取短语里首字母大写的实词拼缩写，非大写词原位保留（standard X → standard RAG）。
#   纯规则、零 LLM、无领域词表；扩展词直接并入 keywords，词法路与向量路读同一份，
#   避免只扩一路造成两路名次错位（实测只扩词法路时 Q3 反而掉回 1/2）。
_CAMEL_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z]+|[a-z]+|\d+")


def _split_compound(kw: str) -> str | None:
    """复合词拆形：GraphRAG → Graph RAG；非驼峰复合词返回 None

    只拆「首字母大写 + 全大写缩写」这类写法。连字符不拆：Retrieval-Augmented Generation
    本身已能被分词器切成三个词，再拆只会制造重复词。
    """
    parts = _CAMEL_RE.findall(kw or "")
    if len(parts) < 2:
        return None
    if "".join(parts).lower() != (kw or "").replace(" ", "").lower():
        return None
    out = " ".join(parts)
    return out if out.lower() != (kw or "").lower() else None


def _acronym(kw: str) -> str | None:
    """缩写派生：Retrieval-Augmented Generation → RAG；standard … → standard RAG

    只取首字母大写（或全大写）的实词拼缩写，其余词原位保留。缩写短于 3 个字母不派生
    （实测两字母缩写 KG / DR / RM 在本语料里几乎不作为术语出现，属噪声）。
    """
    words = [w for w in re.split(r"[\s\-_]+", kw or "") if w]
    caps = [w for w in words if w[:1].isupper() or w.isupper()]
    if len(caps) < 2:
        return None
    abbr = "".join(w[0].upper() for w in caps)
    if len(abbr) < 3:
        return None
    return " ".join([w for w in words if w not in caps] + [abbr])


def _qualified_acronym(kw: str) -> str | None:
    """限定词 + 长全称 → 限定词 + 缩写：Graph Retrieval-Augmented Generation → Graph RAG

    与 _acronym 的差别：首个词是限定词（大写开头，且本身不参与缩写），缩写只取它后面
    的大写实词。语料里这类方法的常用名正是「限定词 + 缩写」，长全称在词法路上排不进前列。
    """
    words = [w for w in re.split(r"[\s\-_]+", kw or "") if w]
    if len(words) < 2 or not words[0][:1].isupper():
        return None
    caps = [w for w in words if w[:1].isupper() or w.isupper()]
    if len(caps) < 3:
        return None
    abbr = "".join(w[0].upper() for w in caps[1:])
    if len(abbr) < 3:
        return None
    return f"{caps[0]} {abbr}"


def _expand_keywords(keywords: list[str]) -> list[str]:
    """检索词扩展：原词 + 拆形词 + 缩写词 + 限定词缩写（去重保序）"""
    out = [k for k in keywords if isinstance(k, str) and k.strip()]
    seen = {k.lower() for k in out}
    for kw in list(out):
        for cand in (_split_compound(kw), _acronym(kw), _qualified_acronym(kw)):
            if cand and cand.lower() not in seen:
                seen.add(cand.lower())
                out.append(cand)
    return out


def rewrite_for_quick(query: str) -> dict | None:
    """quick 通道专用：把问句改写为英文检索关键词

    复用 paragraph 提示词与 qa_router agent，只取 keywords（不接受年份/来源过滤，
    保持 quick 只走库内词法检索的原语义）。命中缓存直接返回。
    首次输出若命中复读护栏 → 带纠偏提示重试**一次**；仍复读或校验不通过返回 None，
    由调用方回退规则关键词（不写缓存，避免把坏结果固化）。
    输出里若出现「X Component/Module/Part」归类标签，另派生 `alt_keywords`（对象词+施事名词），
    供检索层做第二组召回并 RRF 融合（§39 续）。

    keywords 为**扩展后**的检索词（§43：原词 + 复合词拆形 + 缩写派生）；缓存里仍存模型原词，
    扩展每次从原词确定性重算，规则调整后不必等缓存过期。

    :return: {"keywords": [...], "alt_keywords": [...] | None, "rewritten": True}；失败返回 None
    """
    query = (query or "").strip()
    if not query:
        return None
    if query in _QUICK_CACHE:
        base = list(_QUICK_CACHE[query])
        out = {"keywords": _expand_keywords(base), "rewritten": True}
        alt = _derive_alt_keywords(base)
        if alt:
            out["alt_keywords"] = alt
        return out
    keywords = _quick_keywords(query, QUICK_PROMPT)
    if keywords and _is_prompt_echo(keywords, query):
        print("[rewrite] quick 改写疑似复读提示词示例，带纠偏提示重试一次")
        keywords = _quick_keywords(query, QUICK_PROMPT + _ECHO_RETRY_NOTE)
        if keywords and _is_prompt_echo(keywords, query):
            print("[rewrite] quick 改写重试后仍复读示例，交调用方回退正则关键词")
            return None
    if not keywords:
        return None
    _QUICK_CACHE[query] = list(keywords)
    out = {"keywords": _expand_keywords(keywords), "rewritten": True}
    alt = _derive_alt_keywords(keywords)
    if alt:                                     # 无标签式关键词时不带该键（保持既有返回形状）
        out["alt_keywords"] = alt
    return out


# ---------- 确定性关键词抽取（phrase 模式 / LLM 失败兜底共用） ----------

_STOPWORDS = {
    "的", "了", "在", "是", "我", "有", "和", "就", "不", "人", "都", "一", "一个",
    "上", "也", "很", "到", "说", "要", "去", "你", "会", "着", "没有", "看", "好",
    "the", "a", "an", "of", "and", "or", "to", "in", "on", "for", "with", "is",
    "are", "was", "were", "how", "what", "which", "who", "when", "where", "why",
    "please", "me", "my", "最近", "最新", "关于", "请问", "帮我", "有哪些", "什么",
    "怎么", "如何", "哪些", "有没有", "目前",
}


def extract_keywords(text: str, limit: int = 6) -> list[str]:
    """规则抽取关键词：英文按词、中文按 2-gram 切片，去停用词"""
    words = re.findall(r"[a-zA-Z][a-zA-Z0-9_-]{1,}", text or "")
    cjk_spans = re.findall(r"[\u4e00-\u9fff]{2,}", text or "")
    tokens = [w.lower() for w in words if w.lower() not in _STOPWORDS]
    for span in cjk_spans:
        # 中文按 bigram 切片，长词保留完整形态（如"注意力机制"）
        if len(span) <= 4:
            if span not in _STOPWORDS:
                tokens.append(span)
        else:
            tokens.extend(span[i:i + 2] for i in range(len(span) - 1))
    # 去重保序 + 截断
    seen, out = set(), []
    for t in tokens:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out[:limit]
