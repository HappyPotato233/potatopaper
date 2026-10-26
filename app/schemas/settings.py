# 用户设置相关模型
from pydantic import BaseModel, Field


class NotifyConfig(BaseModel):
    email: bool = False
    push: bool = False


class SettingsOut(BaseModel):
    frequency: str = "daily"  # hourly / 6h / daily / weekly
    notify: NotifyConfig = NotifyConfig()
    sources: list[str] = []


class SettingsUpdate(BaseModel):
    frequency: str = Field(default="daily", pattern="^(hourly|6h|daily|weekly)$")
    notify: NotifyConfig = NotifyConfig()
    sources: list[str] = Field(default_factory=list)
