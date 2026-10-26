# 订阅（研究方向）相关模型
from pydantic import BaseModel, Field, field_validator

from app.utils import PUB_WINDOWS


class SubscriptionBase(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    keywords: list[str] = Field(min_length=1)
    sources: list[str] = Field(min_length=1)
    threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    # 多选时间窗：逗号分隔（如 '1Y,3Y+'），至少选一个；存库前统一成固定顺序
    pub_window: str = Field(default="1Y")

    @field_validator("keywords")
    @classmethod
    def keywords_not_empty(cls, v: list[str]) -> list[str]:
        cleaned = [k.strip() for k in v if k and k.strip()]
        if not cleaned:
            raise ValueError("至少需要一个非空关键词")
        return cleaned

    @field_validator("sources")
    @classmethod
    def sources_not_empty(cls, v: list[str]) -> list[str]:
        cleaned = [s.strip() for s in v if s and s.strip()]
        if not cleaned:
            raise ValueError("至少选择一个数据来源")
        return cleaned

    @field_validator("pub_window")
    @classmethod
    def pub_window_not_empty(cls, v: str) -> str:
        tokens = [t.strip() for t in v.split(",") if t.strip()]
        if not tokens:
            raise ValueError("至少选择一个发表时间范围")
        invalid = [t for t in tokens if t not in PUB_WINDOWS]
        if invalid:
            raise ValueError(f"不支持的时间范围：{', '.join(invalid)}")
        return ",".join(w for w in PUB_WINDOWS if w in tokens)


class SubscriptionCreate(SubscriptionBase):
    pass


class SubscriptionUpdate(SubscriptionBase):
    pass


class SubscriptionActivePatch(BaseModel):
    active: bool


class BatchDeleteRequest(BaseModel):
    ids: list[int] = Field(min_length=1)


class SubscriptionOut(BaseModel):
    id: int
    name: str
    keywords: list[str]
    sources: list[str]
    threshold: float
    pub_window: str
    active: bool
    last_run_at: str | None = None
    created_at: str | None = None
    paper_count: int = 0
    unread_count: int = 0
