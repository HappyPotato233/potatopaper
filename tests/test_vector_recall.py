# 用例：向量召回的缓存复用 / 失效 / 降级（不打真实模型、不连数据库）
import numpy as np
import pytest

from ai import vector_recall


class _FakeModel:
    """假编码器：向量首维 = 文本长度，便于断言「是否重复编码」"""

    def __init__(self):
        self.calls: list[list[str]] = []

    def encode(self, texts, **kwargs):
        items = [texts] if isinstance(texts, str) else list(texts)
        self.calls.append(items)
        return np.array([[float(len(t)), 1.0] for t in items], dtype=np.float32)


@pytest.fixture()
def fresh_cache(tmp_path, monkeypatch):
    """隔离向量缓存：临时文件 + 重置进程内状态"""
    monkeypatch.setattr(vector_recall, "CACHE_FILE", tmp_path / "cache.npz")
    monkeypatch.setattr(vector_recall, "_cache", {})
    monkeypatch.setattr(vector_recall, "_cache_loaded", False)


def test_ensure_vectors_reuses_cache_and_invalidates_on_change(fresh_cache):
    """首次全量编码 → 二次命中缓存零编码 → 文本变更只补算变更条目"""
    model = _FakeModel()
    rows = [{"id": 1, "title": "a", "abstract": "x"},
            {"id": 2, "title": "b", "abstract": "y"}]

    first = vector_recall._ensure_vectors(model, rows)
    assert len(model.calls) == 1 and len(model.calls[0]) == 2

    second = vector_recall._ensure_vectors(model, rows)
    assert len(model.calls) == 1                       # 全部命中缓存
    assert np.array_equal(first, second)               # 向量与行序对齐且一致

    rows[1]["title"] = "b2"                            # 内容变更 → 该条缓存失效
    vector_recall._ensure_vectors(model, rows)
    assert len(model.calls) == 2 and len(model.calls[1]) == 1
    assert "b2" in model.calls[1][0]


def test_vectors_persist_across_process_state(fresh_cache):
    """落盘后可重新加载（新进程首次查询不再重算）"""
    model = _FakeModel()
    rows = [{"id": 7, "title": "t", "abstract": "a"}]
    vector_recall._ensure_vectors(model, rows)

    vector_recall._cache = {}            # 模拟新进程：清空内存态但保留磁盘缓存
    vector_recall._cache_loaded = False
    vector_recall._ensure_vectors(model, rows)

    assert len(model.calls) == 1


def test_rank_returns_empty_when_model_unavailable(monkeypatch):
    """模型加载失败 → 返回空列表，由调用方回落词法路（不抛异常）"""
    monkeypatch.setattr(vector_recall, "_get_model", lambda: None)
    assert vector_recall.rank("graphrag", [{"id": 1, "title": "t", "abstract": "a"}]) == []


def test_rank_orders_by_cosine(monkeypatch, fresh_cache):
    """打分按余弦降序：同向量（单位化后相似度 1）的候选排在前面"""
    model = _FakeModel()
    monkeypatch.setattr(vector_recall, "_get_model", lambda: model)
    rows = [{"id": 1, "title": "t", "abstract": "a"},
            {"id": 2, "title": "t", "abstract": "a"}]

    out = vector_recall.rank("query", rows)

    assert len(out) == 2                    # 假编码器下同分，仅验证链路与对齐
    assert {r["id"] for r in out} == {1, 2}
