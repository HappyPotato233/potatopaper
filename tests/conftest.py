# 特征测试夹具：独立测试库（literature_radar_test），绝不触碰生产库 literature_radar
import os

# Phase 3：会话 checkpointer 落盘路径覆盖为进程内内存库（零文件产物、零跨进程污染）
os.environ.setdefault("LITRADAR_QA_DB", ":memory:")

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.models import (AgentModel, Base, LlmModel, Paper, Setting, Subscription,
                        SubscriptionPaper, TaskLog, User)
from app.session import make_url

TEST_DB = os.getenv("TEST_MYSQL_DATABASE", "literature_radar_test")

# 清表顺序（先关联表后主表；同时关了外键检查，顺序其实无所谓）
_CLEAN_ORDER = [SubscriptionPaper, TaskLog, AgentModel, LlmModel,
                Subscription, Setting, Paper, User]


@pytest.fixture(scope="session")
def test_db_name() -> str:
    """测试库名；断言不复用生产库"""
    assert TEST_DB != settings.MYSQL_DATABASE, "测试库不得复用生产库"
    return TEST_DB


@pytest.fixture(scope="session")
def engine(test_db_name):
    """会话级：建测试库 → 用 create_all 建 8 张表（不依赖人工预建）"""
    server = create_engine(make_url(None))
    try:
        with server.begin() as conn:
            conn.execute(text(
                f"CREATE DATABASE IF NOT EXISTS `{test_db_name}` "
                f"DEFAULT CHARSET utf8mb4 COLLATE utf8mb4_unicode_ci"))
    finally:
        server.dispose()

    eng = create_engine(make_url(test_db_name))
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def session(engine):
    """用例级：一个 Session"""
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    s = Session()
    yield s
    s.close()


@pytest.fixture(autouse=True)
def _clean(engine):
    """每个用例开始前清空所有表，保证用例互不干扰"""
    with engine.begin() as conn:
        conn.execute(text("SET FOREIGN_KEY_CHECKS=0"))
        for model in _CLEAN_ORDER:
            conn.execute(text(f"TRUNCATE TABLE `{model.__tablename__}`"))
        conn.execute(text("SET FOREIGN_KEY_CHECKS=1"))
    yield
