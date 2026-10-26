# 订阅（研究方向）管理路由（对齐详细设计 §3.2）
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import case, delete, func, select, update
from sqlalchemy.orm import Session

from app.deps import get_current_user
from app.models import Subscription, SubscriptionPaper, TaskLog
from app.schemas.subscription import (BatchDeleteRequest, SubscriptionActivePatch,
                                      SubscriptionCreate, SubscriptionOut, SubscriptionUpdate)
from app.session import get_db
from app.utils import fmt_dt
from sources.probe import (NO_ADAPTER, NO_RESULT, SOURCE_ERROR, ProbeOutcome,
                           probe_sources)

router = APIRouter(prefix="/api/subscriptions", tags=["subscriptions"])

# 试拉失败原因 → 用户可见的简单提示（底层异常只给管理员）
_FAIL_REASON_HINT = {
    NO_RESULT: "未检索到相关文献，请调整关键词",
    SOURCE_ERROR: "暂时不可用，请稍后重试或移除该来源",
    NO_ADAPTER: "暂不支持，请更换采集来源",
}


def _probe_user_message(failed: list[ProbeOutcome]) -> str:
    """用户侧提示：只说哪个来源没过、大致怎么改，不含任何底层异常"""
    parts = [f"{o.source}（{_FAIL_REASON_HINT.get(o.reason, '暂不可用')}）" for o in failed]
    return f"文献源试拉未通过：{'；'.join(parts)}。请调整研究方向后重新提交。"


def _probe_message(user: dict, failed: list[ProbeOutcome]) -> str:
    """接口返回的提示：普通用户只看简单提示，管理员额外附具体报错"""
    message = _probe_user_message(failed)
    if user["role"] == "admin":
        message += "\n管理员可见详情：" + "；".join(f"{o.source}: {o.detail}" for o in failed)
    return message


def _probe_or_reject(body, user: dict, session: Session, sub_id: int | None = None) -> None:
    """提交前逐个来源试拉 1 条；任一失败则落日志并中断保存（§3.2 前置校验）"""
    started = datetime.now()
    failed = [o for o in probe_sources(body.sources, body.keywords) if not o.ok]
    if not failed:
        return
    finished = datetime.now()
    detail = {
        "brief": _probe_user_message(failed),
        "source_errors": [f"{o.source}: {o.detail}" for o in failed],   # 仅管理员可在日志查看
    }
    session.add(TaskLog(task_id=uuid.uuid4().hex[:16], kind="probe", subscription_id=sub_id,
                        direction_name=body.name, status="failed", detail=detail,
                        started_at=started, finished_at=finished,
                        duration=int((finished - started).total_seconds() * 1000)))
    session.commit()                       # 先落日志再报错，管理员可在追踪日志查看
    raise HTTPException(status_code=400, detail=_probe_message(user, failed))


def _to_out(row: Subscription, paper_count: int = 0, unread_count: int = 0) -> SubscriptionOut:
    """订阅实体 → 输出模型（JSON 列经 ORM 读出即 list，无需 json.loads）"""
    return SubscriptionOut(
        id=row.id, name=row.name, keywords=row.keywords or [], sources=row.sources or [],
        threshold=float(row.threshold), pub_window=row.pub_window, active=bool(row.active),
        last_run_at=fmt_dt(row.last_run_at), created_at=fmt_dt(row.created_at),
        paper_count=paper_count, unread_count=unread_count)


def _counts(session: Session, user_id: int) -> dict[int, tuple[int, int]]:
    """各方向 (关联文献数, 未读数)"""
    stmt = (select(SubscriptionPaper.subscription_id,
                   func.count().label("total"),
                   func.sum(case((SubscriptionPaper.is_read.is_(False), 1), else_=0)).label("unread"))
            .join(Subscription, Subscription.id == SubscriptionPaper.subscription_id)
            .where(Subscription.user_id == user_id)
            .group_by(SubscriptionPaper.subscription_id))
    return {r["subscription_id"]: (int(r["total"]), int(r["unread"] or 0))
            for r in session.execute(stmt).mappings().all()}


def _owned(session: Session, sub_id: int, user_id: int) -> Subscription:
    """取归属当前用户的订阅，否则 404"""
    row = session.get(Subscription, sub_id)
    if not row or row.user_id != user_id:
        raise HTTPException(status_code=404, detail="订阅不存在")
    return row


@router.get("", response_model=list[SubscriptionOut])
def list_subscriptions(user: dict = Depends(get_current_user),
                       session: Session = Depends(get_db)):
    rows = session.scalars(select(Subscription)
                           .where(Subscription.user_id == user["id"])
                           .order_by(Subscription.created_at.desc())).all()
    counts = _counts(session, user["id"])
    return [_to_out(r, *counts.get(r.id, (0, 0))) for r in rows]


@router.post("", response_model=SubscriptionOut, status_code=201)
def create_subscription(body: SubscriptionCreate, user: dict = Depends(get_current_user),
                        session: Session = Depends(get_db)):
    _probe_or_reject(body, user, session)      # 新建方向：先试拉各来源，失败不保存
    sub = Subscription(user_id=user["id"], name=body.name, keywords=body.keywords,
                       sources=body.sources, threshold=body.threshold,
                       pub_window=body.pub_window)
    session.add(sub)
    session.commit()
    return _to_out(sub)


@router.put("/{sub_id}", response_model=SubscriptionOut)
def update_subscription(sub_id: int, body: SubscriptionUpdate,
                        user: dict = Depends(get_current_user),
                        session: Session = Depends(get_db)):
    row = _owned(session, sub_id, user["id"])
    # 只有影响抓取结果的字段变化时重新试拉；改名称/阈值不触发网络请求
    changed = (body.keywords != (row.keywords or []) or body.sources != (row.sources or [])
               or body.pub_window != row.pub_window)
    if changed:
        _probe_or_reject(body, user, session, sub_id=sub_id)
    row.name, row.keywords, row.sources = body.name, body.keywords, body.sources
    row.threshold, row.pub_window = body.threshold, body.pub_window
    session.commit()
    return _to_out(row)


@router.patch("/{sub_id}/active", response_model=SubscriptionOut)
def toggle_active(sub_id: int, body: SubscriptionActivePatch,
                  user: dict = Depends(get_current_user),
                  session: Session = Depends(get_db)):
    row = _owned(session, sub_id, user["id"])
    row.active = bool(body.active)
    session.commit()
    return _to_out(row)


@router.delete("/{sub_id}")
def delete_subscription(sub_id: int, user: dict = Depends(get_current_user),
                        session: Session = Depends(get_db)):
    _owned(session, sub_id, user["id"])
    # ON DELETE CASCADE：级联删除方向关联与阅读状态（交给数据库）
    session.execute(delete(Subscription).where(Subscription.id == sub_id))
    session.commit()
    return {"code": 0, "message": "已删除"}


@router.post("/batch-delete")
def batch_delete(body: BatchDeleteRequest, user: dict = Depends(get_current_user),
                 session: Session = Depends(get_db)):
    owned = set(session.scalars(
        select(Subscription.id).where(Subscription.user_id == user["id"])).all())
    deletable = [i for i in body.ids if i in owned]
    if not deletable:
        raise HTTPException(status_code=404, detail="无权删除的订阅")
    session.execute(delete(Subscription).where(Subscription.id.in_(deletable)))
    session.commit()
    return {"code": 0, "message": f"已删除 {len(deletable)} 个订阅", "deleted": deletable}


@router.post("/{sub_id}/read-all")
def read_all(sub_id: int, user: dict = Depends(get_current_user),
             session: Session = Depends(get_db)):
    """一键全部已读（详细设计 §3.3）"""
    _owned(session, sub_id, user["id"])
    affected = session.execute(
        update(SubscriptionPaper)
        .where(SubscriptionPaper.subscription_id == sub_id,
               SubscriptionPaper.is_read.is_(False))
        .values(is_read=True, read_at=func.now())).rowcount
    session.commit()
    return {"code": 0, "message": f"已标记 {affected} 篇为已读"}
