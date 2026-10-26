# 文献/未读相关模型
from pydantic import BaseModel, Field


class PaperOut(BaseModel):
    """文献条目（含本地静态元数据增强字段）"""
    id: int
    external_id: str | None = None
    source: str
    title: str
    authors: list[str] = []
    abstract: str = ""
    journal: str = ""
    published_at: str | None = None
    url: str | None = None
    score: float = 0.0
    is_read: bool = False
    # 展示前元数据增强（零 LLM，本地静态映射注入）
    journal_level: str = ""
    ccf: str = ""
    cas_zone: str = ""
    arxiv_cat: str = ""


class UnreadItem(BaseModel):
    subscription_id: int
    name: str
    unread: int


class UnreadSummary(BaseModel):
    total_unread: int
    items: list[UnreadItem]


class BatchReadRequest(BaseModel):
    subscription_id: int
    paper_ids: list[int] = Field(min_length=1)
