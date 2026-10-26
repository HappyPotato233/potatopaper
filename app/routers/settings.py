# 用户设置路由：跟进频率 / 通知开关 / 来源池（对齐详细设计 §3.6）
from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.deps import get_current_user
from app.models import Setting
from app.schemas.settings import NotifyConfig, SettingsOut, SettingsUpdate
from app.session import get_db

router = APIRouter(prefix="/api/settings", tags=["settings"])


def _ensure_row(session: Session, user_id: int) -> Setting:
    """读取设置行（无则按默认值落库）"""
    row = session.scalar(select(Setting).where(Setting.user_id == user_id))
    if row:
        return row
    row = Setting(user_id=user_id, frequency="daily",
                  notify={"email": False, "push": False},
                  sources=["arXiv", "Semantic Scholar", "万方"])
    session.add(row)
    session.commit()
    return row


@router.get("", response_model=SettingsOut)
def get_settings(user: dict = Depends(get_current_user),
                 session: Session = Depends(get_db)):
    row = _ensure_row(session, user["id"])
    return SettingsOut(
        frequency=row.frequency,
        notify=NotifyConfig(**(row.notify or {})),
        sources=row.sources or [],
    )


@router.put("", response_model=SettingsOut)
def update_settings(body: SettingsUpdate, user: dict = Depends(get_current_user),
                    session: Session = Depends(get_db)):
    row = _ensure_row(session, user["id"])
    row.frequency = body.frequency
    row.notify = {"email": body.notify.email, "push": body.notify.push}
    row.sources = body.sources
    session.commit()
    return get_settings(user, session)
