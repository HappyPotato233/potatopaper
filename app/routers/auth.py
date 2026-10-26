# 认证路由：登录（下发跟进单 + 不阻塞返回总览）与登出（对齐详细设计 §3.1）
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import case, func, select, update
from sqlalchemy.orm import Session

from app.deps import get_current_user
from app.models import Subscription, SubscriptionPaper, User
from app.schemas.auth import LoginRequest, LoginResponse, UnreadBrief
from app.security import create_token, verify_password
from app.session import get_db
from app.storage import clear_user_pdfs
from app.utils import fmt_dt

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login", response_model=LoginResponse)
def login(body: LoginRequest, session: Session = Depends(get_db)):
    """登录：①校验（停用拒绝）；②下发跟进单（异步，不阻塞）；③返回总览+token；④登记 last_login_at"""
    user = session.scalar(select(User).where(User.username == body.username))
    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    if user.status != "active":
        raise HTTPException(status_code=403, detail="账号已停用，请联系管理员")

    # 登录惰性触发：对到期订阅补发跟进单（非 7×24 容错，总体设计 §7）
    from worker.scheduler import enqueue_due_for_user
    enqueued = enqueue_due_for_user(user.id, kind="login")

    # 不阻塞返回已入库总览：未读数 + 各方向未读概览
    rows = session.execute(
        select(Subscription.id, Subscription.name,
               func.sum(case((SubscriptionPaper.is_read.is_(False), 1), else_=0)).label("unread"))
        .select_from(Subscription)
        .outerjoin(SubscriptionPaper, SubscriptionPaper.subscription_id == Subscription.id)
        .where(Subscription.user_id == user.id)
        .group_by(Subscription.id, Subscription.name)
        .order_by(Subscription.id)).mappings().all()
    brief = [UnreadBrief(subscription_id=r["id"], name=r["name"], unread=int(r["unread"] or 0))
             for r in rows]

    session.execute(update(User).where(User.id == user.id)
                    .values(last_login_at=datetime.now()))
    session.commit()
    print(f"[auth] 用户 {user.username} 登录，惰性补发跟进单 {enqueued} 个")

    return LoginResponse(
        access_token=create_token(user.id, user.username, user.role),
        role=user.role, username=user.username, user_id=user.id,
        total_unread=sum(b.unread for b in brief), unread_brief=brief,
    )


@router.post("/logout")
def logout(user: dict = Depends(get_current_user)):
    """登出：丢弃 token，并删除该用户的 PDF 临时缓存"""
    clear_user_pdfs(user["id"])
    return {"code": 0, "message": "已登出", "last_login_at": fmt_dt(user.get("last_login_at"))}
