# 用例5 papers / 用例6 subscription_papers / 用例7 task_logs / 用例11 批量写
import pytest
from sqlalchemy import func, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.exc import IntegrityError

from app.models import Paper, Subscription, SubscriptionPaper, TaskLog, User


def _seed(s):
    """造一个用户 + 一个方向"""
    u = User(username="u", password_hash="h")
    s.add(u)
    s.commit()
    sub = Subscription(user_id=u.id, name="d", keywords=[], sources=["arXiv"])
    s.add(sub)
    s.commit()
    return u, sub


def test_paper_unique_source_ext(session):
    """用例5：(source, external_id) 唯一去重生效"""
    session.add(Paper(external_id="e1", source="arXiv", title="T"))
    session.commit()
    session.add(Paper(external_id="e1", source="arXiv", title="dup"))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_paper_external_id_unique(session):
    """用例5：external_id 唯一"""
    session.add(Paper(external_id="e1", source="arXiv", title="T"))
    session.commit()
    session.add(Paper(external_id="e1", source="SS", title="dup"))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_subscription_paper_unique_mark_and_unread(session):
    """用例6：(subscription_id, paper_id) 唯一去重；is_read 标记与 read_at；未读数统计"""
    u, sub = _seed(session)
    p = Paper(external_id="e1", source="arXiv", title="T")
    session.add(p)
    session.commit()
    session.add(SubscriptionPaper(subscription_id=sub.id, paper_id=p.id, score=0.9))
    session.commit()
    session.add(SubscriptionPaper(subscription_id=sub.id, paper_id=p.id))   # 重复
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()

    unread = session.scalar(select(func.count()).select_from(SubscriptionPaper)
                            .where(SubscriptionPaper.subscription_id == sub.id,
                                   SubscriptionPaper.is_read.is_(False)))
    assert unread == 1

    row = session.scalar(select(SubscriptionPaper).where(SubscriptionPaper.paper_id == p.id))
    row.is_read, row.read_at = True, func.now()
    session.commit()
    session.expire_all()
    row = session.scalar(select(SubscriptionPaper).where(SubscriptionPaper.paper_id == p.id))
    assert row.is_read is True and row.read_at is not None


def test_task_log_insert_and_query(session):
    """用例7：写入；按 task_id / status 查询"""
    session.add_all([
        TaskLog(task_id="t1", kind="followup", status="success",
                detail={"a": 1}, pulled=3, added=2),
        TaskLog(task_id="t2", kind="followup", status="failed"),
    ])
    session.commit()
    assert session.scalar(select(func.count()).select_from(TaskLog)
                          .where(TaskLog.task_id == "t1")) == 1
    ok = session.scalars(select(TaskLog).where(TaskLog.status == "success")).all()
    assert len(ok) == 1 and ok[0].detail == {"a": 1}


def test_bulk_insert_ignore_semantics(session):
    """用例11：execute_many 语义等价 —— 3 条去重插入只落 2 条"""
    u, sub = _seed(session)
    p1 = Paper(external_id="e1", source="arXiv", title="T1")
    p2 = Paper(external_id="e2", source="arXiv", title="T2")
    session.add_all([p1, p2])
    session.commit()

    rows = [{"subscription_id": sub.id, "paper_id": p1.id},
            {"subscription_id": sub.id, "paper_id": p2.id},
            {"subscription_id": sub.id, "paper_id": p1.id}]      # 第 3 条与第 1 条重复
    session.execute(mysql_insert(SubscriptionPaper).prefix_with("IGNORE"), rows)
    session.commit()
    assert session.scalar(select(func.count()).select_from(SubscriptionPaper)
                          .where(SubscriptionPaper.subscription_id == sub.id)) == 2
