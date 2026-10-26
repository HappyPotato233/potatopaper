# rerank 抽象层：默认 BM25Okapi（rank_bm25）本地重排（对齐详细设计 §4.5）
# 分层定位：MySQL FULLTEXT 负责召回（类 BM25 命中），本层负责结果集跨源统一排序；
# 接口抽象，未来可无缝升级 embedding 重排（仅替换 default_reranker）。
import re
from abc import ABC, abstractmethod

from rank_bm25 import BM25Okapi


def tokenize(text: str) -> list[str]:
    """中英混合分词：英文按词、中文按单字（ngram 思路，无需外部分词依赖）"""
    if not text:
        return []
    tokens = re.findall(r"[a-zA-Z0-9]+", text.lower())
    cjk = re.findall(r"[\u4e00-\u9fff]", text)
    return tokens + cjk


class Reranker(ABC):
    """重排器抽象接口：rerank(query, results) -> 按相关度降序的结果列表"""

    @abstractmethod
    def rerank(self, query: str, results: list[dict]) -> list[dict]:
        ...


class BM25Reranker(Reranker):
    """默认实现：对 title+abstract 拼接文本做 BM25Okapi 打分"""

    def rerank(self, query: str, results: list[dict]) -> list[dict]:
        if not results:
            return []
        corpus = [tokenize(f"{r.get('title', '')} {r.get('abstract', '')}") for r in results]
        # BM25Okapi 要求语料非空且分词后非空；空文档以占位符兜底
        corpus = [c if c else [""] for c in corpus]
        bm25 = BM25Okapi(corpus)
        scores = bm25.get_scores(tokenize(query))
        ranked = sorted(zip(results, scores), key=lambda x: -x[1])
        return [r for r, _ in ranked]


# 默认重排器（升级 embedding 时仅需替换此实例，业务层零改动）
default_reranker = BM25Reranker()


def rerank(query: str, results: list[dict]) -> list[dict]:
    """统一入口：异常时降级为原始召回顺序，不阻断主流程（失败回退原则）"""
    try:
        return default_reranker.rerank(query, results)
    except Exception as e:
        print(f"[rerank] 重排失败，降级为原始顺序: {e}")
        return results


# ---------- 多路融合（RRF：稠密 + 稀疏双路召回的统一排序） ----------

def rrf_fuse(rank_lists: list[list[str]], k: int = 60,
             weights: list[float] | None = None) -> list[str]:
    """倒数排名融合：多路名次 → 单一排序（确定性，零 LLM）

    score(d) = Σ_r w_r / (k + rank_r(d))；只按「名次」融合，不做分数量纲归一化
    （各路分数量纲不可比：BM25 是 unbounded、余弦是 [-1,1]）
    :param rank_lists: 各路按相关度降序的 id 列表（名次从 1 起）
    :param k: 平滑常数（越大越弱化头部名次差异，经典取 60）
    :param weights: 各路权重，缺省等权
    """
    if not rank_lists:
        return []
    ws = weights if weights and len(weights) == len(rank_lists) else [1.0] * len(rank_lists)
    scores: dict[str, float] = {}
    order: list[str] = []          # 首次出现顺序：兜住同分时的稳定排序
    for lst, w in zip(rank_lists, ws):
        for rank, pid in enumerate(lst, start=1):
            pid = str(pid)
            if pid not in scores:
                scores[pid] = 0.0
                order.append(pid)
            scores[pid] += w / (k + rank)
    return sorted(order, key=lambda p: -scores[p])
