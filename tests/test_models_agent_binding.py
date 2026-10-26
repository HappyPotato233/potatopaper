# 用例8 models / agent_model
import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.models import AgentModel, LlmModel


def _mk_model(s, vendor="volc-ark", mid="m1"):
    m = LlmModel(name="默认", model_id=mid, vendor=vendor,
                 capabilities=["chat", "function_call"])
    s.add(m)
    s.commit()
    return m


def test_model_vendor_mid_unique(session):
    """用例8：(vendor, model_id) 唯一"""
    _mk_model(session, "volc-ark", "m1")
    session.add(LlmModel(name="x", model_id="m1", vendor="volc-ark", capabilities=[]))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_model_capabilities_json(session):
    """用例8：capabilities 为 JSON 数组"""
    m = _mk_model(session)
    assert m.capabilities == ["chat", "function_call"]


def test_agent_binding_insert_and_update(session):
    """用例8：agent 绑定插入与更新（agent_key 主键，merge 等价 upsert）"""
    m = _mk_model(session)
    session.merge(AgentModel(agent_key="main", model_id=m.id))
    session.merge(AgentModel(agent_key="planner", model_id=m.id))
    session.commit()
    assert session.scalar(select(AgentModel).where(AgentModel.agent_key == "main")).model_id == m.id

    m2 = _mk_model(session, "volc-ark", "m2")
    session.merge(AgentModel(agent_key="main", model_id=m2.id))
    session.commit()
    assert session.scalar(select(AgentModel).where(AgentModel.agent_key == "main")).model_id == m2.id
    assert session.scalar(select(func.count()).select_from(AgentModel)) == 2
