# 用例：quick 通道「施事名词派生 + 第二组关键词召回 + RRF 融合」（§39 续）
#   派生为纯英语构词规则（retrieval→retriever、generation→generator），不含领域词表；
#   第二组词集由检索层再召回一次并与首组名次融合，使「分类表述」与「具体部件名」两类命中都能保留。
import pytest

from ai import retriever as retr_mod
from ai import rewrite as rewrite_mod


@pytest.fixture(autouse=True)
def _clear_cache():
    rewrite_mod._QUICK_CACHE.clear()
    yield
    rewrite_mod._QUICK_CACHE.clear()


@pytest.mark.parametrize("base,expect", [
    ("retrieval", "retriever"),
    ("Retrieval", "retriever"),
    ("generation", "generator"),
    ("indexing", "indexer"),
    ("ranking", "ranker"),
    ("xyz", None),
    ("cat", None),
])
def test_agent_noun(base, expect):
    """施事名词派生规则：-al→-er、-ion→-or、-ing→-er；无法派生返回 None"""
    assert rewrite_mod._agent_noun(base) == expect


def test_derive_alt_keywords_from_label_style():
    """标签式输出 → 对象词 + 施事名词（Q2 实测输出）"""
    kws = ["Retrieval-Augmented Generation", "Retrieval Component", "Generation Component",
           "Information Retrieval", "Language Generation"]
    assert rewrite_mod._derive_alt_keywords(kws) == [
        "Retrieval-Augmented Generation", "retriever", "generator"]


def test_derive_alt_keywords_returns_none_without_label():
    """没有 X Component/Module/Part 形式 → 不派生第二组（Q1/Q3 不走双集召回）"""
    assert rewrite_mod._derive_alt_keywords(
        ["GraphRAG", "Graph-based", "Retrieval Augmented Generation"]) is None


def test_rewrite_for_quick_exposes_alt_keywords(monkeypatch):
    """rewrite_for_quick 输出 alt_keywords；复读护栏仍只调一次"""
    kws = ["Retrieval-Augmented Generation", "Retrieval Module", "Generation Module"]
    monkeypatch.setattr(rewrite_mod, "chat_json",
                        lambda *a, **kw: {"keywords": kws})
    out = rewrite_mod.rewrite_for_quick("论文中检索增强生成有哪些核心组件？")
    assert out["keywords"] == kws + ["RAG"]      # §43：全称另补一条缩写词
    assert out["alt_keywords"] == ["Retrieval-Augmented Generation", "retriever", "generator"]


def test_fuse_keyword_sets_keeps_both_heads():
    """两组名次融合后，两边的头部文献都保留，且同时出现在头的排前"""
    a = [{"id": "1"}, {"id": "2"}]
    b = [{"id": "3"}, {"id": "1"}]
    fused = retr_mod._fuse_keyword_sets(a, b)
    ids = [r["id"] for r in fused]
    assert ids[0] == "1"                 # 两路都靠前 → 融合后居首
    assert set(ids) == {"1", "2", "3"}


def test_search_library_runs_second_pass_only_with_alt(monkeypatch):
    """带 alt_keywords → 召回两次并融合；不带 → 只召回一次（原行为不变）"""
    calls = []

    def fake_rank(query_struct, top_k, user_id, vector_weight=1.0):
        calls.append(list(query_struct.get("keywords") or []))
        tag = (query_struct.get("keywords") or ["?"])[0]
        return [{"id": f"{tag}-1"}, {"id": f"{tag}-2"}]

    monkeypatch.setattr(retr_mod, "_rank_keywords", fake_rank)
    monkeypatch.setattr(retr_mod, "enhance_meta", lambda rows: rows)
    monkeypatch.setattr(retr_mod, "_to_evidence", lambda r, origin: r)

    out = retr_mod.search_library({"keywords": ["aa"], "alt_keywords": ["bb"]}, user_id=1)
    assert len(calls) == 2 and calls[1] == ["bb"]
    assert {r["id"] for r in out} == {"aa-1", "aa-2", "bb-1", "bb-2"}

    calls.clear()
    retr_mod.search_library({"keywords": ["aa"]}, user_id=1)
    assert len(calls) == 1
