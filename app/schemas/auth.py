# 认证相关模型
from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1)


class UnreadBrief(BaseModel):
    """单方向未读概览（登录快路径返回）"""
    subscription_id: int
    name: str
    unread: int


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str
    username: str
    user_id: int
    total_unread: int
    unread_brief: list[UnreadBrief] = []
