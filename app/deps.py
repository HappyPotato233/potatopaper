# FastAPI 依赖：Bearer 鉴权 + RBAC 角色拦截
from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.models import User
from app.security import decode_token
from app.session import get_db


def get_current_user(request: Request, session: Session = Depends(get_db)) -> dict:
    """解析 Authorization: Bearer <token>，返回用户信息 dict；未登录/失效 401"""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="未登录或 token 缺失")
    payload = decode_token(auth.removeprefix("Bearer ").strip())
    if payload is None:
        raise HTTPException(status_code=401, detail="token 无效或已过期")
    user = session.get(User, int(payload["sub"]))
    if not user:
        raise HTTPException(status_code=401, detail="用户不存在")
    if user.status != "active":
        raise HTTPException(status_code=401, detail="账号已停用")
    # 保持 dict 契约：各路由的 user["id"] / user["role"] 等读法无需改动
    return {"id": user.id, "username": user.username, "role": user.role,
            "status": user.status, "created_at": user.created_at,
            "last_login_at": user.last_login_at}


def require_role(role: str):
    """管理端角色拦截依赖：非目标角色 403（对齐详细设计 §3.8）"""
    def checker(user: dict = Depends(get_current_user)) -> dict:
        if user["role"] != role:
            raise HTTPException(status_code=403, detail="admin role required")
        return user
    return checker
