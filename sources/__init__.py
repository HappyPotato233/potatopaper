# 数据源适配层：统一封装外部 API，处理限速/退避/增量参数
# 导入即注册各适配器（arXiv / Semantic Scholar / 万方）
from sources.base import (
    PaperRecord,
    SourceAdapter,
    available_sources,
    get_adapter,
    normalize_title,
    register_adapter,
)
from sources.arxiv import adapter as _arxiv
from sources.semantic_scholar import adapter as _s2
from sources.wanfang import adapter as _wanfang

register_adapter(_arxiv)
register_adapter(_s2)
register_adapter(_wanfang)

# 前端候选池展示名 → 适配器注册名 的映射（对齐前端设计文档 §4.3 ALL_SOURCES）
# 注：前端 ALL_SOURCES 传入的是中文「万方」，必须映射到注册名 Wanfang，否则被记「无适配器」
SOURCE_LABELS = {
    "arXiv": "arXiv",
    "Semantic Scholar": "Semantic Scholar",
    "万方": "Wanfang",
    "Wanfang": "Wanfang",
}


def resolve_source(label: str) -> SourceAdapter | None:
    """按前端展示名/注册名解析适配器（大小写不敏感）"""
    return get_adapter(label) or get_adapter(SOURCE_LABELS.get(label, ""))
