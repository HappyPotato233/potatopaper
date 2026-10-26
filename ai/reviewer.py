# Reviewer 复审 Agent（编排方案 v4 §2.2 / 详细设计 §5.1.4）：独立质检，不生成也不修改任何业务内容
# 检查对象（§33 汇总前移）：编排层先出「待交付草稿」再进复审，故清单①幻觉校验改为对**草稿正文**核验引用真实性
# 检查清单 5 项：①幻觉校验（对草稿核验引用） ②证据充足度 ③逻辑一致性 ④视角完整性 ⑤置信标记
#   清单按输入自适应：本轮无分析师结论时 ③④ 无输入可评，只查 ①②⑤；
#   「未触发多视角分析」属 Planner 调度选择，不得据此判否（只走 suggestion 提示）
# 输出固定 JSON：{"pass": bool, "defect_list": [...], "suggestion": "给 Planner 的改进指令"}
# 失败降级（§8.1）：LLM 不可用 / 输出非法 → None，调用方跳过质检并标注「未经过完整复审质检」
#   例外：输出被 max_tokens 截断（JSON 尾部不闭合）时走 _salvage 宽松提取，保住「驳回 + 改进指令」不丢失
# 需求通道（Phase 4）：reviewer 认为需补做多视角分析时，在输出 JSON 的 need_analysts 里列出所需视角；
#   该字段为内部通道，由编排层消费后确定性补跑分析师，不得进入 review_notes 前端契约
import re

from ai.llm_provider import chat, parse_json_loose

# 提高预算：512 时常被 defect_list + suggestion 撑满而截断，导致驳回意见整体作废；
# 1024 → 2048：10 并发下约 4/10 返回空字符串（诊断日志实证原文为 ''），
# 怀疑推理 token 吃光预算后正文为空，故再放大一档观察失败率是否下降。
REVIEW_MAX_TOKENS = 2048
EVIDENCE_SNIPPET = 400   # 证据摘要切片（控制 token 边界）
DRAFT_SNIPPET = 2000     # 待交付草稿切片（控制 token 边界）
_DRAFT_PLACEHOLDER = "（本轮未产出草稿）"   # 草稿缺失占位：不作为判否依据
_VIEW_KEYS = ("method", "critic", "compare")   # need_analysts 合法取值（分析师视角 key）

_SYSTEM = "你是文献问答复审质检 Agent。只输出 JSON，不生成也不修改任何业务内容。"

# 有分析师输出：5 项全查
_PROMPT_FULL = """你是独立复审质检 Agent，只做校验、不生成也不修改任何业务内容。
检查清单：
1. 幻觉校验：草稿正文中引用的文献编号必须真实存在于下方证据；
2. 证据充足度：现有文献是否足以回答用户问题；
3. 逻辑一致性：多个分析师结论之间是否互相冲突；
4. 视角完整性：综述类问题是否遗漏主流流派或正反视角；
5. 置信标记：不确定结论是否标注【存疑，建议人工核验】。

用户问题：{question}

待交付草稿（编号为引用标识）：
{draft}

文献证据（编号为引用标识）：
{evidence}

分析师输出（可能部分缺失）：
{analysts}

若第 4 项（视角完整性）判定缺少某视角，可写入 need_analysts 提出补做要求（defect_list 口径不变）。

输出 JSON（defect_list 最多 3 条、每条不超过 20 字；suggestion 一句话不超过 40 字，务必输出完整闭合的 JSON）：
{{"pass": true|false, "defect_list": ["缺陷描述，无缺陷则空数组"], "suggestion": "给 Planner 的改进指令，一句话", "need_analysts": ["需要补做的视角，无需求则空数组"]}}"""

# 无分析师输出：与多视角相关的检查项无输入可评，只查证据类三项；
# 「未触发多视角分析」是 Planner 的调度选择，不得作为判否依据（只走 suggestion 提示）
# 需求通道：认为需补做多视角时走 need_analysts（编排层执行），仍不改 pass 判定口径
_PROMPT_EVIDENCE_ONLY = """你是独立复审质检 Agent，只做校验、不生成也不修改任何业务内容。

本轮未执行多视角分析（Planner 本轮未调用分析师）：与多视角相关的检查项无输入可评，本轮不列入检查范围。

本轮检查清单：
1. 幻觉校验：草稿正文中引用的文献编号必须真实存在于下方证据；
2. 证据充足度：现有文献是否足以回答用户问题；
3. 置信标记：不确定结论是否标注【存疑，建议人工核验】。

用户问题：{question}

待交付草稿（编号为引用标识）：
{draft}

文献证据（编号为引用标识）：
{evidence}

硬性规则：
1. 本轮未触发多视角分析属于 Planner 的调度选择，不得据此判 pass=false；
2. 若你认为本问题确需多视角分析（如综述、流派对比、正反评价类），请写入 need_analysts 提出补做要求，不要写入 defect_list；
3. 仅当证据少于 2 篇时不得提出该要求（分析师无材料可评，补做无意义）。

need_analysts 取值：["method"（方法抽取）| "critic"（批判审阅）| "compare"（横向对比）]，不需要补做时输出空数组。

输出 JSON（defect_list 最多 3 条、每条不超过 20 字；suggestion 一句话不超过 40 字，务必输出完整闭合的 JSON）：
{{"pass": true|false, "defect_list": ["缺陷描述，无缺陷则空数组"], "suggestion": "给 Planner 的改进指令，一句话", "need_analysts": ["需要补做的视角，无需求则空数组"]}}"""


def review(question: str, evidence: list[dict],
           analyst_outputs: dict | None = None,
           draft: str | None = None) -> dict | None:
    """复审质检入口（一次 LLM 调用）

    检查清单按输入自适应：本轮有分析师结论时 5 项全查；无分析师结论时只查证据类三项，
    并把「未触发多视角分析」限定为 suggestion 提示（不作为判否依据）。

    :param draft: 编排层汇总出的待交付草稿正文（§33 汇总前移）；缺省/空白 → 回填占位，不作为判否依据
    :return: {"passed": bool, "defects": [str], "suggestions": [str], "need_analysts": [str]}；
             LLM 不可用/输出非法 → None
    """
    # 两套模板占位符不同（证据态无 {analysts}），故 format 必须落在分支内
    draft_block = _build_draft_block(draft)
    if _analysts_present(analyst_outputs):
        prompt = _PROMPT_FULL.format(question=question, draft=draft_block,
                                     evidence=_build_evidence_block(evidence),
                                     analysts=_build_analyst_block(analyst_outputs or {}))
    else:
        prompt = _PROMPT_EVIDENCE_ONLY.format(question=question, draft=draft_block,
                                              evidence=_build_evidence_block(evidence))
    try:
        raw = chat("reviewer", prompt, system=_SYSTEM, max_tokens=REVIEW_MAX_TOKENS)
    except Exception as e:
        print(f"[reviewer] 复审失败（跳过质检）: {e}")
        return None
    struct = parse_json_loose(raw)
    if not struct or not isinstance(struct.get("pass"), bool):
        struct = _salvage(raw)  # JSON 被截断（尾部不闭合）→ 宽松提取，避免「已驳回」这一事实丢失
    if not struct or not isinstance(struct.get("pass"), bool):
        # 输出非法（空响应/非 JSON/截断兜底也救不回）→ 跳过质检；留原文片段便于定位
        print(f"[reviewer] 复审输出无法解析（跳过质检），原文前 120 字: {raw[:120]!r}")
        return None
    defects = [str(d).strip() for d in (struct.get("defect_list") or []) if str(d).strip()][:3]
    suggestion = str(struct.get("suggestion") or "").strip()
    # 归一化 DTO：passed / defects / suggestions 为前端契约字段；
    # need_analysts 为内部需求通道（编排层消费，不进入 review_notes）
    return {"passed": struct["pass"], "defects": defects,
            "suggestions": [suggestion] if suggestion else [],
            "need_analysts": _clean_views(struct.get("need_analysts"))}


def _salvage(raw: str) -> dict | None:
    """截断兜底：JSON 尾部不闭合时，从原文宽松提取 pass / defect_list / suggestion

    背景：Reviewer 驳回时常带 5 条缺陷 + 一句建议，超出 max_tokens 被硬截断 → 严格解析失败，
    若直接返回 None，编排器会记为「复审异常，跳过质检」，把「未通过 + 改进指令」一并丢弃，
    上游 Planner 因此拿不到该调分析师/补检索的指示而空转。故只要 pass 字段可辨，就保留驳回事实。
    """
    if not raw:
        return None
    m = re.search(r'"pass"\s*:\s*(true|false)', raw)
    if not m:
        return None
    struct: dict = {"pass": m.group(1) == "true"}
    block = re.search(r'"defect_list"\s*:\s*\[(.*?)(?:\]|$)', raw, flags=re.S)
    if block:
        struct["defect_list"] = re.findall(r'"((?:[^"\\]|\\.)*)"', block.group(1))
    views = re.search(r'"need_analysts"\s*:\s*\[(.*?)(?:\]|$)', raw, flags=re.S)
    if views:
        struct["need_analysts"] = re.findall(r'"((?:[^"\\]|\\.)*)"', views.group(1))
    # suggestion 可能被截断在字符串中途（无闭合引号），故允许匹配到文末
    sug = re.search(r'"suggestion"\s*:\s*"(.*?)(?:"|\Z)', raw, flags=re.S)
    if sug:
        struct["suggestion"] = sug.group(1).strip()
    return struct


def _build_draft_block(draft: str | None) -> str:
    """待交付草稿切片：超长截断（token 边界）；未产出草稿时回填占位（不作为判否依据）"""
    text = (draft or "").strip()
    return text[:DRAFT_SNIPPET] if text else _DRAFT_PLACEHOLDER


def _build_evidence_block(evidence: list[dict]) -> str:
    blocks = []
    for i, ev in enumerate(evidence, start=1):
        abstract = (ev.get("abstract") or "")[:EVIDENCE_SNIPPET]
        blocks.append(f"[{i}] {ev.get('title', '')}\n{abstract}")
    return "\n\n".join(blocks) or "（无检索证据）"


def _clean_views(raw) -> list[str]:
    """need_analysts 合法性校验：只认三个视角 key、去重保序（不可信 LLM 输出）"""
    out: list[str] = []
    for k in raw or []:
        k = str(k).strip()
        if k in _VIEW_KEYS and k not in out:
            out.append(k)
    return out


def _analysts_present(analyst_outputs: dict | None) -> bool:
    """本轮是否产出过分析师结论（任一视角有非空文本即为真）

    判据取「值非空」而非「键存在」，与下方 _build_analyst_block 的过滤口径一致：
    只挂了视角键、文本为空的输出不算「有内容可评」，此时不该评 ③逻辑一致性 / ④视角完整性。
    """
    return any(str(v).strip() for v in (analyst_outputs or {}).values())


def _build_analyst_block(analyst_outputs: dict) -> str:
    labels = {"method": "方法抽取", "critic": "批判审阅", "compare": "横向对比"}
    parts = [f"{labels[k]}：{analyst_outputs[k]}" for k in labels if analyst_outputs.get(k)]
    return "\n\n".join(parts) or "（本问题未触发多视角分析）"
