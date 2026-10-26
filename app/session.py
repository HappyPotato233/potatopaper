# 会话层：SQLAlchemy 引擎 / SessionLocal / FastAPI 依赖（只管"怎么连、怎么给会话"）
from urllib.parse import quote_plus

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings


def make_url(db_name: str | None = None) -> str:
    """构造 MySQL 连接串（pymysql 驱动）；db_name 为空则构造不含库名的连接串（建库用）"""
    pwd = quote_plus(settings.MYSQL_PASSWORD)
    base = (f"mysql+pymysql://{settings.MYSQL_USER}:{pwd}@"
            f"{settings.MYSQL_HOST}:{settings.MYSQL_PORT}")
    tail = f"/{db_name}?charset=utf8mb4" if db_name else "/?charset=utf8mb4"
    return base + tail


def create_app_engine(db_name: str | None = None):
    """创建引擎：QueuePool(pool_size=10) + pre_ping + recycle（对齐 spec §6.3）"""
    return create_engine(
        make_url(db_name),
        pool_size=10,
        pool_pre_ping=True,   # 取连接前先探活，避免拿到僵尸连接
        pool_recycle=3600,    # 提前回收长连接
    )


# 进程级引擎（FastAPI 与 Worker 共用）
engine = create_app_engine(settings.MYSQL_DATABASE)

# Session 不是线程安全的：sessionmaker 每次调用返回新 Session，一请求/一线程一个
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db():
    """FastAPI 依赖：每请求产出一个 Session，请求结束自动 close（只读路径不 commit）"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
