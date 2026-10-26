# 跟进路由：手动触发（异步下发）、任务状态轮询、跟进日志列表（对齐详细设计 §3.4）
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.deps import get_current_user
from app.models import Subscription, TaskLog
from app.session import get_db
from app.utils import fmt_dt, jloads

router = APIRouter(prefix="/api", tags=["followup"])


def _sanitize_detail(detail: dict, role: str) -> dict:
    """日志/任务详情脱敏：底层来源报错只给管理员，普通用户只看概括提示"""
    errors = (detail or {}).get("source_errors", [])
    if role != "admin" and errors:
        detail = {**detail, "source_errors": ["部分来源暂时不可用，请稍后重试"]}
    return detail or {}


@router.post("/follow-up/run")
def run_followup(subscription_id: int, user: dict = Depends(get_current_user),
                 session: Session = Depends(get_db)):
    """手动触发"运行跟进"：下发跟进单，立即返回 {task_id, status:'queued'}（异步）"""
    # 仅取主键做存在性判断（等价原 SELECT *）
    sub = session.scalar(
        select(Subscription.id).where(Subscription.id == subscription_id,
                                      Subscription.user_id == user["id"]))
    if sub is None:
        raise HTTPException(status_code=404, detail="订阅不存在")

    from worker.queue import task_queue
    from worker.scheduler import _inflight, _inflight_lock
    with _inflight_lock:
        if subscription_id in _inflight:
            raise HTTPException(status_code=409, detail="该方向已有跟进任务在执行中")
        _inflight.add(subscription_id)
    task_id = task_queue.enqueue(
        "follow_up",
        {"subscription_id": subscription_id, "kind": "manual", "merge": False, "merged_windows": 1},
    )
    return {"task_id": task_id, "status": "queued"}


@router.get("/follow-up/tasks/{task_id}")
def task_status(task_id: str, user: dict = Depends(get_current_user),
                session: Session = Depends(get_db)):
    """查询任务状态/进度（前端轮询依据；状态落在 task_logs）"""
    t = TaskLog.__table__          # 列形态 select：mappings() 键即列名，兼容原 dict 读法
    log = session.execute(
        select(t)
        .outerjoin(Subscription, Subscription.id == t.c.subscription_id)
        .where(t.c.task_id == task_id,
               # 保留原语义：无订阅归属的日志对所有登录用户可见
               or_(Subscription.user_id == user["id"], t.c.subscription_id.is_(None)))
    ).mappings().first()
    if not log:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {
        "task_id": task_id,
        "status": log.get("status") or "unknown",
        "subscription_id": log.get("subscription_id"),
        "direction_name": log.get("direction_name"),
        "pulled": log.get("pulled", 0),
        "added": log.get("added", 0),
        "deduped": log.get("deduped", 0),
        "duration": log.get("duration"),
        "detail": _sanitize_detail(jloads(log.get("detail"), {}), user["role"]),
        "started_at": fmt_dt(log.get("started_at")),
        "finished_at": fmt_dt(log.get("finished_at")),
    }


@router.get("/logs")
def list_logs(user: dict = Depends(get_current_user),
              page: int = Query(1, ge=1), limit: int = Query(20, ge=1, le=100),
              status: str = Query("", pattern="^(|success|partial_failed|failed)$"),
              subscription_id: int = Query(0),
              session: Session = Depends(get_db)):
    """跟进日志列表（分页 + 状态筛选；具体报错仅管理员可见）"""
    t = TaskLog.__table__
    if user["role"] == "admin":
        # 管理员额外可见无归属订阅的试拉失败日志（新建方向被拦截时 subscription_id 为空）
        base = select(t).outerjoin(Subscription, Subscription.id == t.c.subscription_id)
        owner = or_(Subscription.user_id == user["id"], t.c.subscription_id.is_(None))
    else:
        base = select(t).join(Subscription, Subscription.id == t.c.subscription_id)
        owner = Subscription.user_id == user["id"]
    conditions = [owner]
    if status:
        conditions.append(t.c.status == status)
    if subscription_id:
        conditions.append(t.c.subscription_id == subscription_id)

    base = base.where(*conditions)
    snapshot = base.subquery()          # 聚合/计数统一在子查询上做

    total = session.scalar(select(func.count()).select_from(snapshot))
    # 累计拉取/新增（统计行数据源，§2.4）
    agg = session.execute(
        select(func.coalesce(func.sum(snapshot.c.pulled), 0).label("pulled"),
               func.coalesce(func.sum(snapshot.c.added), 0).label("added"))
    ).mappings().first()
    rows = session.execute(
        base.order_by(t.c.started_at.desc()).limit(limit).offset((page - 1) * limit)
    ).mappings().all()
    items = []
    for r in rows:
        detail = _sanitize_detail(jloads(r.get("detail"), {}), user["role"])
        items.append({
            "id": r["id"], "task_id": r.get("task_id"), "kind": r.get("kind"),
            "subscription_id": r.get("subscription_id"),
            "direction_name": r.get("direction_name"),
            "pulled": r.get("pulled", 0), "added": r.get("added", 0), "deduped": r.get("deduped", 0),
            "duration": r.get("duration"), "status": r.get("status"),
            "brief": detail.get("brief", ""), "source_errors": detail.get("source_errors", []),
            "started_at": fmt_dt(r.get("started_at")), "finished_at": fmt_dt(r.get("finished_at")),
        })
    return {"total": total, "total_pulled": int(agg["pulled"]), "total_added": int(agg["added"]),
            "page": page, "limit": limit, "items": items}
