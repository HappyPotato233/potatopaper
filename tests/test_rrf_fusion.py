# 用例：RRF 多路名次融合（稠密路 + 稀疏路统一排序，纯函数、零依赖）
from ai.rerank import rrf_fuse


def test_rrf_fuses_by_rank_across_paths():
    """两路名次累加：双路命中压过单路命中，融合结果保留两路文献"""
    lex = ["1", "2", "3", "4"]
    dense = ["3", "1", "2"]

    out = rrf_fuse([lex, dense], k=60)

    assert out[0] == "1"                      # 词法 1 + 向量 2 → 总分最高
    assert out.index("3") < out.index("4")    # 3 双路命中，4 只在词法路且名次靠后
    assert "4" in out                         # 单路文献不丢（只是降权）


def test_rrf_recovers_doc_pushed_out_by_lexical_pretruncation():
    """词法路排到 58 位的文献，只要向量路排第 2，就能挤进融合 top3（预截断丢文献的补救）"""
    lex = [str(i) for i in range(1, 61)]
    dense = ["7", "58"]

    out = rrf_fuse([lex, dense], k=60)

    assert "58" in out[:3]
    assert out.index("58") < out.index("10")   # 压过词法路靠前的文献


def test_rrf_weights_shift_priority():
    """权重可调：等权同分时先出现的路优先；给向量路加权后次序反转"""
    lex, dense = ["1"], ["2"]

    assert rrf_fuse([lex, dense])[0] == "1"
    assert rrf_fuse([lex, dense], weights=[0.1, 1.0])[0] == "2"


def test_rrf_edge_cases():
    """空路 → 空结果；单路 → 原序"""
    assert rrf_fuse([]) == []
    assert rrf_fuse([["9", "8"]]) == ["9", "8"]
