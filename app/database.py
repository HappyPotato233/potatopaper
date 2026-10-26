# 数据库层：SQLAlchemy 引擎/会话 + 建库建表/初始化
# init_database：建库 → create_all（模型为权威）→ 管理员/默认模型引导
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.models import AgentModel, Base, LlmModel, User
from app.session import engine, make_url


def _ensure_database(name: str) -> None:
    """建库：用不含库名的临时引擎连 server（幂等）"""
    server = create_engine(make_url(None))
    try:
        with server.begin() as conn:
            conn.execute(text(
                f"CREATE DATABASE IF NOT EXISTS `{name}` "
                f"DEFAULT CHARSET utf8mb4 COLLATE utf8mb4_unicode_ci"))
    finally:
        server.dispose()


def init_database(db_name: str | None = None) -> None:
    """启动初始化：建库 → 建表 → 管理员 → 默认模型引导（幂等）

    :param db_name: 目标库名，默认 settings.MYSQL_DATABASE（测试可传独立测试库名）
    """
    name = db_name or settings.MYSQL_DATABASE
    _ensure_database(name)

    # 建表：模型为权威；create_all 对已存在的表是空操作（对存量库绝对安全）
    target = engine if name == settings.MYSQL_DATABASE else create_engine(make_url(name))
    Base.metadata.create_all(target)

    Session = sessionmaker(bind=target, autoflush=False, expire_on_commit=False)
    with Session() as session:
        _init_admin(session)
        _bootstrap_default_model(session)
    if target is not engine:
        target.dispose()


def _init_admin(session) -> None:
    """管理员初始化（方案 A）：无 admin 账号时从环境变量创建（幂等，逻辑不变）"""
    from app.security import hash_password

    admin = session.scalar(select(User).where(User.role == "admin").limit(1))
    if admin:
        return
    username, password = settings.ADMIN_USERNAME, settings.ADMIN_PASSWORD
    if not username or not password:
        print("[init] 未配置 ADMIN_USERNAME/ADMIN_PASSWORD，跳过管理员创建")
        return
    session.add(User(username=username, password_hash=hash_password(password), role="admin"))
    session.commit()
    print(f"[init] 已创建管理员账号: {username}（请尽快修改默认口令）")


def _bootstrap_default_model(session) -> None:
    """模型池引导：表为空且有 LLM 环境配置时写入默认模型（幂等，逻辑不变）"""
    if not settings.LLM_API_KEY or not settings.LLM_BASE_URL:
        return
    if session.scalar(select(LlmModel.id).limit(1)):
        return
    model = LlmModel(
        name=f"{settings.LLM_MODEL_NAME} (默认)",
        model_id=settings.LLM_MODEL_NAME,
        vendor="volc-ark",
        base_url=settings.LLM_BASE_URL,
        api_key_enc=settings.LLM_API_KEY,
        # function_call：Planner / Reviewer 绑定的必需能力标签（详细设计 §3.8）
        capabilities=["chat", "function_call"],
        is_default=True,
    )
    session.add(model)
    session.flush()                                   # 取到自增主键
    # 各 agent 绑定该模型（planner/reviewer 为 v4 编排新增，retriever 无 LLM 不绑）
    for key in ("main", "planner", "reviewer", "qa_router", "summarizer",
                "analyst-method", "analyst-critic", "analyst-compare"):
        session.merge(AgentModel(agent_key=key, model_id=model.id))  # 等价 INSERT IGNORE
    session.commit()
    print(f"[init] 已引导默认模型: {settings.LLM_MODEL_NAME} 并完成 agent 绑定")
