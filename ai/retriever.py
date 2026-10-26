# 检索 Agent（retriever）：三路检索 + 证据汇总（对齐 AI 方案 §2.4 / §3.1）
#   library：双路召回（词法 FULLTEXT/LIKE + 向量 bge-large-en-v1.5）→ RRF 融合 → 元数据增强
#   web：外部数据源适配器实时拉取（限速/退避）
#   memory：历史会话记忆（关键词重叠打分）
# 定位：确定性检索无 LLM；证据保留原文引用、不加料
import json
import os
import re
import sys
from datetime import datetime, timedelta

from ai import rerank as rerank_mod
from ai import vector_recall as vector_mod
from ai.meta_enhance import enhance_meta
from ai.rewrite import extract_keywords
from app.session import engine
from sources import resolve_source
from sources.base import normalize_title

# 各路证据上限（token 边界控制，AI 方案 §2.3）
LIBRARY_TOP_K = 8
WEB_TOP_K = 6
MEMORY_TOP_K = 3
ABSTRACT_SNIPPET = 400  # 证据切片摘要截断长度
# 召回候选池：与 top_k 解耦（大池召回 → 各路排名 → RRF 融合 → top_k）
# 只截候选池上限、不按 FULLTEXT 名次预截断，避免中段相关文献在融合前就被丢掉
LIBRARY_CANDIDATES = 500
VECTOR_CANDIDATES = 500  # 向量路候选范围上限（同一放宽链 + 同一用户隔离）


# 双路召回开关与 RRF 参数（评测归因用环境变量，缺省开启）
def _vector_recall_enabled() -> bool:
    """向量路开关：置 0 可退回单路词法召回做对照"""
    return os.getenv("LITRADAR_VECTOR_RECALL", "1") != "0"


def _rrf_k() -> int:
    """RRF 平滑常数 k（越大越弱化头部名次差异）

    缺省 10：本语料实测对照 k=60 → 15/23、k=10 → 16/23（多补回 Q10 期望文献 #4），
    故取 10；可用 LITRADAR_RRF_K 覆盖
    """
    try:
        return int(os.getenv("LITRADAR_RRF_K", "10"))
    except ValueError:
        return 10


# ---------- ① 文献库检索（双路召回 → RRF 融合 → 元数据增强） ----------

def search_library(query_struct: dict, top_k: int = LIBRARY_TOP_K,
                   user_id: int | None = None,
                   vector_weight: float = 1.0) -> list[dict]:
    """库内检索：词法路（FULLTEXT/LIKE）+ 向量路（本地 bge）双路召回 → RRF 融合

    :param user_id: 传入则按用户订阅做隔离（工具层 library_search 权限校验，编排方案 v4 §3）
    :param vector_weight: 向量路在 RRF 里的权重（词法路固定 1）；缺省 1.0 即等权
    """
    ranked = _rank_keywords(query_struct, top_k, user_id, vector_weight)
    # 第二组关键词（quick 通道施事名词派生词集）再召回一次 → 两组名次 RRF 融合（§39 续）
    alt = [k for k in (query_struct.get("alt_keywords") or []) if len(k) >= 2]
    if alt:
        ranked = _fuse_keyword_sets(ranked, _rank_keywords({**query_struct, "keywords": alt},
                                                           top_k, user_id, vector_weight))

    # 元数据增强（零 LLM）→ 统一证据结构
    ranked = enhance_meta(ranked[:top_k])
    return [_to_evidence(r, origin="library") for r in ranked]


def _rank_keywords(query_struct: dict, top_k: int, user_id: int | None,
                   vector_weight: float = 1.0) -> list[dict]:
    """单组关键词的双路召回 + 融合（原 search_library 主体，按关键词集可重复调用）"""
    keywords = [k for k in (query_struct.get("keywords") or []) if len(k) >= 2]
    # 词法路查询优先取本轮关键词：会话级 query（phrase 模式下即原始中文问句）与全英文语料几乎无交集，
    # 直接用于 BM25 会让排序与检索词无关、top_k 退化为固定集合，把已召回的期望文献挤出
    text = " ".join(keywords) or query_struct.get("query") or ""
    filters = query_struct.get("filters") or {}

    # 路1 词法：零命中逐级放宽过滤（原 filters → 去年份 → 再去来源），user_id 权限隔离永不放宽
    rows_lex = _search_relaxed(text, keywords, filters, top_k, user_id=user_id)
    ranked_lex = rerank_mod.rerank(text, rows_lex)     # 词法路名次（BM25）

    # 路2 向量 + RRF 融合（模型不可用/零候选/异常 → 直接回落词法路）
    return _fuse_vector(query_struct, filters, ranked_lex, top_k, user_id=user_id,
                        vector_weight=vector_weight)


def _fuse_keyword_sets(ranked_a: list[dict], ranked_b: list[dict]) -> list[dict]:
    """多关键词集 RRF 融合：同一问句两组改写词各召回一次，按名次融合（§39 续）

    只做名次融合、不再加权：两组词分别覆盖「对象/分类」与「具体部件」两种表述，
    融合后两边的头部文献都能保留（单组关键词各自会丢掉对方命中的期望文献）。
    """
    pool: dict[str, dict] = {}
    for row in [*ranked_a, *ranked_b]:
        pool.setdefault(str(row["id"]), row)
    fused = rerank_mod.rrf_fuse([[str(r["id"]) for r in ranked_a],
                                 [str(r["id"]) for r in ranked_b]], k=_rrf_k())
    return [pool[i] for i in fused if i in pool]


def _fuse_vector(query_struct: dict, filters: dict, ranked_lex: list[dict], top_k: int,
                 user_id: int | None, vector_weight: float = 1.0) -> list[dict]:
    """向量路召回 + RRF 融合：两路名次融合后返回；vector_weight 放大向量路名次贡献"""
    if not _vector_recall_enabled() or not vector_mod.available():
        return ranked_lex[:top_k]
    rows_dense = _scope_rows(filters, user_id=user_id)
    ranked_dense = vector_mod.rank(_dense_query(query_struct), rows_dense)
    if not ranked_dense:
        return ranked_lex[:top_k]
    pool: dict[str, dict] = {}
    for row in [*ranked_lex, *rows_dense]:
        pool.setdefault(str(row["id"]), row)
    fused = rerank_mod.rrf_fuse([[str(r["id"]) for r in ranked_lex],
                                 [str(r["id"]) for r in ranked_dense]], k=_rrf_k(),
                                weights=[1.0, float(vector_weight)])
    return [pool[i] for i in fused if i in pool]


def _dense_query(query_struct: dict) -> str:
    """向量路查询文本：英文检索词优先，否则退回会话原始问句

    语料为英文、编码器为 bge-large-en：中文问句直接编码会显著退化，
    故本轮关键词里只要有英文词就优先用关键词串（planner 轮的英文改写词正合此用途）。
    """
    kws = [k for k in (query_struct.get("keywords") or []) if len(k) >= 2]
    ascii_kws = [k for k in kws if k.isascii() and any(c.isalpha() for c in k)]
    return " ".join(ascii_kws) or (query_struct.get("query") or "")


def _filter_chain(filters: dict) -> list[dict]:
    """逐级放宽的过滤链：原 filters → 去年份 → 再去来源

    user_id 由调用方固定传入，不在此链中放宽（权限隔离硬约束）
    """
    chain = [dict(filters)]
    cur = chain[-1]
    if cur.get("year_from") or cur.get("year_to"):
        cur = {**cur, "year_from": "", "year_to": ""}
        chain.append(cur)
    if cur.get("sources"):
        chain.append({**cur, "sources": []})
    return chain


def _search_relaxed(text: str, keywords: list[str], filters: dict, top_k: int,
                    user_id: int | None) -> list[dict]:
    """按放宽链逐级召回：每级 FULLTEXT →（零命中）LIKE 兜底；任一级有结果即返回"""
    limit = max(LIBRARY_CANDIDATES, top_k * 3)
    for f in _filter_chain(filters):
        rows = _fulltext_search(text, keywords, f, limit=limit, user_id=user_id)
        if not rows:
            rows = _like_search(text, keywords, f, limit=limit, user_id=user_id)
        if rows:
            return rows
    return []


def _scope_rows(filters: dict, user_id: int | None) -> list[dict]:
    """向量路候选范围：与词法路同一放宽链、同一用户隔离，仅去掉关键词命中条件"""
    for f in _filter_chain(filters):
        rows = _scope_query(f, user_id=user_id)
        if rows:
            return rows
    return []


def _scope_query(filters: dict, user_id: int | None,
                 limit: int = VECTOR_CANDIDATES) -> list[dict]:
    """按元数据过滤取候选行（不参与关键词打分）：向量路候选池的确定性来源"""
    where, params = ["1=1"], []
    _apply_filters(where, params, filters, user_id=user_id)
    # 与 FULLTEXT/LIKE 一致保留原生 SQL，仅换执行通道
    sql = f"SELECT * FROM papers WHERE {' AND '.join(where)} ORDER BY id LIMIT {int(limit)}"
    with engine.connect() as conn:
        result = conn.exec_driver_sql(sql, tuple(params))
        return [dict(r) for r in result.mappings().all()]


def _fulltext_search(text: str, keywords: list[str], filters: dict, limit: int,
                     user_id: int | None = None) -> list[dict]:
    """FULLTEXT 自然语言模式召回（类 BM25 打分，负责召回层）"""
    against = " ".join(keywords) if keywords else text
    if not against.strip():
        return []
    where, params = ["MATCH(title, abstract) AGAINST (%s IN NATURAL LANGUAGE MODE)"], [against]
    _apply_filters(where, params, filters, user_id=user_id)
    # 全文检索保留原生 SQL（spec §6.4 允许），仅把执行通道换成 SQLAlchemy 连接
    sql = f"""SELECT * FROM papers WHERE {' AND '.join(where)}
              ORDER BY MATCH(title, abstract) AGAINST (%s IN NATURAL LANGUAGE MODE) DESC
              LIMIT {int(limit)}"""
    with engine.connect() as conn:
        result = conn.exec_driver_sql(sql, tuple(params + [against]))
        return [dict(r) for r in result.mappings().all()]


def _like_search(text: str, keywords: list[str], filters: dict, limit: int,
                 user_id: int | None = None) -> list[dict]:
    """LIKE 模糊兜底：任一关键词命中 title 或 abstract"""
    kws = keywords or extract_keywords(text)
    if not kws:
        return []
    conds = " OR ".join(["title LIKE %s OR abstract LIKE %s"] * len(kws))
    params = [p for k in kws for p in (f"%{k}%", f"%{k}%")]
    where = [f"({conds})"]
    _apply_filters(where, params, filters, user_id=user_id)
    # LIKE 兜底同样保留原生 SQL，仅换执行通道
    sql = f"""SELECT * FROM papers WHERE {' AND '.join(where)}
              ORDER BY published_at DESC LIMIT {int(limit)}"""
    with engine.connect() as conn:
        result = conn.exec_driver_sql(sql, tuple(params))
        return [dict(r) for r in result.mappings().all()]


def _apply_filters(where: list[str], params: list, filters: dict,
                   user_id: int | None = None) -> None:
    """元数据过滤（层1，确定性）：年份 / 来源 / 分类 / 用户隔离（对齐 AI 方案 §3.1）

    :param user_id: 传入则限定在该用户订阅关联的文献范围内（v4 工具层权限隔离）
    """
    if user_id:
        where.append(
            "id IN (SELECT sp.paper_id FROM subscription_papers sp"
            " JOIN subscriptions s ON s.id = sp.subscription_id WHERE s.user_id = %s)")
        params.append(user_id)
    if filters.get("year_from"):
        where.append("YEAR(published_at) >= %s")
        params.append(int(filters["year_from"]))
    if filters.get("year_to"):
        where.append("YEAR(published_at) <= %s")
        params.append(int(filters["year_to"]))
    if filters.get("sources"):
        srcs = [s for s in filters["sources"] if s]
        if srcs:
            where.append("source IN (%s)" % ",".join(["%s"] * len(srcs)))
            params.extend(srcs)


# ---------- ② 实时网络检索（外部数据源实时拉取） ----------

# web 关键词形态护栏（§37）：适配器以 all:"<关键词>" 精确短语匹配，长中英混排串 /
# 携带来源与年份噪声的短语必然 0 命中，故送入适配器前统一压成短英文术语
WEB_KEYWORD_MAX_WORDS = 3   # 单词条词数上限，超出即拆成相邻二元组
WEB_KEYWORD_MAX = 6         # 清洗后关键词条数上限（约束检索式长度）
_WEB_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_\-]*")   # 只取英文词（连字符保持整词）
_WEB_YEAR_RE = re.compile(r"^(19|20)\d{2}$")
_WEB_NOISE_WORDS = {"arxiv", "semantic", "scholar", "wanfang", "journal", "preprint",
                    "conference", "proceedings", "paper", "papers", "article",
                    "publication", "venue"}


def sanitize_web_keywords(keywords: list[str]) -> list[str]:
    """网络检索关键词清洗：剔噪 + 长短语拆二元组 + 去重 + 截断（纯确定性，无 LLM）

    Planner 送来的关键词常为长中英混排短语且含 "arXiv" / 年份 / 期刊类型词，
    直接进 arXiv 检索式会因精确短语匹配而恒 0 命中；此处按「短英文术语」降形。
    """
    out, seen = [], set()
    for kw in keywords or []:
        if not isinstance(kw, str):
            continue
        for term in _web_terms(_web_content_words(kw)):
            key = term.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(term)
            if len(out) >= WEB_KEYWORD_MAX:
                return out
    return out


def _web_content_words(kw: str) -> list[str]:
    """切词并剔噪：丢掉来源 / 期刊类型词、纯年份与无 ASCII 字母的 token（中文对英文语料无效）"""
    return [w for w in _WEB_WORD_RE.findall(kw)
            if w.lower() not in _WEB_NOISE_WORDS and not _WEB_YEAR_RE.match(w)]


def _web_terms(words: list[str]) -> list[str]:
    """降形：≤3 词原样保留；>3 词拆成相邻二元组（真实搭配才有命中可能）"""
    if len(words) <= WEB_KEYWORD_MAX_WORDS:
        return [" ".join(words)] if words else []
    return [" ".join(words[i:i + 2]) for i in range(len(words) - 1)]


def search_web(query_struct: dict, top_k: int = WEB_TOP_K) -> list[dict]:
    """实时拉取外部数据源（arXiv / Semantic Scholar；万方无 API 优雅降级跳过）"""
    keywords = sanitize_web_keywords(
        query_struct.get("keywords") or extract_keywords(query_struct.get("query", "")))
    if not keywords:
        return []
    since = datetime.now() - timedelta(days=365)  # 实时路默认 1 年窗口
    evidence: list[dict] = []
    per_source = max(2, top_k // 2)
    for label in ("arXiv", "Semantic Scholar"):
        adapter = resolve_source(label)
        if adapter is None:
            continue
        try:
            records = adapter.fetch(keywords, since, max_results=per_source)
            evidence.extend(_to_evidence({
                "id": r.external_id, "external_id": r.external_id, "source": r.source,
                "title": r.title, "authors": json.dumps(r.authors, ensure_ascii=False),
                "abstract": r.abstract, "journal": r.journal,
                "published_at": r.published_at, "url": r.url,
                "raw_meta": json.dumps(r.raw_meta, ensure_ascii=False),
            }, origin="web") for r in records)
        except Exception as e:
            # 走 stderr：本模块也会在 MCP stdio 子进程里执行，stdout 是 JSON-RPC 通道，
            # 往 stdout 打印会把协议报文冲掉（客户端解析失败 → 无谓降级回进程内实现）
            print(f"[retriever] 实时检索 {label} 失败: {e}", file=sys.stderr)
    return evidence[:top_k]


# ---------- ③ 记忆检索（历史会话上下文） ----------

def search_memory(memory_records: list[dict], query: str,
                  keywords: list[str] | None = None, top_k: int = MEMORY_TOP_K) -> list[dict]:
    """从用户历史会话中按关键词重叠度召回相关问答（无 LLM，确定性打分）"""
    if not memory_records:
        return []
    kws = set(keywords or extract_keywords(query))
    if not kws:
        return []
    scored = []
    for rec in memory_records:
        text = f"{rec.get('query', '')} {' '.join(rec.get('points') or [])} {rec.get('answer', '')}"
        hits = sum(1 for k in kws if k in text)
        if hits > 0:
            scored.append((hits, rec))
    scored.sort(key=lambda x: -x[0])
    return [{
        "id": f"memory-{rec.get('session_id', '')}",
        "title": rec.get("query", "")[:80],
        "abstract": (rec.get("answer") or "")[:ABSTRACT_SNIPPET],
        "source": "会话记忆", "url": "", "published_at": "",
        "authors": [], "origin": "memory",
        "score": hits / max(len(kws), 1),
    } for hits, rec in scored[:top_k]]


# ---------- 证据汇总（去重 + 排序，对齐 §2.4「三路证据汇总」） ----------

def merge_evidence(*evidence_lists: list[dict]) -> list[dict]:
    """跨路去重（标题归一化）+ 保序合并：library → web → memory"""
    seen: set[str] = set()
    merged: list[dict] = []
    for lst in evidence_lists:
        for ev in lst:
            key = normalize_title(ev.get("title", ""))
            if not key or key in seen:
                continue
            seen.add(key)
            merged.append(ev)
    return merged


# ---------- 工具 ----------

def _to_evidence(row: dict, origin: str) -> dict:
    """papers 行 / 适配器记录 → 统一证据结构（保留原文引用，切片截断控制 token）"""
    authors = row.get("authors")
    if isinstance(authors, str):
        try:
            authors = json.loads(authors)
        except json.JSONDecodeError:
            authors = []
    abstract = row.get("abstract") or ""
    published = row.get("published_at")
    if isinstance(published, datetime):
        published = published.strftime("%Y-%m-%d")
    return {
        "id": str(row.get("id") or row.get("external_id") or ""),
        "external_id": row.get("external_id") or "",
        "title": row.get("title") or "",
        "abstract": abstract[:ABSTRACT_SNIPPET],
        "journal": row.get("journal") or "",
        "source": row.get("source") or "",
        "url": row.get("url") or "",
        "published_at": str(published or ""),
        "authors": authors if isinstance(authors, list) else [],
        "origin": origin,
        "journal_level": row.get("journal_level", ""),
        "ccf": row.get("ccf", ""),
        "cas_zone": row.get("cas_zone", ""),
    }
