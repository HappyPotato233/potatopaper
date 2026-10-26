# 用例1 建表（8 张表齐全、索引/唯一键/外键、ngram 全文索引）/ 用例2 初始化幂等
import pytest
from sqlalchemy import func, inspect, select
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.models import AgentModel, LlmModel, User

EXPECTED_TABLES = {"users", "subscriptions", "papers", "subscription_papers",
                   "task_logs", "models", "agent_model", "settings"}


def test_all_tables_exist(engine):
    """用例1：8 张表齐全"""
    assert EXPECTED_TABLES <= set(inspect(engine).get_table_names())


def test_papers_indexes_and_fulltext(engine):
    """用例1：papers 索引/唯一键齐全；ngram 全文索引存在"""
    names = {i["name"] for i in inspect(engine).get_indexes("papers")}
    assert {"uk_papers_external", "uk_papers_source_ext", "idx_papers_source",
            "idx_papers_published", "ft_papers_text"} <= names
    with engine.connect() as conn:
        ddl = conn.exec_driver_sql("SHOW CREATE TABLE papers").fetchone()[1]
    # MySQL 8 回显为 /*!50100 WITH PARSER `ngram` */（解析器名带反引号），去反引号后比对
    assert "FULLTEXT" in ddl and "WITH PARSER ngram" in ddl.replace("`", "")


def test_foreign_keys_exist(engine):
    """用例1：外键（含级联）存在"""
    insp = inspect(engine)
    assert "fk_subs_user" in {fk["name"] for fk in insp.get_foreign_keys("subscriptions")}
    sp = {fk["name"] for fk in insp.get_foreign_keys("subscription_papers")}
    assert "fk_sp_sub" in sp and "fk_sp_paper" in sp
    assert "fk_am_model" in {fk["name"] for fk in insp.get_foreign_keys("agent_model")}
    assert "fk_settings_user" in {fk["name"] for fk in insp.get_foreign_keys("settings")}


def test_init_database_idempotent(engine, test_db_name):
    """用例2：init_database 连跑两遍不报错；管理员创建；默认模型 + 8 个 agent 绑定"""
    from app import database as db

    db.init_database(test_db_name)
    db.init_database(test_db_name)              # 第二遍必须不报错（幂等）

    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with Session() as s:
        assert s.scalar(select(func.count()).select_from(User).where(User.role == "admin")) == 1
        if not (settings.LLM_API_KEY and settings.LLM_BASE_URL):
            pytest.skip("未配置 LLM 环境变量，跳过默认模型引导断言")
        assert s.scalar(select(func.count()).select_from(LlmModel)) == 1
        assert s.scalar(select(func.count()).select_from(AgentModel)) == 8
