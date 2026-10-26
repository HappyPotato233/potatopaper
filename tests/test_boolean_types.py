# 用例10 布尔返回值：active / is_read / is_default 读出为 True / False（不再是 0/1）
from sqlalchemy import select

from app.models import LlmModel, Paper, Subscription, SubscriptionPaper, User


def test_boolean_reads_are_python_bool(session):
    """active / is_read / is_default 读出为 Python 布尔"""
    u = User(username="u", password_hash="h")
    sub = Subscription(user_id=1, name="d", keywords=[], sources=["arXiv"])
    p = Paper(external_id="e1", source="arXiv", title="T")
    m = LlmModel(name="n", model_id="m1", vendor="v", capabilities=[])
    session.add(u)
    session.commit()
    sub.user_id = u.id
    session.add_all([sub, p, m])
    session.commit()
    session.add(SubscriptionPaper(subscription_id=sub.id, paper_id=p.id))
    session.commit()
    sub_id, paper_id, model_id = sub.id, p.id, m.id

    session.expire_all()                                  # 强制从库里重新读
    assert session.scalar(select(Subscription).where(Subscription.id == sub_id)).active is True
    assert session.scalar(select(SubscriptionPaper)
                          .where(SubscriptionPaper.paper_id == paper_id)).is_read is False
    assert session.scalar(select(LlmModel).where(LlmModel.id == model_id)).is_default is False

    session.execute(__import__("sqlalchemy").update(Subscription)
                    .where(Subscription.id == sub_id).values(active=False))
    session.execute(__import__("sqlalchemy").update(SubscriptionPaper)
                    .where(SubscriptionPaper.paper_id == paper_id).values(is_read=True))
    session.execute(__import__("sqlalchemy").update(LlmModel)
                    .where(LlmModel.id == model_id).values(is_default=True))
    session.commit()
    session.expire_all()
    assert session.scalar(select(Subscription).where(Subscription.id == sub_id)).active is False
    assert session.scalar(select(SubscriptionPaper)
                          .where(SubscriptionPaper.paper_id == paper_id)).is_read is True
    assert session.scalar(select(LlmModel).where(LlmModel.id == model_id)).is_default is True
