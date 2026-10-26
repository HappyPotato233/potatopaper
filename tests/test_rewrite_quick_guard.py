# 用例：quick 通道关键词「复读护栏」（§39：提示词示例被 8B 模型逐字复读 → 重试一次）
#   护栏条件：输出词全部落在旧示例集合（≥2 个）且问句本身未提到 rag / graphrag 等词
import pytest

from ai import rewrite as rewrite_mod


@pytest.fixture(autouse=True)
def _clear_cache():
    """每例前后清空模块级关键词缓存，避免跨例污染"""
    rewrite_mod._QUICK_CACHE.clear()
    yield
    rewrite_mod._QUICK_CACHE.clear()


def _patcher(monkeypatch, outputs: list):
    """按顺序回放 keywords 列表；记录每次收到的提示词"""
    seen: list[str] = []
    queue = list(outputs)

    def fake_chat_json(agent_key, prompt, **kw):
        seen.append(prompt)
        kws = queue.pop(0) if queue else []
        return {"keywords": kws, "year_from": "", "year_to": "", "sources": [], "cats": []}

    monkeypatch.setattr(rewrite_mod, "chat_json", fake_chat_json)
    return seen


_ECHO = ["Retrieval-Augmented Generation", "Large Language Models", "GraphRAG", "Knowledge Graph"]
_Q2 = "论文中检索增强生成有哪些核心组件？"


def test_echo_triggers_one_retry_then_uses_retry_keywords(monkeypatch):
    """首次复读示例 → 带纠偏提示重试一次，采用重试结果"""
    seen = _patcher(monkeypatch, [_ECHO, ["Retriever", "Vector Database", "Generator"]])

    out = rewrite_mod.rewrite_for_quick(_Q2)

    assert out == {"keywords": ["Retriever", "Vector Database", "Generator"], "rewritten": True}
    assert len(seen) == 2
    # 重试提示词 = 首次已格式化提示词 + 纠偏附加段（占位符已替换，故与首次前缀比对）
    assert "上一次输出" in seen[1] and seen[1].startswith(seen[0])


def test_echo_twice_returns_none_and_skips_cache(monkeypatch):
    """重试后仍复读 → 返回 None（交调用方回退正则关键词），且不写缓存"""
    seen = _patcher(monkeypatch, [_ECHO, _ECHO])

    assert rewrite_mod.rewrite_for_quick(_Q2) is None
    assert len(seen) == 2
    assert _Q2 not in rewrite_mod._QUICK_CACHE


def test_query_mentioning_graphrag_is_exempt(monkeypatch):
    """问句本身提到 GraphRAG（Q3 型）→ 不判复读，只调一次；输出按 §43 规则扩展"""
    seen = _patcher(monkeypatch, [_ECHO])

    out = rewrite_mod.rewrite_for_quick("什么是 GraphRAG 方法？")

    # 扩展：Retrieval-Augmented Generation→RAG、Large Language Models→LLM、GraphRAG→Graph RAG
    assert out == {"keywords": _ECHO + ["RAG", "LLM", "Graph RAG"], "rewritten": True}
    assert len(seen) == 1


def test_normal_keywords_do_not_trigger_retry(monkeypatch):
    """正常输出（不含示例集）→ 只调一次；全称补一条缩写词 RAG（§43）"""
    kws = ["Retrieval-Augmented Generation", "Retriever", "Generator"]
    seen = _patcher(monkeypatch, [kws])

    out = rewrite_mod.rewrite_for_quick(_Q2)

    assert out == {"keywords": kws + ["RAG"], "rewritten": True}
    assert len(seen) == 1


def test_quick_prompt_carries_no_domain_example_terms():
    """提示词形态守卫：不得再出现可被复读的具体领域术语"""
    for term in ("GraphRAG", "Retrieval-Augmented", "Knowledge Graph", "Large Language Models"):
        assert term not in rewrite_mod.QUICK_PROMPT
    assert "禁止把本提示词中的任何字句当作关键词" in rewrite_mod.QUICK_PROMPT


def test_parse_failure_retries_once(monkeypatch):
    """§43：返回了内容但解析不出 keywords → 同一提示词再试一次"""
    calls: list[str] = []

    def fake_chat_json(agent_key, prompt, **kw):
        calls.append(prompt)
        return None if len(calls) == 1 else {"keywords": ["retriever", "taxonomy"]}

    monkeypatch.setattr(rewrite_mod, "chat_json", fake_chat_json)

    out = rewrite_mod.rewrite_for_quick(_Q2)

    assert len(calls) == 2 and calls[0] == calls[1]
    assert out == {"keywords": ["retriever", "taxonomy"], "rewritten": True}


def test_parse_failure_twice_returns_none(monkeypatch):
    """连续两次解析失败 → 返回 None 交调用方回退正则关键词，且不写缓存"""
    calls: list = []

    def fake_chat_json(agent_key, prompt, **kw):
        calls.append(prompt)
        return None

    monkeypatch.setattr(rewrite_mod, "chat_json", fake_chat_json)

    assert rewrite_mod.rewrite_for_quick(_Q2) is None
    assert len(calls) == 2
    assert _Q2 not in rewrite_mod._QUICK_CACHE


def test_llm_unavailable_does_not_retry(monkeypatch):
    """模型不可用（抛异常）不重试：重试也只会再失败一次，白等一轮超时"""
    calls: list = []

    def fake_chat_json(*a, **kw):
        calls.append(1)
        raise RuntimeError("upstream down")

    monkeypatch.setattr(rewrite_mod, "chat_json", fake_chat_json)

    assert rewrite_mod.rewrite_for_quick(_Q2) is None
    assert len(calls) == 1


def test_quick_keywords_use_temperature_zero(monkeypatch):
    """温度固定 0（§40）：正常调用与复读重试都必须显式传 temperature=0

    quick 关键词是确定映射；不传温度时同一问句会换词，Q1 因此在 0/2~2/2 之间抖。
    """
    calls: list[dict] = []

    def fake_chat_json(agent_key, prompt, **kw):
        calls.append(kw)
        return {"keywords": list(_ECHO)}

    monkeypatch.setattr(rewrite_mod, "chat_json", fake_chat_json)

    # 复读 → 会重试一次，两次调用都要带 temperature=0
    assert rewrite_mod.rewrite_for_quick(_Q2) is None
    assert len(calls) == 2
    assert all(kw.get("temperature") == 0 for kw in calls)
