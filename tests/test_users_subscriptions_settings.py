# 用例3 users / 用例4 subscriptions / 用例9 settings
import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from app.models import Setting, Subscription, User


def _mk_user(s, username="alice", role="user"):
    u = User(username=username, password_hash="h", role=role)
    s.add(u)
    s.commit()
    return u


def test_user_lookup_and_role(session):
    """用例3：按用户名查询；role 枚举值正确"""
    _mk_user(session, "alice", "admin")
    u = session.scalar(select(User).where(User.username == "alice"))
    assert u is not None and u.role == "admin"


def test_username_unique_conflict(session):
    """用例3：username 唯一冲突报错"""
    _mk_user(session)
    session.add(User(username="alice", password_hash="h"))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_subscription_crud_and_active_toggle(session):
    """用例4：增删改查；active 切换"""
    u = _mk_user(session)
    sub = Subscription(user_id=u.id, name="方向A", keywords=["a"], sources=["arXiv"],
                       threshold=0.5, pub_window="1Y")
    session.add(sub)
    session.commit()
    sub.name, sub.active = "方向B", False
    session.commit()
    session.refresh(sub)
    assert (sub.name, sub.active, sub.threshold) == ("方向B", False, 0.5)
    sub_id = sub.id
    session.delete(sub)
    session.commit()
    assert session.scalar(select(Subscription).where(Subscription.id == sub_id)) is None


def test_user_delete_cascades_subscription(session):
    """用例4：删用户级联带走订阅"""
    u = _mk_user(session)
    sub = Subscription(user_id=u.id, name="x", keywords=[], sources=["arXiv"])
    session.add(sub)
    session.commit()
    sub_id, user_id = sub.id, u.id
    session.execute(delete(User).where(User.id == user_id))   # 直删父行，级联交给数据库
    session.commit()
    assert session.scalar(select(Subscription).where(Subscription.id == sub_id)) is None


def test_settings_unique_and_roundtrip(session):
    """用例9：user_id 唯一；读写往返"""
    u = _mk_user(session)
    session.add(Setting(user_id=u.id, frequency="weekly",
                        notify={"email": True, "push": False}, sources=["arXiv", "万方"]))
    session.commit()
    row = session.scalar(select(Setting).where(Setting.user_id == u.id))
    assert row.frequency == "weekly"
    assert row.notify == {"email": True, "push": False}
    assert row.sources == ["arXiv", "万方"]
    row.frequency = "daily"
    session.commit()
    assert session.scalar(select(Setting).where(Setting.user_id == u.id)).frequency == "daily"
    session.add(Setting(user_id=u.id, frequency="daily", notify={}, sources=[]))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()
