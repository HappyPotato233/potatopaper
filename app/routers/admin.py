# 管理端路由（RBAC，admin 专属）：用户管理 + 模型池 + agent 绑定（对齐详细设计 §3.8）
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ai.llm_provider import AGENT_DEFS
from app.deps import require_role
from app.models import AgentModel, LlmModel, User
from app.schemas.admin import (AdminUserOut, AdminUserPatch, AgentBindingOut,
                               AgentBindRequest, ModelCreate, ModelOut, ModelStatusPatch,
                               ModelUpdate)
from app.session import get_db
from app.utils import fmt_dt

router = APIRouter(prefix="/api/admin", tags=["admin"],
                   dependencies=[Depends(require_role("admin"))])


# ---------------- 用户管理 ----------------

def _user_out(row: User) -> AdminUserOut:
    return AdminUserOut(
        id=row.id, username=row.username, role=row.role, status=row.status,
        created_at=fmt_dt(row.created_at), last_login_at=fmt_dt(row.last_login_at),
    )


@router.get("/users")
def list_users(page: int = Query(1, ge=1), limit: int = Query(20, ge=1, le=100),
               q: str = "", session: Session = Depends(get_db)):
    """用户列表（分页 + 关键字筛选）"""
    conditions = []
    if q:
        conditions.append(User.username.like(f"%{q}%"))
    base = select(User).where(*conditions)
    total = session.scalar(select(func.count()).select_from(base.subquery()))
    rows = session.scalars(base.order_by(User.id)
                           .limit(limit).offset((page - 1) * limit)).all()
    return {"total": total, "page": page, "limit": limit,
            "items": [_user_out(r) for r in rows]}


@router.get("/users/{user_id}", response_model=AdminUserOut)
def get_user(user_id: int, session: Session = Depends(get_db)):
    row = session.get(User, user_id)
    if not row:
        raise HTTPException(status_code=404, detail="用户不存在")
    return _user_out(row)


@router.patch("/users/{user_id}", response_model=AdminUserOut)
def patch_user(user_id: int, body: AdminUserPatch, session: Session = Depends(get_db)):
    """更新用户 status / role；最后一名管理员保护（停用/降级拦截）"""
    row = session.get(User, user_id)
    if not row:
        raise HTTPException(status_code=404, detail="用户不存在")

    demoting = (body.role == "user" or body.status == "disabled") and row.role == "admin"
    if demoting:
        admin_n = session.scalar(select(func.count()).select_from(User)
                                 .where(User.role == "admin", User.status == "active"))
        if admin_n <= 1:
            raise HTTPException(status_code=400, detail="不能停用/降级最后一名管理员")

    if body.status:
        row.status = body.status
    if body.role:
        row.role = body.role
    session.commit()
    session.refresh(row)
    return _user_out(row)


# ---------------- 模型池 ----------------

def _model_out(row: LlmModel) -> ModelOut:
    return ModelOut(
        id=row.id, name=row.name, model_id=row.model_id, vendor=row.vendor,
        base_url=row.base_url or "", capabilities=row.capabilities or [],
        is_default=bool(row.is_default), status=row.status, extra=row.extra or {},
        api_key_configured=bool(row.api_key_enc), updated_at=fmt_dt(row.updated_at),
    )


def _get_model(session: Session, model_id: int) -> LlmModel:
    row = session.get(LlmModel, model_id)
    if not row:
        raise HTTPException(status_code=404, detail="模型不存在")
    return row


def _clear_default(session: Session, model_id: int) -> None:
    """设置新默认时清除其他默认标记（同能力唯一默认）"""
    session.execute(update(LlmModel)
                    .where(LlmModel.is_default.is_(True), LlmModel.id != model_id)
                    .values(is_default=False))


@router.get("/models")
def list_models(page: int = Query(1, ge=1), limit: int = Query(50, ge=1, le=200),
                status: str = Query("", pattern="^(|active|disabled)$"),
                capability: str = "", session: Session = Depends(get_db)):
    """模型池列表（分页 + 状态/能力筛选；不回传 api_key 明文）"""
    conditions = []
    if status:
        conditions.append(LlmModel.status == status)
    if capability:
        conditions.append(LlmModel.capabilities.like(f'%"{capability}"%'))
    base = select(LlmModel).where(*conditions)
    total = session.scalar(select(func.count()).select_from(base.subquery()))
    rows = session.scalars(base.order_by(LlmModel.id)
                           .limit(limit).offset((page - 1) * limit)).all()
    return {"total": total, "page": page, "limit": limit, "items": [_model_out(r) for r in rows]}


@router.post("/models", response_model=ModelOut, status_code=201)
def create_model(body: ModelCreate, session: Session = Depends(get_db)):
    """添加模型：校验 UNIQUE(vendor, model_id)（F-MODEL-01）"""
    dup = session.scalar(select(LlmModel.id).where(LlmModel.vendor == body.vendor,
                                                   LlmModel.model_id == body.model_id))
    if dup:
        raise HTTPException(status_code=409, detail="同 vendor 下 model_id 已存在")
    if body.is_default:
        _clear_default(session, 0)                      # 0 = 清掉全部默认标记
    model = LlmModel(name=body.name, model_id=body.model_id, vendor=body.vendor,
                     base_url=body.base_url, api_key_enc=body.api_key,
                     capabilities=body.capabilities, is_default=bool(body.is_default),
                     extra=body.extra)
    session.add(model)
    session.commit()
    return _model_out(model)


@router.get("/models/{model_id}", response_model=ModelOut)
def get_model(model_id: int, session: Session = Depends(get_db)):
    """模型详情（api_key 仅返回已配置/未配置脱敏标记）"""
    return _model_out(_get_model(session, model_id))


@router.put("/models/{model_id}", response_model=ModelOut)
def update_model(model_id: int, body: ModelUpdate, session: Session = Depends(get_db)):
    """更新模型配置（api_key 不传保持不变）"""
    row = _get_model(session, model_id)
    changes = {}
    if body.name is not None:
        changes["name"] = body.name
    if body.base_url is not None:
        changes["base_url"] = body.base_url
    if body.api_key:
        changes["api_key_enc"] = body.api_key
    if body.capabilities is not None:
        changes["capabilities"] = body.capabilities
    if body.is_default is not None:
        if body.is_default:
            _clear_default(session, model_id)
        changes["is_default"] = bool(body.is_default)
    if body.extra is not None:
        changes["extra"] = body.extra
    if changes:
        session.execute(update(LlmModel).where(LlmModel.id == model_id).values(**changes))
    session.commit()
    session.refresh(row)        # 回读 DB 侧 ON UPDATE CURRENT_TIMESTAMP 生成的 updated_at
    return _model_out(row)


@router.patch("/models/{model_id}/status", response_model=ModelOut)
def patch_model_status(model_id: int, body: ModelStatusPatch, session: Session = Depends(get_db)):
    """启停模型：停用后不可再被选用、不影响进行中任务（F-MODEL-02）"""
    row = _get_model(session, model_id)
    session.execute(update(LlmModel).where(LlmModel.id == model_id)
                    .values(status=body.status))
    session.commit()
    session.refresh(row)        # 回读 DB 侧 ON UPDATE CURRENT_TIMESTAMP 生成的 updated_at
    return _model_out(row)


# ---------------- agent 绑定 ----------------

@router.get("/agents", response_model=list[AgentBindingOut])
def list_agents(session: Session = Depends(get_db)):
    """agent 清单 + 当前绑定模型；前端绑定下拉候选 = 启用且能力匹配的模型"""
    bindings = {r.agent_key: r for r in session.scalars(select(AgentModel)).all()}
    out = []
    for key, desc in AGENT_DEFS.items():
        b = bindings.get(key)
        model_name = None
        if b:
            m = session.get(LlmModel, b.model_id)
            model_name = m.name if m else None
        out.append(AgentBindingOut(
            agent_key=key, label=desc["label"],
            required_capability=desc["capability"],
            model_id=b.model_id if b else None,
            model_name=model_name,
            updated_at=fmt_dt(b.updated_at) if b else None,
        ))
    return out


@router.put("/agents/{agent_key}/model")
def bind_agent(agent_key: str, body: AgentBindRequest,
               admin: dict = Depends(require_role("admin")),
               session: Session = Depends(get_db)):
    """为 agent 绑定模型：校验 agent 存在、模型 active 且能力匹配（F-MODEL-03/04）"""
    if agent_key not in AGENT_DEFS:
        raise HTTPException(status_code=404, detail="未知 agent")
    need_cap = AGENT_DEFS[agent_key]["capability"]
    model = _get_model(session, body.model_id)
    if model.status != "active":
        raise HTTPException(status_code=400, detail="目标模型已停用，不可绑定")
    if need_cap not in (model.capabilities or []):
        raise HTTPException(status_code=400, detail=f"模型能力不满足该 agent（需 {need_cap}）")

    session.merge(AgentModel(agent_key=agent_key, model_id=body.model_id,
                             updated_by=admin["id"]))
    session.commit()
    return {"code": 0, "message": f"agent {agent_key} 已绑定模型 {model.name}",
            "agent_key": agent_key, "model_id": body.model_id}


@router.put("/models/{model_id}/bind-all")
def bind_all_agents(model_id: int,
                    admin: dict = Depends(require_role("admin")),
                    session: Session = Depends(get_db)):
    """一键切换：把全部能力匹配的 agent 绑定到指定模型

    逐 agent 复用 bind_agent 的校验口径（模型 active + 能力匹配），
    能力不满足的 agent 保持原绑定并在 skipped 里返回原因。
    """
    model = _get_model(session, model_id)
    if model.status != "active":
        raise HTTPException(status_code=400, detail="目标模型已停用，不可绑定")
    caps = model.capabilities or []
    bound, skipped = [], []
    for key, desc in AGENT_DEFS.items():
        need_cap = desc["capability"]
        if need_cap not in caps:
            skipped.append({"agent_key": key, "reason": f"模型缺少能力 {need_cap}"})
            continue
        session.merge(AgentModel(agent_key=key, model_id=model_id,
                                 updated_by=admin["id"]))
        bound.append(key)
    session.commit()
    message = f"{len(bound)} 个 agent 已切换到 {model.name}"
    if skipped:
        message += f"，{len(skipped)} 个能力不匹配已跳过"
    return {"code": 0, "message": message, "model_id": model_id,
            "bound": bound, "skipped": skipped}
