# 用例：Reviewer 复审清单按输入自适应（无分析师 → 只评证据类项，不评 ③逻辑一致性 / ④视角完整性）
# 纯单元测试：monkeypatch ai.reviewer.chat 截获 prompt 并回灌 JSON，不依赖数据库与真实 LLM
import pytest

from ai import reviewer


def _ev(pid, title="标题", abstract="摘要"):
    return {"id": pid, "title": title, "abstract": abstract}


def _capture(monkeypatch, payload):
    """替换 reviewer.chat：记录 prompt，返回固定 JSON 字符串"""
    box = {}

    def fake(agent_key, prompt, system=None, max_tokens=None):
        box["agent_key"] = agent_key
        box["prompt"] = prompt
        box["max_tokens"] = max_tokens
        return payload

    monkeypatch.setattr(reviewer, "chat", fake)
    return box


def test_evidence_only_template_when_no_analysts(monkeypatch):
    """无分析师输出 → 证据态模板：含硬性规则，且不列 ③④ 两项"""
    box = _capture(monkeypatch, '{"pass": true, "defect_list": [], "suggestion": ""}')

    note = reviewer.review("问题", [_ev(1)])

    prompt = box["prompt"]
    # 硬性规则：不得因「未触发多视角分析」判否
    assert "不得据此判 pass=false" in prompt
    # ③④ 两项不得出现在检查清单语境中（无输入可评）
    assert "逻辑一致性" not in prompt
    assert "视角完整性" not in prompt
    # 可评的三项仍在
    assert "幻觉校验" in prompt and "证据充足度" in prompt and "置信标记" in prompt
    assert note["passed"] is True


def test_full_template_when_analysts_present(monkeypatch):
    """有分析师输出 → 全量模板：5 项全查，且不出现「不得据此判否」规则"""
    box = _capture(monkeypatch, '{"pass": true, "defect_list": [], "suggestion": ""}')

    reviewer.review("问题", [_ev(1)], {"method": "方法抽取结论"})

    prompt = box["prompt"]
    assert "逻辑一致性" in prompt and "视角完整性" in prompt
    assert "方法抽取结论" in prompt
    assert "不得据此判 pass=false" not in prompt


def test_evidence_only_path_can_still_fail_on_insufficient_evidence(monkeypatch):
    """无分析师时仍可因证据不足判否：诚实降级不回退"""
    _capture(monkeypatch, '{"pass": false, "defect_list": ["证据不足，无法支撑结论"], "suggestion": "补充检索相关文献"}')

    note = reviewer.review("问题", [_ev(1)])

    assert note["passed"] is False
    assert note["defects"] == ["证据不足，无法支撑结论"]
    assert note["suggestions"] == ["补充检索相关文献"]


def test_analysts_present_boundaries():
    """_analysts_present：只有「有非空文本」才算有内容可评"""
    assert reviewer._analysts_present(None) is False
    assert reviewer._analysts_present({}) is False
    assert reviewer._analysts_present({"method": ""}) is False
    assert reviewer._analysts_present({"method": "   "}) is False
    assert reviewer._analysts_present({"method": "", "critic": "有结论"}) is True


def test_review_dto_shape_and_defect_truncation(monkeypatch):
    """返回结构不变（passed/defects/suggestions），defect_list 截断至 3 条"""
    _capture(monkeypatch, '{"pass": false, "defect_list": ["d1", "d2", "d3", "d4"], "suggestion": "s"}')

    note = reviewer.review("问题", [_ev(1)], {"method": "x"})

    assert set(note) == {"passed", "defects", "suggestions", "need_analysts"}
    assert note["need_analysts"] == []
    assert note["defects"] == ["d1", "d2", "d3"]
    assert note["suggestions"] == ["s"]


def test_evidence_only_template_offers_need_analysts(monkeypatch):
    """无分析师 → 证据态模板：提供 need_analysts 需求通道，且不列 ③④ 两项"""
    box = _capture(monkeypatch,
                   '{"pass": true, "defect_list": [], "suggestion": "", "need_analysts": ["critic", "compare"]}')

    note = reviewer.review("问题", [_ev(1)])

    prompt = box["prompt"]
    assert "need_analysts" in prompt
    assert "不得据此判 pass=false" in prompt
    # ③④ 两项仍不得出现在证据态模板中
    assert "逻辑一致性" not in prompt
    assert "视角完整性" not in prompt
    assert note["need_analysts"] == ["critic", "compare"]
    assert note["passed"] is True


def test_full_template_offers_need_analysts(monkeypatch):
    """有分析师 → 全量模板：也提供 need_analysts 需求通道，但无证据态硬性规则"""
    box = _capture(monkeypatch,
                   '{"pass": false, "defect_list": ["缺批判视角"], "suggestion": "补做批判审阅", "need_analysts": ["critic"]}')

    note = reviewer.review("问题", [_ev(1)], {"method": "方法抽取结论"})

    prompt = box["prompt"]
    assert "need_analysts" in prompt
    assert "不得据此判 pass=false" not in prompt
    assert note["need_analysts"] == ["critic"]
    assert note["passed"] is False


def test_draft_injected_into_evidence_only_prompt(monkeypatch):
    """草稿正文入参 → 进入证据态模板的草稿块；清单①口径改为对草稿核验引用真实性"""
    box = _capture(monkeypatch, '{"pass": true, "defect_list": [], "suggestion": ""}')

    reviewer.review("问题", [_ev(1)], draft="本文结论基于注意力机制[1]的实证。")

    prompt = box["prompt"]
    assert "待交付草稿" in prompt
    assert "本文结论基于注意力机制[1]的实证。" in prompt
    # ① 幻觉校验改为审草稿正文（而非「结论」）
    assert "草稿正文中引用的文献编号必须真实存在于下方证据" in prompt


def test_draft_injected_into_full_prompt(monkeypatch):
    """有分析师时同样注入草稿块（两套模板口径一致）"""
    box = _capture(monkeypatch, '{"pass": true, "defect_list": [], "suggestion": ""}')

    reviewer.review("问题", [_ev(1)], {"method": "方法抽取结论"}, draft="草稿正文[1]")

    prompt = box["prompt"]
    assert "待交付草稿" in prompt and "草稿正文[1]" in prompt
    assert "草稿正文中引用的文献编号必须真实存在于下方证据" in prompt


def test_draft_placeholder_when_absent(monkeypatch):
    """未传草稿 → 回填占位串，且不影响 pass 判定（LLM 输出为准）"""
    box = _capture(monkeypatch, '{"pass": true, "defect_list": [], "suggestion": ""}')

    note = reviewer.review("问题", [_ev(1)])

    assert "（本轮未产出草稿）" in box["prompt"]
    assert note["passed"] is True


def test_draft_blank_string_uses_placeholder(monkeypatch):
    """草稿为空串/空白 → 同样回填占位（不把空草稿当缺陷）"""
    box = _capture(monkeypatch, '{"pass": true, "defect_list": [], "suggestion": ""}')

    reviewer.review("问题", [_ev(1)], draft="   ")

    assert "（本轮未产出草稿）" in box["prompt"]


def test_draft_truncated_to_snippet_limit(monkeypatch):
    """草稿超长 → 切片至 DRAFT_SNIPPET（token 边界控制）"""
    box = _capture(monkeypatch, '{"pass": true, "defect_list": [], "suggestion": ""}')

    reviewer.review("问题", [_ev(1)], draft="甲" * (reviewer.DRAFT_SNIPPET + 50))

    assert "甲" * reviewer.DRAFT_SNIPPET in box["prompt"]
    assert "甲" * (reviewer.DRAFT_SNIPPET + 1) not in box["prompt"]


def test_clean_views_boundaries():
    """_clean_views：只认三个视角 key、去重保序"""
    assert reviewer._clean_views(None) == []
    assert reviewer._clean_views([]) == []
    assert reviewer._clean_views(["xxx"]) == []
    assert reviewer._clean_views(["critic", "critic", "method"]) == ["critic", "method"]
    assert reviewer._clean_views(["method", " compare "]) == ["method", "compare"]


def test_salvage_extracts_need_analysts_when_truncated(monkeypatch):
    """JSON 被截断 → _salvage 宽松提取，need_analysts 不丢失"""
    _capture(monkeypatch,
             '{"pass": false, "defect_list": ["证据不足"], "need_analysts": ["method", "critic"], "suggestion": "补做多视')

    note = reviewer.review("问题", [_ev(1)])

    assert note["passed"] is False
    assert note["need_analysts"] == ["method", "critic"]
