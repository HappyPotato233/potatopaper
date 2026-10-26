# 数据源试拉：提交研究方向前，对每个来源各取 1 条，验证来源可用性与关键词有效性
from dataclasses import dataclass
from datetime import datetime

from sources import resolve_source

# 试拉不套用增量游标：新建方向的游标就是“当前时间”，套用会必然拉到 0 条。
# 这里只回答一个问题：这组关键词在该来源能不能返回文献。
PROBE_SINCE = datetime(2000, 1, 1)
PROBE_MAX_RESULTS = 1

# 失败原因分类：决定给用户看的简单提示（具体异常只给管理员）
NO_RESULT = "no_result"
SOURCE_ERROR = "source_error"
NO_ADAPTER = "no_adapter"


@dataclass
class ProbeOutcome:
    """单个来源的试拉结果（reason 为空表示成功）"""
    source: str
    reason: str | None = None
    title: str | None = None
    detail: str = ""          # 底层具体原因，仅管理员可见

    @property
    def ok(self) -> bool:
        return self.reason is None


def probe_source(label: str, keywords: list[str]) -> ProbeOutcome:
    """对单个来源试拉 1 条；异常不抛出，统一转成失败结果"""
    adapter = resolve_source(label)
    if adapter is None:
        return ProbeOutcome(label, reason=NO_ADAPTER, detail="未注册的数据源适配器")
    try:
        items = adapter.fetch(keywords, PROBE_SINCE, max_results=PROBE_MAX_RESULTS)
    except Exception as e:
        return ProbeOutcome(label, reason=SOURCE_ERROR, detail=f"{type(e).__name__}: {e}")
    if not items:
        return ProbeOutcome(label, reason=NO_RESULT, detail="该来源未返回任何文献")
    return ProbeOutcome(label, title=items[0].title)


def probe_sources(labels: list[str], keywords: list[str]) -> list[ProbeOutcome]:
    """按前端展示名逐个试拉（去重且保持原顺序）"""
    return [probe_source(label, keywords) for label in dict.fromkeys(labels)]
