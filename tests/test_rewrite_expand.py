# 用例：quick 检索词确定性扩展（§43：复合词拆形 / 缩写派生 / 限定词缩写）
#   全部为纯构词规则，不含领域词表；扩展词与原词一起进 keywords，词法路与向量路共用。
import pytest

from ai import rewrite as rewrite_mod


@pytest.fixture(autouse=True)
def _clear_cache():
    rewrite_mod._QUICK_CACHE.clear()
    yield
    rewrite_mod._QUICK_CACHE.clear()


@pytest.mark.parametrize("kw,expect", [
    ("GraphRAG", "Graph RAG"),                     # 驼峰复合词拆成两词
    ("GraphRAG method", "Graph RAG method"),
    ("Retrieval-Augmented Generation", None),      # 连字符不拆（分词器已能切开）
    ("knowledge graph", None),                     # 本来就是空格分词
    ("retriever", None),
])
def test_split_compound(kw, expect):
    assert rewrite_mod._split_compound(kw) == expect


@pytest.mark.parametrize("kw,expect", [
    ("Retrieval-Augmented Generation", "RAG"),
    ("standard Retrieval-Augmented Generation", "standard RAG"),   # Q3 对照文献的字面词
    ("Large Language Models", "LLM"),
    ("Knowledge Graph", None),      # 两字母缩写属噪声，不派生
    ("Dense Retrieval", None),
    ("retriever", None),            # 单词没有缩写
])
def test_acronym(kw, expect):
    assert rewrite_mod._acronym(kw) == expect


@pytest.mark.parametrize("kw,expect", [
    ("Graph Retrieval-Augmented Generation", "Graph RAG"),   # 限定词 + 缩写
    ("standard Retrieval-Augmented Generation", None),       # 首词小写走 _acronym
    ("External Knowledge Base", None),                       # 缩写仅两字母
    ("Large Language Models", None),                         # 去掉限定词后只剩 LM
])
def test_qualified_acronym(kw, expect):
    assert rewrite_mod._qualified_acronym(kw) == expect


def test_expand_keywords_appends_variants():
    """原词不动，扩展词追加在后；Q3 实测词集 → Graph RAG + standard RAG"""
    kws = ["GraphRAG", "standard Retrieval-Augmented Generation",
           "knowledge graph", "community summarization"]
    assert rewrite_mod._expand_keywords(kws) == kws + ["Graph RAG", "standard RAG"]


def test_expand_keywords_dedupes_case_insensitively():
    """已有 RAG 时不再追加 rag；大小写不同视为同一个词"""
    assert rewrite_mod._expand_keywords(["RAG", "Retrieval-Augmented Generation"]) == \
        ["RAG", "Retrieval-Augmented Generation"]


def test_expand_keywords_leaves_plain_terms_unchanged():
    """无复合词/大写的普通词集原样返回（Q2 的 taxonomy 形态）"""
    kws = ["retrieval-augmented generation", "retriever", "generator", "taxonomy"]
    assert rewrite_mod._expand_keywords(kws) == kws
