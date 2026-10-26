# 管理端（RBAC）相关模型：用户管理 + 模型池 + agent 绑定
from pydantic import BaseModel, Field, field_validator


# ---------------- 用户管理 ----------------

class AdminUserOut(BaseModel):
    id: int
    username: str
    role: str
    status: str
    created_at: str | None = None
    last_login_at: str | None = None


class AdminUserPatch(BaseModel):
    status: str | None = Field(default=None, pattern="^(active|disabled)$")
    role: str | None = Field(default=None, pattern="^(user|admin)$")


# ---------------- 模型池 ----------------

class ModelCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    model_id: str = Field(min_length=1, max_length=128)
    vendor: str = Field(min_length=1, max_length=64)
    base_url: str = ""
    api_key: str = ""  # 仅服务端存储，不回传
    capabilities: list[str] = Field(default_factory=lambda: ["chat"])
    is_default: bool = False
    extra: dict = Field(default_factory=dict)

    @field_validator("capabilities")
    @classmethod
    def capabilities_valid(cls, v: list[str]) -> list[str]:
        allowed = {"chat", "embedding", "vision"}
        cleaned = [c for c in v if c in allowed]
        if not cleaned:
            raise ValueError("能力标签至少包含 chat/embedding/vision 之一")
        return cleaned


class ModelUpdate(BaseModel):
    name: str | None = None
    base_url: str | None = None
    api_key: str | None = None  # 不传则保持不变
    capabilities: list[str] | None = None
    is_default: bool | None = None
    extra: dict | None = None


class ModelStatusPatch(BaseModel):
    status: str = Field(pattern="^(active|disabled)$")


class ModelOut(BaseModel):
    id: int
    name: str
    model_id: str
    vendor: str
    base_url: str = ""
    capabilities: list[str] = []
    is_default: bool = False
    status: str = "active"
    extra: dict = {}
    api_key_configured: bool = False  # 脱敏：仅返回是否已配置
    updated_at: str | None = None


# ---------------- agent 绑定 ----------------

class AgentBindingOut(BaseModel):
    agent_key: str
    label: str
    required_capability: str  # 该 agent 用途要求的能力标签（chat）
    model_id: int | None = None
    model_name: str | None = None
    updated_at: str | None = None


class AgentBindRequest(BaseModel):
    model_id: int
