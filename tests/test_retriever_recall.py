# 用例：检索层召回缺陷修复（P1 放宽回退链 / P3c LIKE 全文条件 / 双路召回 RRF 融合）
# 纯单元测试：monkeypatch 检索原语、rerank 与向量路，不依赖数据库
import pytest

from ai import retriever


def _row(pid, title="t", abstract="a"):
    return {"id": pid, "title": title, "abstract": abstract}


def _mock_lexical(monkeypatch, rows):
    """词法路桩：FULLTEXT 直接命中，跳过 LIKE 与精排"""
    monkeypatch.setattr(retriever, "_fulltext_search", lambda *a, **k: list(rows))
    monkeypatch.setattr(retriever, "_like_search", lambda *a, **k: [])
    monkeypatch.setattr(retriever.rerank_mod, "rerank", lambda text, r: r)
    monkeypatch.setattr(retriever, "enhance_meta", lambda r: r)


def _mock_vector(monkeypatch, available, scope_rows, ranked_ids):
    """向量路桩：available/候选范围/向量名次全为可注入"""
    monkeypatch.setattr(retriever.vector_mod, "available", lambda: available)
    monkeypatch.setattr(retriever, "_scope_rows", scope_rows)
    monkeypatch.setattr(retriever.vector_mod, "rank",
                        lambda query, rows: [r for pid in ranked_ids
                                             for r in rows if str(r["id"]) == pid])


def test_library_search_relax_year_then_sources(monkeypatch):
    """P1：原 filters 零命中 → 去年份 → 再去来源逐级放宽；user_id 永不放宽"""
    monkeypatch.setenv("LITRADAR_VECTOR_RECALL", "0")   # 本用例只验词法放宽链，关掉向量路避免触库
    seen = []

    def fake_fulltext(text, keywords, filters, limit, user_id=None):
        seen.append((dict(filters), user_id))
        # 仅当去年份且去来源时命中（复现「年份过滤在 2026 语料上等于自杀」）
        if not filters.get("year_from") and not filters.get("year_to") \
                and not filters.get("sources"):
            return [_row(33)]
        return []

    monkeypatch.setattr(retriever, "_fulltext_search", fake_fulltext)
    monkeypatch.setattr(retriever, "_like_search", lambda *a, **k: [])
    monkeypatch.setattr(retriever.rerank_mod, "rerank", lambda text, rows: rows)
    monkeypatch.setattr(retriever, "enhance_meta", lambda rows: rows)

    out = retriever.search_library(
        {"query": "GraphRAG", "keywords": ["GraphRAG"],
         "filters": {"year_from": "2023", "year_to": "2025", "sources": ["arxiv"]}},
        user_id=1)

    # 三次 FULLTEXT 尝试：原 filters → 去年份 → 去年份 + 去来源
    assert [f["year_from"] for f, _ in seen] == ["2023", "", ""]
    assert [f["sources"] for f, _ in seen] == [["arxiv"], ["arxiv"], []]
    # user_id 权限隔离永不放宽：每次调用都带 user_id=1
    assert [uid for _, uid in seen] == [1, 1, 1]
    # 末级放宽后命中 #33
    assert [e["id"] for e in out] == ["33"]


class _Res:
    def mappings(self):
        return self

    def all(self):
        return []


class _Conn:
    def __init__(self, sqls):
        self._sqls = sqls

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def exec_driver_sql(self, sql, params):
        self._sqls.append((sql, list(params)))
        return _Res()


class _Engine:
    def __init__(self, sqls):
        self._sqls = sqls

    def connect(self):
        return _Conn(self._sqls)


def test_like_search_matches_title_or_abstract(monkeypatch):
    """P3c：LIKE 兜底条件须同时覆盖 title 与 abstract（原实现只落 title）"""
    sqls = []
    monkeypatch.setattr(retriever, "engine", _Engine(sqls))

    retriever._like_search("rag", ["rag"], {}, limit=5, user_id=None)

    sql, params = sqls[0]
    assert "title LIKE %s OR abstract LIKE %s" in sql
    assert sql.count("LIKE %s") == 2          # 标题 + 摘要各一个占位符
    assert params.count("%rag%") == 2


# ---------- 双路召回 + RRF 融合 ----------

def test_library_search_dual_recall_rrf(monkeypatch):
    """双路召回：只被向量路排到首位的文献能进 top_k（纯词法单路的 top_k 里没有它）"""
    monkeypatch.setenv("LITRADAR_VECTOR_RECALL", "1")
    _mock_lexical(monkeypatch, [_row(i) for i in (1, 2, 3, 4, 5)])
    _mock_vector(monkeypatch, True, lambda filters, user_id: [_row(1), _row(2), _row(9)],
                 ["9", "1", "2"])

    out = retriever.search_library({"query": "graphrag", "keywords": ["graphrag"]},
                                   top_k=3, user_id=1)

    ids = [e["id"] for e in out]
    assert ids[:2] == ["1", "2"]   # 两路都靠前的仍居首
    assert "9" in ids              # 仅向量路命中的文献被融合捞回


def test_library_search_forwards_vector_weight(monkeypatch):
    """§42：vector_weight 透传到 RRF（quick 传 5.0；不传保持等权 1.0）"""
    seen: list[list[float]] = []

    def fake_fuse(lists, k=60, weights=None):
        seen.append(weights)
        return list(lists[0])

    monkeypatch.setenv("LITRADAR_VECTOR_RECALL", "1")
    _mock_lexical(monkeypatch, [_row(1), _row(2)])
    _mock_vector(monkeypatch, True, lambda filters, user_id: [_row(2), _row(1)], ["2", "1"])
    monkeypatch.setattr(retriever.rerank_mod, "rrf_fuse", fake_fuse)

    retriever.search_library({"keywords": ["rag"]}, top_k=2, user_id=1)
    retriever.search_library({"keywords": ["rag"]}, top_k=12, user_id=1,
                             vector_weight=5.0)

    assert seen == [[1.0, 1.0], [1.0, 5.0]]   # 缺省等权 → quick 放大向量路


def test_library_search_falls_back_to_lexical_when_vector_unavailable(monkeypatch):
    """向量路不可用（模型缺失 / 加载失败）→ 结果与单路词法一致，且不触达向量候选范围"""
    monkeypatch.setenv("LITRADAR_VECTOR_RECALL", "1")
    _mock_lexical(monkeypatch, [_row(1), _row(2), _row(3)])
    _mock_vector(monkeypatch, False,
                 lambda filters, user_id: pytest.fail("不可用时不查向量候选"), ["9"])

    out = retriever.search_library({"query": "graphrag", "keywords": ["graphrag"]},
                                   top_k=2, user_id=1)

    assert [e["id"] for e in out] == ["1", "2"]


def test_library_search_vector_switch_off(monkeypatch):
    """开关置 0（评测对照用）→ 纯单路词法，不查向量候选"""
    monkeypatch.setenv("LITRADAR_VECTOR_RECALL", "0")
    _mock_lexical(monkeypatch, [_row(1), _row(2)])
    _mock_vector(monkeypatch, True,
                 lambda filters, user_id: pytest.fail("开关关闭时不查向量候选"), ["9"])

    out = retriever.search_library({"keywords": ["graphrag"]}, top_k=2, user_id=1)

    assert [e["id"] for e in out] == ["1", "2"]


def test_scope_query_applies_filters_and_user_isolation(monkeypatch):
    """向量路候选 SQL：年份过滤 + 用户隔离子查询都在，且不含关键词命中条件"""
    sqls = []
    monkeypatch.setattr(retriever, "engine", _Engine(sqls))

    retriever._scope_query({"year_from": "2023"}, user_id=7, limit=50)

    sql, params = sqls[0]
    assert "s.user_id = %s" in sql and "subscriptions" in sql
    assert "YEAR(published_at) >= %s" in sql
    assert params == [7, 2023]
    assert "MATCH" not in sql and "LIKE" not in sql   # 候选范围不参与关键词打分


def test_dense_query_prefers_english_keywords():
    """英文语料 + bge-large-en：向量路查询优先取英文检索词"""
    assert retriever._dense_query({"query": "什么是 GraphRAG 方法？",
                                   "keywords": ["GraphRAG", "方法"]}) == "GraphRAG"


def test_dense_query_falls_back_to_raw_question():
    """关键词全为中文碎片（quick 正则产物）→ 退回会话原始问句"""
    assert retriever._dense_query({"query": "什么是 GraphRAG 方法？",
                                   "keywords": ["什么是", "方法"]}) == "什么是 GraphRAG 方法？"


# ---------- web 关键词清洗（§37：Planner 长串 → 短英文术语，救活 arXiv 精确短语匹配） ----------

def test_sanitize_web_keywords_strips_noise_and_years():
    """Q10 形态：剔掉 arXiv / 纯年份 / 期刊类型词，且单词条 ≤3 词"""
    raw = ["arXiv RAG evaluation synthetic queries real queries data scale "
           "task difficulty reliability 2025 2026 journal preprint"]
    out = retriever.sanitize_web_keywords(raw)

    assert out                                          # 保底非空
    assert len(out) <= retriever.WEB_KEYWORD_MAX
    joined = " ".join(out).lower()
    for noise in ("arxiv", "2025", "2026", "journal", "preprint"):
        assert noise not in joined
    assert all(len(t.split()) <= retriever.WEB_KEYWORD_MAX_WORDS for t in out)


def test_sanitize_web_keywords_splits_long_phrase_to_bigrams():
    """Q9 形态：>3 词长短语拆成相邻二元组（真实搭配才有命中可能）"""
    out = retriever.sanitize_web_keywords(["membership inference attack defense"])

    assert out == ["membership inference", "inference attack", "attack defense"]


def test_sanitize_web_keywords_keeps_clean_terms_verbatim():
    """干净短语（§36.2 直调对照用的形态）输入即输出，零回归"""
    assert retriever.sanitize_web_keywords(
        ["GraphRAG", "retrieval-augmented generation"]
    ) == ["GraphRAG", "retrieval-augmented generation"]


def test_sanitize_web_keywords_dedupes_and_caps():
    """大小写不敏感去重（保留首次出现形态）+ 条数上限"""
    raw = ["GraphRAG", "graphrag", "RAG", "RAG", "LLM", "agent", "memory", "review"]
    out = retriever.sanitize_web_keywords(raw)

    assert out == ["GraphRAG", "RAG", "LLM", "agent", "memory", "review"]


def test_sanitize_web_keywords_drops_non_ascii():
    """纯中文关键词对英文语料无效 → 清洗掉（不再拼成 0 命中长串）"""
    assert retriever.sanitize_web_keywords(["对抗攻击防御", "引用操纵"]) == []


def test_search_web_skips_adapters_when_keywords_empty(monkeypatch):
    """清洗后关键词为空 → 直接返回空，不触达任何数据源"""
    monkeypatch.setattr(retriever, "resolve_source", lambda label: pytest.fail("不应触达数据源"))

    assert retriever.search_web({"keywords": ["对抗攻击"], "query": "对抗攻击是什么"}) == []


def test_search_web_passes_sanitized_keywords_to_adapter(monkeypatch):
    """端到端挂钩：送进适配器的是清洗后关键词（长串已拆短、噪声已剔除）"""
    seen: list[list[str]] = []

    class _Adapter:
        def fetch(self, keywords, since, max_results=50):
            seen.append(list(keywords))
            return []

    monkeypatch.setattr(retriever, "resolve_source",
                        lambda label: _Adapter() if label == "arXiv" else None)

    assert retriever.search_web({"keywords": ["membership inference attack defense arXiv"]}) == []
    assert seen == [["membership inference", "inference attack", "attack defense"]]
