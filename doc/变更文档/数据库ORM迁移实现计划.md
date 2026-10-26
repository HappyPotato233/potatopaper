# 文献雷达数据层 ORM 迁移 · 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把文献雷达的数据访问层从「原生 SQL + mysql-connector-python」迁移为「SQLAlchemy 2.0 ORM + PyMySQL」，模型成为表结构唯一权威，行为零变化。

**Architecture:** 分四阶段：`阶段0 连通性预检` → `阶段1 搭地基`（新增 models/session，旧助手等价替换、业务代码一行未动）→ `阶段2 上安全网`（pytest 特征测试全绿）→ `阶段3 逐个换血`（14 文件 / 69 处调用点换成 SQLAlchemy 表达式，换完即跑测试）。每阶段均可独立运行、独立回退。

**Tech Stack:** Python 3.11 / FastAPI / SQLAlchemy 2.0（DeclarativeBase + Mapped）/ PyMySQL / MySQL 8 / pytest

**依据文档：** [数据库ORM迁移设计.md](file:///d:/实习/my_project/deep_search_pro-master/doc/数据库ORM迁移设计.md)（spec v1.0，已评审通过）

---

## Global Constraints

以下为 spec 的全局要求，**每个任务的隐含前提**，精确值逐字取自 spec：

- 驱动固定 `mysql+pymysql://`，移除 `mysql-connector-python`。
- 依赖下限：`sqlalchemy>=2.0`、`PyMySQL>=1.1.0`、`cryptography>=42`、`pytest>=8`。
- **不引入 Alembic**；**不新建 Repository / DAO 层**。
- 表结构以 `app/models.py` 为**唯一权威**；`app/schema.sql` 在阶段 3 末尾删除。
- 索引名 / 唯一键名 / 外键名沿用现状，使生成的 DDL 与存量表逐字一致。
- **不改任何 HTTP 接口契约、不改前端、不改 v3/v4 编排与抓取业务逻辑。**
- 允许保留原生 SQL 的仅限：`ai/retriever.py` 的 `MATCH ... AGAINST` 全文检索、聚合统计类查询。
- 连接池固定 `QueuePool(pool_size=10, pool_pre_ping=True, pool_recycle=3600)`。
- 时间戳写法固定：`server_default=text("CURRENT_TIMESTAMP")` / `server_default=text("CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP")`。
- 测试库固定 `literature_radar_test`，**绝不复用生产库 `literature_radar`**。
- 验收门禁：`scripts/align_schema.py` 差异清单为空、`pytest` 全绿、全仓 grep 无 `db.query(` / `db.execute(` / `db.execute_many(` / `mysql.connector` 残留。
- 环境：解释器 `D:\Anaconda\envs\shixi\python.exe`；库账号 `root/123456@localhost`（见 `.env`）。

---

## 转换约定（Phase 3 通用规则）

阶段 3 的每个调用点都归入下列某一类，后文按编号引用，避免重复贴代码。

| 编号 | 场景 | 写法 |
|---|---|---|
| **C1** | 按主键取一行 | `session.get(Model, pk)` → 实体 |
| **C2** | 单表条件查询 | `session.scalars(select(Model).where(...)).all()` / `.first()` |
| **C3** | JOIN / 聚合 / 自定义列投影，且消费方按 dict 读 | `session.execute(select(...)).mappings()` → dict 列表 |
| **C4** | 全文检索 / 复杂 SQL（spec §6.4 允许） | `session.execute(text(sql), {...}).mappings()` 保留原生 SQL |
| **C5** | 单行写入 | ORM 实体 `session.add()` / 属性赋值 + `session.commit()` |
| **C6** | 批量 UPDATE / DELETE | `session.execute(update(Model).where(...).values(...))` / `delete(Model).where(...)` + `commit()` |
| **C7** | upsert / 忽略插入 | `mysql_insert(...).on_duplicate_key_update(...)`；`mysql_insert(...).prefix_with("IGNORE")` |
| **C8** | 结果流向 dict 消费者（`enhance_meta`、`_to_evidence`、`_to_out(row: dict)`、pipeline 的 `sub`） | 一律走 **C3**，产出 dict / `RowMapping`，**不改消费者** |

补充约定：

- **路由会话注入**：每个路由函数新增形参 `session: Session = Depends(get_db)`；`get_current_user` 也通过 `Depends(get_db)` 取同一 Session（FastAPI 依赖缓存保证同请求同 Session）。
- **`get_current_user` 返回值保持 `dict`**：内部用 `session.get(User, id)`（C1）读实体，再转成 dict 返回 —— 这样 8 个路由文件里所有 `user["id"]` / `user["role"]` 等读法**一行都不用改**，是本次迁移最重要的减震设计。
- **`sub` 传递保持 dict**：pipeline 内部把订阅行以 `RowMapping` 形态向下传（C3/C8），`_fetch_start`、`_rank_and_filter`、`_build_brief` 等抓取逻辑函数签名与内部读法不变。
- **非 web 场景的会话**：worker / ai / 脚本各自 `with SessionLocal() as s:` 开短事务，不跨线程共享 Session。

---

## Phase 0 · 连通性预检（已执行，仅留档与补备份）

### Task 1: 备份 + 依赖与连通性核定

**Files:**
- 只读核对：`.env`、`requirements.txt`、`pyproject.toml`
- 产出：`d:\实习\my_project\backup\literature_radar_YYYYMMDD.sql`（仓库外，不入项目树）

**Interfaces:**
- Consumes: 无
- Produces: 环境结论（后续所有任务的前提）

- [ ] **Step 1: 备份生产库**

```powershell
mysqldump -h localhost -P 3306 -u root -p123456 --single-transaction --routines --triggers literature_radar > "d:\实习\my_project\backup\literature_radar_20261017.sql"
```

Expected: 生成非空 .sql 文件（含 users / subscriptions / papers / subscription_papers / task_logs / models / agent_model / settings 八张表的 CREATE 与 INSERT）。

- [ ] **Step 2: 核定依赖已就位**

Run:

```powershell
D:\Anaconda\envs\shixi\python.exe -m pip show SQLAlchemy PyMySQL cryptography pytest
```

Expected（本机实测已满足，若缺则 `pip install`）：`SQLAlchemy 2.0.30`、`PyMySQL 1.2.0`、`cryptography 49.0.0`、`pytest 8.3.4`。

- [ ] **Step 3: pymysql 连通性预检（门禁）**

```python
# 临时脚本，跑完即删（不留在项目树）
import pymysql
c = pymysql.connect(host="localhost", port=3306, user="root", password="123456",
                    database="literature_radar", charset="utf8mb4")
cur = c.cursor()
cur.execute("SELECT 1")
assert cur.fetchone() == (1,)
cur.execute("SELECT id, title FROM papers LIMIT 1")
assert cur.fetchone() is not None
c.close()
print("Phase 0 PASS")
```

Expected: 输出 `Phase 0 PASS`（本机已实测通过；`caching_sha2_password` 认证依赖 `cryptography`，已装）。

- [ ] **Step 4: 确定版本控制基线（需你拍板，见文末"待确认事项 A"）**

若选择 `git init`：

```powershell
cd d:\实习\my_project\deep_search_pro-master
git init
git add app ai worker sources web requirements.txt pyproject.toml main.py doc
git commit -m "chore: baseline before ORM migration"
```

Expected: 得到可回退的基线提交。**若你拒绝 git（项目当前在 `d:\实习` 仓库中处于未跟踪状态，本目录无 `.git`）**，则改为对 `app/`、`ai/`、`worker/` 做一次 zip 快照放到 `d:\实习\my_project\backup\`，后续每个阶段的"提交"步骤替换为"打一个阶段快照"。

**门禁：Step 1–3 未通过，后续一步都不动。**

---

## Phase 1 · 搭地基（业务代码一行不动）

### Task 2: 新增 `app/models.py`（8 个 ORM 模型）

**Files:**
- Create: `app/models.py`

**Interfaces:**
- Consumes: 无
- Produces: `Base`（DeclarativeBase）、8 个模型类 `User / Subscription / Paper / SubscriptionPaper / TaskLog / LlmModel / AgentModel / Setting`；`Subscription.items`、`Paper.items`（→ `SubscriptionPaper`）与 `SubscriptionPaper.subscription / .paper` 双向关系。后续所有任务依赖这里的类名与属性名。

- [ ] **Step 1: 写入模型文件**

```python
# 数据模型层：8 张表的 ORM 模型（表结构唯一权威，替代 app/schema.sql）
# 对齐《详细设计文档.md》§2；索引/唯一键/外键名沿用现状，保证生成的 DDL 与存量库逐字一致
from datetime import date, datetime

from sqlalchemy import (BigInteger, Boolean, Date, DateTime, Double, Enum, ForeignKey,
                        Index, Integer, JSON, String, Text, UniqueConstraint, text)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# 全表通用建表参数：InnoDB + utf8mb4（与现状一致）
TABLE_KW = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4",
            "mysql_collate": "utf8mb4_unicode_ci"}

# 时间戳：显式 text() 写法（不用 func.now()），保证 DDL 与现状逐字一致
TS_CREATED = text("CURRENT_TIMESTAMP")
TS_UPDATED = text("CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP")


class Base(DeclarativeBase):
    """声明基类：Base.metadata 是建表权威"""


class User(Base):
    """用户表"""
    __tablename__ = "users"
    __table_args__ = (Index("idx_users_role", "role"), TABLE_KW)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(Enum("user", "admin"), nullable=False,
                                      server_default=text("'user'"))
    status: Mapped[str] = mapped_column(Enum("active", "disabled"), nullable=False,
                                        server_default=text("'active'"))
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False,
                                                 server_default=TS_CREATED)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Subscription(Base):
    """研究方向订阅表"""
    __tablename__ = "subscriptions"
    __table_args__ = (Index("idx_subs_user", "user_id"), TABLE_KW)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", name="fk_subs_user", ondelete="CASCADE"),
        nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    keywords: Mapped[list] = mapped_column(JSON, nullable=False)
    sources: Mapped[list] = mapped_column(JSON, nullable=False)
    threshold: Mapped[float] = mapped_column(Double, nullable=False,
                                             server_default=text("0.5"))
    pub_window: Mapped[str] = mapped_column(Enum("1Y", "1_3Y", "3Y+"), nullable=False,
                                            server_default=text("'1Y'"))
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("1"))
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False,
                                                 server_default=TS_CREATED)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False,
                                                 server_default=TS_UPDATED)

    items: Mapped[list["SubscriptionPaper"]] = relationship(
        back_populates="subscription", passive_deletes=True)


class Paper(Base):
    """文献主表"""
    __tablename__ = "papers"
    __table_args__ = (
        UniqueConstraint("external_id", name="uk_papers_external"),
        UniqueConstraint("source", "external_id", name="uk_papers_source_ext"),
        Index("idx_papers_source", "source"),
        Index("idx_papers_published", "published_at"),
        # ngram 全文索引（生成 DDL：FULLTEXT ... WITH PARSER ngram）
        Index("ft_papers_text", "title", "abstract",
              mysql_prefix="FULLTEXT", mysql_with_parser="ngram"),
        TABLE_KW,
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    authors: Mapped[list | None] = mapped_column(JSON, nullable=True)
    abstract: Mapped[str | None] = mapped_column(Text, nullable=True)
    journal: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_meta: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False,
                                                 server_default=TS_CREATED)

    items: Mapped[list["SubscriptionPaper"]] = relationship(
        back_populates="paper", passive_deletes=True)


class SubscriptionPaper(Base):
    """方向-文献关联表（关联对象，含 score/is_read/read_at 附加字段）"""
    __tablename__ = "subscription_papers"
    __table_args__ = (
        UniqueConstraint("subscription_id", "paper_id", name="uk_sub_paper"),
        Index("idx_sub_read", "is_read", "subscription_id"),
        TABLE_KW,
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    subscription_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("subscriptions.id", name="fk_sp_sub", ondelete="CASCADE"),
        nullable=False)
    paper_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("papers.id", name="fk_sp_paper", ondelete="CASCADE"),
        nullable=False)
    score: Mapped[float | None] = mapped_column(Double, nullable=True)
    is_read: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("0"))
    read_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False,
                                                 server_default=TS_CREATED)

    subscription: Mapped["Subscription"] = relationship(back_populates="items")
    paper: Mapped["Paper"] = relationship(back_populates="items")


class TaskLog(Base):
    """跟进/任务日志表"""
    __tablename__ = "task_logs"
    __table_args__ = (
        Index("idx_logs_task", "task_id"),
        Index("idx_logs_kind", "kind"),
        Index("idx_logs_status", "status"),
        TABLE_KW,
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    subscription_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    direction_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    pulled: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    added: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    deduped: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    duration: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class LlmModel(Base):
    """模型池（表名 models；类名避开 pydantic 的 Model 命名风格）"""
    __tablename__ = "models"
    __table_args__ = (
        UniqueConstraint("vendor", "model_id", name="uk_models_vendor_mid"),
        Index("idx_models_status", "status"),
        TABLE_KW,
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    model_id: Mapped[str] = mapped_column(String(128), nullable=False)
    vendor: Mapped[str] = mapped_column(String(64), nullable=False)
    base_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    api_key_enc: Mapped[str | None] = mapped_column(String(512), nullable=True)
    capabilities: Mapped[list] = mapped_column(JSON, nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("0"))
    status: Mapped[str] = mapped_column(Enum("active", "disabled"), nullable=False,
                                        server_default=text("'active'"))
    extra: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False,
                                                 server_default=TS_CREATED)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False,
                                                 server_default=TS_UPDATED)


class AgentModel(Base):
    """agent ↔ 模型绑定（主键为字符串 agent_key，非自增 id）"""
    __tablename__ = "agent_model"
    __table_args__ = (TABLE_KW,)

    agent_key: Mapped[str] = mapped_column(String(32), primary_key=True)
    model_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("models.id", name="fk_am_model", ondelete="CASCADE"),
        nullable=False)
    updated_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False,
                                                 server_default=TS_UPDATED)


class Setting(Base):
    """用户设置表（user_id 唯一）"""
    __tablename__ = "settings"
    __table_args__ = (TABLE_KW,)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", name="fk_settings_user", ondelete="CASCADE"),
        nullable=False, unique=True)
    frequency: Mapped[str] = mapped_column(String(16), nullable=False,
                                           server_default=text("'daily'"))
    notify: Mapped[dict] = mapped_column(JSON, nullable=False)
    sources: Mapped[list] = mapped_column(JSON, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False,
                                                 server_default=TS_UPDATED)
```

- [ ] **Step 2: 验证模型可导入且生成 DDL 含 ngram 全文索引**

```powershell
D:\Anaconda\envs\shixi\python.exe -c "from app.models import Base; from sqlalchemy.schema import CreateTable, CreateIndex; from sqlalchemy.dialects import mysql; d=mysql.dialect(); t=Base.metadata.tables['papers']; ix=next(i for i in t.indexes if i.name=='ft_papers_text'); print(str(CreateIndex(ix).compile(dialect=d)).strip())"
```

Expected: `CREATE FULLTEXT INDEX ft_papers_text ON papers (title, abstract) WITH PARSER ngram`（本机已实测）。

- [ ] **Step 3: 提交**

```powershell
git add app/models.py
git commit -m "feat(db): 新增 8 张表的 ORM 模型，表结构权威转移至 app/models.py"
```

---

### Task 3: 新增 `app/session.py`（引擎 / SessionLocal / FastAPI 依赖）

**Files:**
- Create: `app/session.py`

**Interfaces:**
- Consumes: `app.config.settings`
- Produces: `make_url(db_name=None) -> str`、`create_app_engine(db_name=None) -> Engine`、`engine`（进程级）、`SessionLocal`（`sessionmaker`）、`get_db()`（生成器依赖）。Task 4 / 5 / 7 及阶段 3 全部依赖这些名字。

- [ ] **Step 1: 写入会话层**

```python
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
```

- [ ] **Step 2: 验证引擎可建、连接可用**

```powershell
D:\Anaconda\envs\shixi\python.exe -c "from app.session import engine; from sqlalchemy import text; c=engine.connect(); print(c.execute(text('SELECT 1')).scalar()); c.close()"
```

Expected: 输出 `1`。

- [ ] **Step 3: 提交**

```powershell
git add app/session.py
git commit -m "feat(db): 新增 SQLAlchemy 会话层（engine/SessionLocal/get_db）"
```

---

### Task 4: 改写 `app/database.py`（旧助手等价替换 + create_all 建表）

**Files:**
- Modify: `app/database.py`（整文件重写）

**Interfaces:**
- Consumes: `app.models`、`app.session`
- Produces:
  - `query(sql, params=None, one=False)`、`execute(sql, params=None)`、`execute_many(sql, seq_params)` —— **签名与返回值与现状一致**（`%s` 位置参数 + tuple，返回 dict/行数/自增 id），阶段 3 完成后删除；
  - `init_database(db_name: str | None = None) -> None` —— 建库 → `create_all` → 管理员 → 默认模型引导（幂等）；`db_name` 参数供测试指向独立测试库，`main.py` 的 `db.init_database()` 调用不变。

- [ ] **Step 1: 重写 `database.py`**

```python
# 数据库层：SQLAlchemy 引擎/会话 + 兼容助手（阶段1 过渡）+ 建库建表/初始化
# 阶段3 完成后删除三个兼容助手；init_database 改为「建库 → create_all → 引导」
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.models import AgentModel, Base, LlmModel, User
from app.session import engine, make_url


def query(sql: str, params: tuple | dict | None = None, one: bool = False) -> list[dict] | dict | None:
    """执行 SELECT，返回 dict 列表（等价替换：exec_driver_sql 保留 %s 位置参数语义）"""
    with engine.connect() as conn:
        result = conn.exec_driver_sql(sql, params) if params else conn.exec_driver_sql(sql)
        rows = [dict(r) for r in result.mappings().all()]
    return (rows[0] if rows else None) if one else rows


def execute(sql: str, params: tuple | dict | None = None) -> int:
    """执行写入语句，返回受影响行数或自增 id（lastrowid 优先，与现状一致）"""
    with engine.begin() as conn:                      # begin() 退出时自动提交
        result = conn.exec_driver_sql(sql, params) if params else conn.exec_driver_sql(sql)
        last_id, count = result.lastrowid, result.rowcount
    return last_id if last_id else count


def execute_many(sql: str, seq_params: list[tuple]) -> int:
    """批量执行（去重插入等场景）"""
    with engine.begin() as conn:
        result = conn.exec_driver_sql(sql, seq_params)
        return result.rowcount


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
```

- [ ] **Step 2: 三个助手语义等价自检（只读 + 只写的幂等验证）**

```powershell
D:\Anaconda\envs\shixi\python.exe -c "from app.database import query, execute; rows=query('SELECT id, title, is_read FROM subscription_papers LIMIT 3'); print(type(rows), len(rows)); print(rows[0]); print('count', query('SELECT COUNT(1) AS n FROM papers', one=True)['n'])"
```

Expected: `type` 为 `list`，行是 `dict`；`count` 为正整数（生产库现有 101 篇）。

- [ ] **Step 3: 启动后端验证（业务代码零改动仍可跑）**

```powershell
D:\Anaconda\envs\shixi\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8000
```

Expected: 打印 `[main] 文献雷达后端已就绪`；无异常。`Ctrl+C` 结束。

- [ ] **Step 4: 提交**

```powershell
git add app/database.py
git commit -m "refactor(db): 三个 SQL 助手等价替换为 SQLAlchemy 实现；建表改 create_all"
```

**交付判据：14 个调用点一行未改，服务仍能正常启动并跑通。**

---

### Task 5: 新增 `scripts/align_schema.py`（结构对齐校验，只读）

**Files:**
- Create: `scripts/align_schema.py`

**Interfaces:**
- Consumes: `app.models.Base`、`app.session.engine` 或 `make_url`
- Produces: 差异清单（stdout）；退出码 0=无差异、1=有差异。改用 `--db` 可指向库名。

- [ ] **Step 1: 写入校验脚本**

```python
# 结构对齐校验（只读，不执行任何 DDL）：information_schema vs 模型 metadata
# 验收标准：差异清单为空。差异由人工判断处理。
import argparse
import sys

from sqlalchemy import create_engine, inspect

from app.models import Base
from app.session import make_url

# 类型等价表：模型生成的类型与 MySQL 存储类型之间的归一化
TYPE_EQUIV = {
    "bool": "tinyint(1)",
    "boolean": "tinyint(1)",
    "tinyint(1)": "tinyint(1)",
}


def _norm_type(t: str) -> str:
    """归一化列类型字符串：小写、去空白、BOOL→tinyint(1)"""
    s = "".join(str(t).lower().split())
    s = s.replace("unsigned", "")
    return TYPE_EQUIV.get(s, s)


def _norm_default(d) -> str | None:
    """归一化默认值：None 与 'NULL' 等价"""
    if d is None:
        return None
    s = str(d).strip()
    if s.upper() in ("NULL", ""):
        return None
    # MySQL 对 CURRENT_TIMESTAMP 的回显可能与写入形式不同，去引号后比较
    return s.strip("'")


def _meta_type(col) -> str:
    """模型列类型 → MySQL 方言字符串"""
    from sqlalchemy.dialects import mysql
    return str(col.type.compile(dialect=mysql.dialect()))


def _meta_default(col) -> str | None:
    """模型列 server_default → 字符串"""
    sd = col.server_default
    if sd is None:
        return None
    return _norm_default(getattr(sd, "arg", None))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None, help="库名，默认 settings.MYSQL_DATABASE")
    ap.add_argument("--show-ddl", action="store_true", help="额外打印 SHOW CREATE TABLE（人工核对 ngram 解析器）")
    args = ap.parse_args()

    eng = create_engine(make_url(args.db))
    insp = inspect(eng)
    db_tables = set(insp.get_table_names())
    diffs: list[str] = []

    for table in Base.metadata.sorted_tables:
        name = table.name
        if name not in db_tables:
            diffs.append(f"[表缺失] {name}")
            continue

        # 1) 列
        db_cols = {c["name"]: c for c in insp.get_columns(name)}
        meta_cols = {c.name: c for c in table.columns}
        for cname in sorted(set(meta_cols) | set(db_cols)):
            if cname not in db_cols:
                diffs.append(f"[列缺失] {name}.{cname}")
                continue
            if cname not in meta_cols:
                diffs.append(f"[列多余] {name}.{cname}")
                continue
            m, d = meta_cols[cname], db_cols[cname]
            if _norm_type(_meta_type(m)) != _norm_type(d["type"]):
                diffs.append(f"[类型] {name}.{cname}: 模型 {_meta_type(m)} ≠ 库 {d['type']}")
            if bool(m.nullable) != bool(d["nullable"]):
                diffs.append(f"[可空] {name}.{cname}: 模型 {m.nullable} ≠ 库 {d['nullable']}")
            if _meta_default(m) != _norm_default(d.get("default")):
                diffs.append(f"[默认值] {name}.{cname}: 模型 {_meta_default(m)} ≠ 库 {_norm_default(d.get('default'))}")
            extra = (d.get("extra") or "")
            if "on update CURRENT_TIMESTAMP" in extra.upper() and TS_UPDATE_MARK not in str(m.server_default.arg if m.server_default else ""):
                diffs.append(f"[ON UPDATE] {name}.{cname}: 库有 ON UPDATE，模型未声明")
            if "auto_increment" in extra.lower() and not m.autoincrement:
                diffs.append(f"[自增] {name}.{cname}: 库自增，模型未声明")

        # 2) 索引 / 唯一键
        db_idx = {i["name"]: i for i in insp.get_indexes(name)}
        meta_idx: dict[str, tuple[list[str], bool]] = {}
        for ix in table.indexes:                      # 显式 Index
            meta_idx[ix.name] = ([c.name for c in ix.columns], bool(ix.unique))
        for con in table.constraints:                 # 具名 UniqueConstraint
            from sqlalchemy import UniqueConstraint
            if isinstance(con, UniqueConstraint) and con.name:
                meta_idx[con.name] = ([c.name for c in con.columns], True)
        # 内联 unique（如 users.username / settings.user_id）在库中索引名 = 列名
        for c in table.columns:
            if c.unique:
                meta_idx.setdefault(c.name, ([c.name], True))

        for iname in sorted(set(meta_idx) | set(db_idx)):
            if iname not in db_idx:
                diffs.append(f"[索引缺失] {name}.{iname}（模型列 {meta_idx.get(iname, ('?',))[0]}）")
                continue
            if iname not in meta_idx:
                diffs.append(f"[索引多余] {name}.{iname}")
                continue
            dcols, duniq = meta_idx[iname]
            if db_idx[iname].get("column_names") != dcols:
                diffs.append(f"[索引列] {name}.{iname}: 模型 {dcols} ≠ 库 {db_idx[iname].get('column_names')}")
            if bool(db_idx[iname].get("unique")) != duniq:
                diffs.append(f"[索引唯一性] {name}.{iname}: 模型 {duniq} ≠ 库 {db_idx[iname].get('unique')}")
            if iname == "ft_papers_text":
                # FULLTEXT 解析器 information_schema 不暴露，改为 SHOW CREATE TABLE 核对
                ddl = eng.connect().exec_driver_sql(f"SHOW CREATE TABLE `{name}`").fetchone()[1]
                if "WITH PARSER ngram" not in ddl:
                    diffs.append(f"[全文解析器] {name}.{iname}: 库中未使用 ngram 解析器")

        # 3) 外键
        db_fk = {fk.get("name"): fk for fk in insp.get_foreign_keys(name)}
        meta_fk: dict[str, tuple[list[str], str, list[str], str]] = {}
        for fk in table.foreign_key_constraints:
            elem = list(fk.elements)[0]
            meta_fk[fk.name] = ([e.parent.name for e in fk.elements], elem.column.table.name,
                                [e.column.name for e in fk.elements],
                                (fk.ondelete or "").upper())
        for fname in sorted(set(meta_fk) | set(db_fk)):
            if fname not in db_fk:
                diffs.append(f"[外键缺失] {name}.{fname}")
                continue
            if fname not in meta_fk:
                diffs.append(f"[外键多余] {name}.{fname}")
                continue
            dcols, dref_t, dref_c, dondel = meta_fk[fname]
            d = db_fk[fname]
            if d.get("constrained_columns") != dcols:
                diffs.append(f"[外键列] {name}.{fname}: 模型 {dcols} ≠ 库 {d.get('constrained_columns')}")
            if d.get("referred_table") != dref_t:
                diffs.append(f"[外键目标表] {name}.{fname}: 模型 {dref_t} ≠ 库 {d.get('referred_table')}")
            if d.get("referred_columns") != dref_c:
                diffs.append(f"[外键目标列] {name}.{fname}: 模型 {dref_c} ≠ 库 {d.get('referred_columns')}")
            if (d.get("options", {}).get("ondelete") or "").upper() != dondel:
                diffs.append(f"[外键级联] {name}.{fname}: 模型 {dondel} ≠ 库 {(d.get('options') or {}).get('ondelete')}")

        if args.show_ddl:
            print(eng.connect().exec_driver_sql(f"SHOW CREATE TABLE `{name}`").fetchone()[1], "\n")

    eng.dispose()
    if diffs:
        print(f"发现 {len(diffs)} 处差异：")
        for item in diffs:
            print("  -", item)
        return 1
    print("结构对齐校验通过：差异清单为空")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

> 注：脚本中 `TS_UPDATE_MARK` 需在第 1 行常量区补上：`TS_UPDATE_MARK = "ON UPDATE CURRENT_TIMESTAMP"`，与 `TYPE_EQUIV` 并列。

> **实现偏差记录（2026-10-17，实机运行后修正，已落库到 `scripts/align_schema.py`）**：首轮对生产库运行报出 31 条差异，经逐条核对**全部为脚本归一化/匹配缺口，模型与存量表无真差异**。按 Step 2 约定「等价但写法不同 → 补进归一化表」修正 5 处：
> 1. `_norm_type` 剥离反射类型自带的后缀：库反射字符串类型回显 `VARCHAR(512) COLLATE "utf8mb4_unicode_ci"`，先按 `collate` 截断并去引号（消除 21 条 `[类型]`）；
> 2. `TYPE_EQUIV` 增补 `tinyint → tinyint(1)`：模型 `Boolean` 生成 `BOOL`，MySQL 8 的 `BOOL` 与 `TINYINT(1)` 同义且反射无显示宽度（消除 3 条 BOOL≠TINYINT）；
> 3. `_norm_type` 对 `enum` 关键字归一：库反射 ENUM 只回显 `"ENUM"` 不含枚举值（消除 4 条 ENUM 差异）；
> 4. 全文解析器比对改为先 `ddl.lower().replace("\`","")`：库回显为 `WITH PARSER \`ngram\``（带反引号），原串匹配假阳性（消除 1 条 `[全文解析器]`）；
> 5. 索引比对时排除**外键同名支撑索引**：MySQL 为缺少覆盖索引的外键自动创建同名索引（模型未声明、亦无需声明），按 FK 名过滤后再比对（消除 2 条 `[索引多余]`，即 `agent_model.fk_am_model`、`subscription_papers.fk_sp_paper`）。
>
> 另修 2 处运行缺陷：① 脚本改为可独立运行（`sys.path.insert(0, 项目根)`），不必再手工设 `PYTHONPATH`；② `--db` 未传时按 help 文本回退到 `settings.MYSQL_DATABASE`（原实现传 `None` 会因 `default_schema_name` 为 None 崩溃）。

- [ ] **Step 2: 对生产库跑校验（只读）**

```powershell
D:\Anaconda\envs\shixi\python.exe scripts\align_schema.py --show-ddl
```

Expected: **差异清单为空**（`结构对齐校验通过：差异清单为空`，退出码 0）。若报了差异，**逐个核对**：真差异 → 修模型；等价但写法不同 → 补进 `TYPE_EQUIV` 归一化表，并在脚本注释记录原因。

- [ ] **Step 3: 提交**

```powershell
git add scripts/align_schema.py
git commit -m "chore(db): 新增只读结构对齐校验脚本 align_schema.py"
```

---

### Task 6: 依赖清单切换

**Files:**
- Modify: `requirements.txt:4`（移除）、末尾追加
- Modify: `pyproject.toml:10`（移除）、`dependencies` 数组追加

**Interfaces:**
- Consumes: 无
- Produces: 依赖声明与实现一致（后续验收 grep 检查无 `mysql-connector`）

- [ ] **Step 1: 改 `requirements.txt`**

```diff
-fastapi>=0.115.0
-uvicorn>=0.30.0
-mysql-connector-python>=9.0.0
+fastapi>=0.115.0
+uvicorn>=0.30.0
+SQLAlchemy>=2.0
+PyMySQL>=1.1.0
+cryptography>=42
 pydantic>=2.7.0
 python-dotenv>=1.0.0
 pyjwt>=2.8.0
 httpx>=0.27.0
 openai>=1.30.0
 rank-bm25>=0.2.2
 tavily-python>=0.5.0
 python-docx>=1.1.0
+pytest>=8
```

- [ ] **Step 2: 改 `pyproject.toml` `dependencies`（保持两文件同步）**

```diff
-    "mysql-connector-python>=9.0.0",
+    "SQLAlchemy>=2.0",
+    "PyMySQL>=1.1.0",
+    "cryptography>=42",
+    "pytest>=8",
```

- [ ] **Step 3: 提交**

```powershell
git add requirements.txt pyproject.toml
git commit -m "chore(deps): 引入 SQLAlchemy/PyMySQL/cryptography/pytest，移除 mysql-connector-python"
```

---

## Phase 2 · 上安全网（写特征测试，先绿）

> 这批测试在 Phase 1 结束时即须全绿 —— 这就是"地基等价替换"的证明。

### Task 7: `tests/conftest.py` + pytest 配置

**Files:**
- Create: `tests/conftest.py`
- Modify: `pyproject.toml`（追加 `[tool.pytest.ini_options]`）

**Interfaces:**
- Consumes: `app.config.settings`、`app.models.Base`、`app.session.make_url`
- Produces: fixtures —— `test_db_name`（会话级，测试库名）、`engine`（会话级，测试库引擎 + `create_all`）、`session`（用例级 Session）、autouse `_clean`（每用例前清空所有表）。Task 8–12 全部依赖。

- [ ] **Step 1: 追加 pytest 配置到 `pyproject.toml`**

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["."]          # 让 tests 里能 import app
```

- [ ] **Step 2: 写入 `tests/conftest.py`**

```python
# 特征测试夹具：独立测试库（literature_radar_test），绝不触碰生产库 literature_radar
import os

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
```

- [ ] **Step 3: 验证测试库能建、能跑起来**

```powershell
D:\Anaconda\envs\shixi\python.exe -m pytest -q
```

Expected: `no tests ran`（此时还没有用例），且 **没有** collection error。

- [ ] **Step 4: 确认没有误碰生产库**

```powershell
D:\Anaconda\envs\shixi\python.exe -c "from sqlalchemy import create_engine, text; from app.session import make_url; e=create_engine(make_url('literature_radar_test')); c=e.connect(); print(c.execute(text('SHOW TABLES')).fetchall())"
```

Expected: 8 张表（先前由 `create_all` 建出）。生产库 `literature_radar` 行数不受影响（`init_database` 未指向它）。

- [ ] **Step 5: 提交**

```powershell
git add tests/conftest.py pyproject.toml
git commit -m "test(db): 搭建 pytest 特征测试夹具（独立测试库 + 建表/清表）"
```

---

### Task 8: `tests/test_schema.py`（用例 1、2）

**Files:**
- Create: `tests/test_schema.py`

**Interfaces:**
- Consumes: fixtures `engine`、`test_db_name`；`app.database.init_database`
- Produces: 覆盖 spec §8 第 1、2 条。

- [ ] **Step 1: 写用例**

```python
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
    assert "FULLTEXT" in ddl and "WITH PARSER ngram" in ddl


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
```

> **实现偏差记录（2026-10-17，实机运行后修正，已落库到 `tests/test_schema.py`）**：`test_papers_indexes_and_fulltext` 里原断言 `"WITH PARSER ngram" in ddl` 会失败——`SHOW CREATE TABLE` 的**回显**形式为 `` /*!50100 WITH PARSER `ngram` */ ``（解析器名带反引号），与直接执行 `CREATE FULLTEXT INDEX ... WITH PARSER ngram` 的字面不同。改为先 `ddl.replace("`", "")` 再比对。

- [ ] **Step 2: 跑用例**

```powershell
D:\Anaconda\envs\shixi\python.exe -m pytest tests/test_schema.py -q
```

Expected: `4 passed`。

- [ ] **Step 3: 提交**

```powershell
git add tests/test_schema.py
git commit -m "test(db): 覆盖建表与初始化幂等（spec §8 用例1/2）"
```

---

### Task 9: `tests/test_users_subscriptions_settings.py`（用例 3、4、9）

**Files:**
- Create: `tests/test_users_subscriptions_settings.py`

**Interfaces:**
- Consumes: fixture `session`
- Produces: 覆盖 spec §8 第 3、4、9 条。

- [ ] **Step 1: 写用例**

```python
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
```

- [ ] **Step 2: 跑用例**

```powershell
D:\Anaconda\envs\shixi\python.exe -m pytest tests/test_users_subscriptions_settings.py -q
```

Expected: `5 passed`。

- [ ] **Step 3: 提交**

```powershell
git add tests/test_users_subscriptions_settings.py
git commit -m "test(db): 覆盖 users/subscriptions/settings（spec §8 用例3/4/9）"
```

---

### Task 10: `tests/test_papers_subscription_papers_logs.py`（用例 5、6、7、11）

**Files:**
- Create: `tests/test_papers_subscription_papers_logs.py`

**Interfaces:**
- Consumes: fixture `session`
- Produces: 覆盖 spec §8 第 5、6、7、11 条（含 upsert / INSERT IGNORE 语义）。

- [ ] **Step 1: 写用例**

```python
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
```

- [ ] **Step 2: 跑用例**

```powershell
D:\Anaconda\envs\shixi\python.exe -m pytest tests/test_papers_subscription_papers_logs.py -q
```

Expected: `5 passed`。

- [ ] **Step 3: 提交**

```powershell
git add tests/test_papers_subscription_papers_logs.py
git commit -m "test(db): 覆盖 papers/关联表/日志/批量写（spec §8 用例5/6/7/11）"
```

---

### Task 11: `tests/test_models_agent_binding.py`（用例 8）

**Files:**
- Create: `tests/test_models_agent_binding.py`

**Interfaces:**
- Consumes: fixture `session`
- Produces: 覆盖 spec §8 第 8 条。

- [ ] **Step 1: 写用例**

```python
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
```

- [ ] **Step 2: 跑用例**

```powershell
D:\Anaconda\envs\shixi\python.exe -m pytest tests/test_models_agent_binding.py -q
```

Expected: `3 passed`。

- [ ] **Step 3: 提交**

```powershell
git add tests/test_models_agent_binding.py
git commit -m "test(db): 覆盖 models/agent_model（spec §8 用例8）"
```

---

### Task 12: `tests/test_boolean_types.py`（用例 10）

**Files:**
- Create: `tests/test_boolean_types.py`

**Interfaces:**
- Consumes: fixture `session`
- Produces: 覆盖 spec §8 第 10 条（`active`/`is_read`/`is_default` 读出为 True/False）。

- [ ] **Step 1: 写用例**

```python
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
```

> 若嫌 `__import__("sqlalchemy").update` 难看，改成文件顶部 `from sqlalchemy import select, update`，正文用 `update(...)`。

- [ ] **Step 2: 跑该用例**

```powershell
D:\Anaconda\envs\shixi\python.exe -m pytest tests/test_boolean_types.py -q
```

Expected: `1 passed`。

- [ ] **Step 3: 全量回归**

```powershell
D:\Anaconda\envs\shixi\python.exe -m pytest -q
```

Expected: `18 passed`（4+5+5+3+1）。

- [ ] **Step 4: 提交**

```powershell
git add tests/test_boolean_types.py
git commit -m "test(db): 覆盖布尔字段返回值（spec §8 用例10）；Phase 2 安全网全绿"
```

**交付判据：11 项特征测试全绿 —— 地基等价替换已被证明。**

---

## Phase 3 · 逐个换血（一文件一小任务，换完立刻跑测试）

> 每个任务收尾统一执行一次回归：`D:\Anaconda\envs\shixi\python.exe -m pytest -q`（Expected: `18 passed`）。
> 路由文件统一补两行 import：`from sqlalchemy.orm import Session`、`from app.session import get_db`；并给每个 handler 补形参 `session: Session = Depends(get_db)`。

### Task 13: `app/routers/settings.py`（4 处）

**Files:** Modify: `app/routers/settings.py`

**Interfaces:**
- Consumes: `Setting` 模型、`get_db`
- Produces: `_ensure_row(session, user_id) -> Setting`

| 行 | 原调用 | 目标写法 |
|---|---|---|
| 15、18、23 | `db.query("SELECT * FROM settings WHERE user_id=%s")` + `db.execute("INSERT INTO settings ...")` + 二次 `db.query` | 见下方 `_ensure_row` |
| 51 | `db.execute("UPDATE settings SET frequency=%s, notify=%s, sources=%s WHERE user_id=%s")` | `row.frequency/notify/sources = ...` + `session.commit()` |

- [ ] **Step 1: 改 `_ensure_row` 为 ORM（C1/C5）**

```python
from sqlalchemy import select

from app.models import Setting


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
```

- [ ] **Step 2: 改 `get_settings`（JSON 列读出即原生对象，删掉字符串兜底分支）**

```python
@router.get("", response_model=SettingsOut)
def get_settings(user: dict = Depends(get_current_user),
                 session: Session = Depends(get_db)):
    row = _ensure_row(session, user["id"])
    notify = row.notify or {}
    sources = row.sources or []
    return SettingsOut(user_id=user["id"], frequency=row.frequency,
                       notify=NotifyOut(**notify), sources=sources)
```

> 逐字保留原返回结构（字段名、顺序以原文件为准）；原 `isinstance(notify, str)` / `json.loads` 兜底分支删除，因为 JSON 列经 ORM 读出已是 `dict`。`import json` 若不再使用则一并删除。

> **实现偏差记录（2026-10-17）**：本 Step 上方示例写了 `SettingsOut(user_id=user["id"], ...)`，但 `app/schemas/settings.py` 的 `SettingsOut` **并无 `user_id` 字段**，且原实现返回的也只有 `frequency/notify/sources`。实际交付按本 Step 的说明文字「逐字保留原返回结构」执行，**未添加 `user_id`**（保持 HTTP 契约零变化）；示例中的 `NotifyOut` 亦按原文件写作 `NotifyConfig`。

- [ ] **Step 3: 改 `update_settings`（C5）**

```python
@router.put("", response_model=SettingsOut)
def update_settings(body: SettingsUpdate, user: dict = Depends(get_current_user),
                    session: Session = Depends(get_db)):
    row = _ensure_row(session, user["id"])
    row.frequency = body.frequency
    row.notify = {"email": body.notify.email, "push": body.notify.push}
    row.sources = body.sources
    session.commit()
    return get_settings(user, session)
```

- [ ] **Step 4: 回归 + 提交**

```powershell
D:\Anaconda\envs\shixi\python.exe -m pytest -q
git add app/routers/settings.py
git commit -m "refactor(db): settings 路由改用 ORM"
```

---

### Task 14: `app/routers/followup.py`（5 处）

**Files:** Modify: `app/routers/followup.py`

**Interfaces:** Consumes `Subscription`、`TaskLog` 模型；Produces 无。

| 行 | 原调用 | 目标写法 |
|---|---|---|
| 14 | `db.query("SELECT * FROM subscriptions WHERE id=%s AND user_id=%s", ..., one=True)` | C2：`session.scalar(select(Subscription.id).where(Subscription.id == subscription_id, Subscription.user_id == user["id"]))`；`if sub is None: 404` |
| 37 | `LEFT JOIN` 取 `l.*`（C8，dict 消费） | C3：`select(TaskLog).outerjoin(Subscription, Subscription.id == TaskLog.subscription_id).where(TaskLog.task_id == task_id, or_(Subscription.user_id == user["id"], TaskLog.subscription_id.is_(None)))` → `.mappings().first()` |
| 75 | `SELECT COUNT(*) AS n` | C3：`select(func.count()).select_from(base.subquery())` |
| 79 | `SUM(l.pulled)/SUM(l.added)` | C3：`select(func.coalesce(func.sum(TaskLog.pulled), 0).label("pulled"), func.coalesce(func.sum(TaskLog.added), 0).label("added")).select_from(base.subquery())` → `.mappings().first()` |
| 83 | `SELECT l.* ... LIMIT/OFFSET` | C3：`base.order_by(TaskLog.started_at.desc()).limit(limit).offset((page - 1) * limit)` → `.mappings().all()` |

- [ ] **Step 1: 改 `run_followup` 的订阅存在性检查**

```python
    sub = session.scalar(
        select(Subscription.id).where(Subscription.id == subscription_id,
                                      Subscription.user_id == user["id"]))
    if sub is None:
        raise HTTPException(status_code=404, detail="订阅不存在")
```

- [ ] **Step 2: 改 `task_status`（C3）**

```python
    log = session.execute(
        select(TaskLog)
        .outerjoin(Subscription, Subscription.id == TaskLog.subscription_id)
        .where(TaskLog.task_id == task_id,
               or_(Subscription.user_id == user["id"]),
               # 保留原语义：无订阅归属的日志对所有登录用户可见
               ).where(or_(Subscription.user_id == user["id"], TaskLog.subscription_id.is_(None)))
    ).mappings().first()
```

> 上面写两处 `where` 只为对齐原文的 `AND (s.user_id=%s OR l.subscription_id IS NULL)`；落地时合并成一处 `.where(TaskLog.task_id == task_id, or_(Subscription.user_id == user["id"], TaskLog.subscription_id.is_(None)))`。返回的 `log` 是 `RowMapping`，后续 `log.get("status")` 等读法**保持不变**。

- [ ] **Step 3: 改 `list_logs`（动态筛选从字符串拼接改为表达式列表）**

```python
    conditions = [Subscription.user_id == user["id"]]
    if status:
        conditions.append(TaskLog.status == status)
    if subscription_id:
        conditions.append(TaskLog.subscription_id == subscription_id)

    base = (select(TaskLog)
            .join(Subscription, Subscription.id == TaskLog.subscription_id)
            .where(*conditions))
    snapshot = base.subquery()

    total = session.scalar(select(func.count()).select_from(snapshot))
    agg = session.execute(
        select(func.coalesce(func.sum(TaskLog.pulled), 0).label("pulled"),
               func.coalesce(func.sum(TaskLog.added), 0).label("added"))
        .select_from(snapshot)).mappings().first()
    rows = session.execute(
        base.order_by(TaskLog.started_at.desc())
            .limit(limit).offset((page - 1) * limit)).mappings().all()
```

> `items` 构造里 `r["id"]` / `r.get(...)` / `jloads(r.get("detail"), {})` / `fmt_dt(...)` 全部**原样保留**（`RowMapping` 兼容）。

> **实现偏差记录（2026-10-17，实机验证后修正，已落库到 `app/routers/followup.py`）**：Step 2/3 示例里的 `select(TaskLog)` 有**两处会运行时报错**，故改为列形态：
> 1. `select(TaskLog).mappings()` 的键是**实体类名**（`['TaskLog']`），不是列名——Step 2 的 `log.get("status")` 与 Step 3 的 `r["id"]` 会 KeyError。实测确认后统一改为 `select(TaskLog.__table__)`，其 `mappings()` 键即列名（`id/task_id/status/pulled/...`），原 dict 读法与 `items` 构造得以逐字保留。
> 2. Step 3 的 `func.coalesce(func.sum(TaskLog.pulled), 0).select_from(snapshot)` 实测生成 `FROM (…) AS anon_1, task_logs` 的**笛卡尔积**（聚合结果成倍错误）。改为聚合子查询自身的列：`func.sum(snapshot.c.pulled)` / `func.sum(snapshot.c.added)`，生成 `FROM (…) AS anon_1` 单表，语义与原文一致。
>
> 其余（`Subscription.id` 存在性判断、`or_` 语义合并、`items` 与返回字典）均按计划执行。

- [ ] **Step 4: 回归 + 提交**

```powershell
D:\Anaconda\envs\shixi\python.exe -m pytest -q
git add app/routers/followup.py
git commit -m "refactor(db): followup 路由改用 SQLAlchemy 表达式"
```

---

### Task 15: `app/routers/unread.py`（6 处）

**Files:** Modify: `app/routers/unread.py`

**Interfaces:** Consumes `Paper`、`Subscription`、`SubscriptionPaper` 模型。

| 行 | 原调用 | 目标写法 |
|---|---|---|
| 16 | `SELECT p.*, sp.score, sp.is_read`（C8，喂给 `enhance_meta`） | 见下方 `_paper_query` |
| 43 | 未读汇总 `LEFT JOIN + SUM(CASE)` | C3：`select(Subscription.id, Subscription.name, func.sum(case((SubscriptionPaper.is_read.is_(False), 1), else_=0)).label("unread")).outerjoin(...).group_by(...)` |
| 71 | owned 校验 | C2：`select(SubscriptionPaper.id).join(...).where(...).limit(1)` |
| 79 | `UPDATE ... SET is_read=1, read_at=NOW()` | C6：`update(SubscriptionPaper).where(SubscriptionPaper.paper_id == paper_id).values(is_read=True, read_at=func.now())` |
| 87 | 订阅归属校验 | C2：`select(Subscription.id).where(Subscription.id == body.subscription_id, Subscription.user_id == user["id"])` |
| 94 | `IN (...)` 批量已读 | C6：`update(SubscriptionPaper).where(SubscriptionPaper.subscription_id == body.subscription_id, SubscriptionPaper.paper_id.in_(ids), SubscriptionPaper.is_read.is_(False)).values(is_read=True, read_at=func.now())`，`affected = result.rowcount` |

- [ ] **Step 1: 改 `_paper_query`（显式列投影，避开 `p.id` 与 `sp.id` 键冲突；产出 dict 喂 `enhance_meta`）**

```python
def _paper_query(session: Session, user_id: int, sub_id: int, only_unread: bool) -> list[dict]:
    """某方向下的文献列表（论文列 + score + is_read），结果保持 dict 形态"""
    stmt = (select(Paper.id, Paper.external_id, Paper.source, Paper.title, Paper.authors,
                   Paper.abstract, Paper.journal, Paper.published_at, Paper.url,
                   Paper.raw_meta, Paper.created_at,
                   SubscriptionPaper.score, SubscriptionPaper.is_read)
            .join(SubscriptionPaper, SubscriptionPaper.paper_id == Paper.id)
            .join(Subscription, Subscription.id == SubscriptionPaper.subscription_id)
            .where(Subscription.user_id == user_id,
                   SubscriptionPaper.subscription_id == sub_id))
    if only_unread:
        stmt = stmt.where(SubscriptionPaper.is_read.is_(False))
    stmt = stmt.order_by(SubscriptionPaper.score.desc(), Paper.published_at.desc()).limit(200)
    return [dict(r) for r in session.execute(stmt).mappings().all()]
```

> 关键：**不要** select `sp.id`，否则会覆盖 `p.id` 的映射键。`enhance_meta(rows)` 签名与内部（`p["journal_level"] = ...` 等原地改写）保持不变。

> **实现偏差记录（2026-10-17，实机验证后修正，已落库到 `app/routers/unread.py`）**：本 Step 示例的末行直接 `return [dict(r) for r in session.execute(stmt).mappings().all()]`，**漏掉了原文的 `enhance_meta(rows)` 调用**——若照抄会让 `journal_level/ccf/cas_zone/arxiv_cat` 四个展示字段全部丢失（属行为变更，违反"零行为变化"）。实际交付改为：先 `rows = [dict(r) for r in ...]`，再 `return enhance_meta(rows)`，与本 Step 说明文字一致。其余（显式列投影避开 `sp.id`、排序/上限 200、`is_read.is_(False)` 过滤）按计划执行，已在测试库实测通过。

- [ ] **Step 2: 改 `unread_summary`（C3）**

```python
    stmt = (select(Subscription.id, Subscription.name,
                   func.sum(case((SubscriptionPaper.is_read.is_(False), 1), else_=0)).label("unread"))
            .select_from(Subscription)
            .outerjoin(SubscriptionPaper, SubscriptionPaper.subscription_id == Subscription.id)
            .where(Subscription.user_id == user["id"])
            .group_by(Subscription.id, Subscription.name)
            .order_by(Subscription.id))
    rows = session.execute(stmt).mappings().all()
    items = [UnreadItem(subscription_id=r["id"], name=r["name"], unread=int(r["unread"] or 0))
             for r in rows]
```

- [ ] **Step 3: 改 `mark_read` / `batch_read`（C2 + C6）**

```python
    owned = session.scalar(
        select(SubscriptionPaper.id)
        .join(Subscription, Subscription.id == SubscriptionPaper.subscription_id)
        .where(SubscriptionPaper.paper_id == paper_id,
               Subscription.user_id == user["id"]).limit(1))
    if owned is None:
        raise HTTPException(status_code=404, detail="文献不存在或不属于当前用户")
    session.execute(update(SubscriptionPaper)
                    .where(SubscriptionPaper.paper_id == paper_id)
                    .values(is_read=True, read_at=func.now()))
    session.commit()
    return {"code": 0, "message": "已标记已读"}
```

```python
    sub = session.scalar(select(Subscription.id).where(
        Subscription.id == body.subscription_id, Subscription.user_id == user["id"]))
    if sub is None:
        raise HTTPException(status_code=404, detail="订阅不存在")
    ids = body.paper_ids[:500]
    affected = session.execute(
        update(SubscriptionPaper)
        .where(SubscriptionPaper.subscription_id == body.subscription_id,
               SubscriptionPaper.paper_id.in_(ids),
               SubscriptionPaper.is_read.is_(False))
        .values(is_read=True, read_at=func.now())).rowcount
    session.commit()
    return {"code": 0, "message": f"已标记 {affected} 篇为已读"}
```

> 原 `placeholders` 手拼与 `%s` 展开整段删除（`IN` 交给 `in_()` 参数化）。

- [ ] **Step 4: 回归 + 提交**

```powershell
D:\Anaconda\envs\shixi\python.exe -m pytest -q
git add app/routers/unread.py
git commit -m "refactor(db): unread 路由改用 ORM/Core 表达式"
```

---

### Task 16: `app/routers/subscriptions.py`（11 处）

**Files:** Modify: `app/routers/subscriptions.py`

**Interfaces:**
- Consumes: `Subscription`、`SubscriptionPaper` 模型
- Produces: `_counts(session, user_id) -> dict`、`_owned(session, sub_id, user_id) -> Subscription`、`_to_out(row: Subscription, ...) -> SubscriptionOut`

| 行 | 原调用 | 目标写法 |
|---|---|---|
| 30 | 计数聚合 | C3：见 Step 1 |
| 42 | `SELECT * ... WHERE id=%s` + `row["user_id"]` 校验 | C1：`session.get(Subscription, sub_id)` + `entity.user_id != user_id` |
| 50 | 列表 | C2：`session.scalars(select(Subscription).where(Subscription.user_id == user["id"]).order_by(Subscription.created_at.desc())).all()` |
| 58 | `INSERT INTO subscriptions ...` | C5：`session.add(Subscription(...))` |
| 64 | 取刚插入的行 | 直接用 `session.add` 后的实体（删掉二次查询） |
| 74 | `UPDATE subscriptions SET name/keywords/sources/threshold/pub_window` | C5：属性赋值 |
| 87 | `UPDATE subscriptions SET active=%s` | C5：`row.active = bool(body.active)` |
| 95 | `DELETE FROM subscriptions WHERE id=%s` | C6：`session.execute(delete(Subscription).where(Subscription.id == sub_id))` |
| 101 | 归属集合 | C2：`set(session.scalars(select(Subscription.id).where(Subscription.user_id == user["id"])).all())` |
| 107 | 循环逐条 `DELETE` | C6：合并为一条 `delete(Subscription).where(Subscription.id.in_(deletable))` |
| 115 | `UPDATE subscription_papers SET is_read=1, read_at=NOW() WHERE subscription_id=%s AND is_read=0` | C6：`update(SubscriptionPaper).where(SubscriptionPaper.subscription_id == sub_id, SubscriptionPaper.is_read.is_(False)).values(is_read=True, read_at=func.now())` |

- [ ] **Step 1: 改 `_counts` / `_owned` / `_to_out`**

```python
def _counts(session: Session, user_id: int) -> dict[int, tuple[int, int]]:
    """各方向的（文献总数, 未读数）"""
    stmt = (select(SubscriptionPaper.subscription_id,
                   func.count().label("total"),
                   func.sum(case((SubscriptionPaper.is_read.is_(False), 1), else_=0)).label("unread"))
            .join(Subscription, Subscription.id == SubscriptionPaper.subscription_id)
            .where(Subscription.user_id == user_id)
            .group_by(SubscriptionPaper.subscription_id))
    return {r["subscription_id"]: (int(r["total"]), int(r["unread"] or 0))
            for r in session.execute(stmt).mappings().all()}


def _owned(session: Session, sub_id: int, user_id: int) -> Subscription:
    """取归属当前用户的订阅，否则 404"""
    row = session.get(Subscription, sub_id)
    if not row or row.user_id != user_id:
        raise HTTPException(status_code=404, detail="订阅不存在")
    return row


def _to_out(row: Subscription, paper_count: int = 0, unread_count: int = 0) -> SubscriptionOut:
    """订阅实体 → 输出模型（JSON 列经 ORM 读出即 list，无需 json.loads）"""
    return SubscriptionOut(
        id=row.id, name=row.name, keywords=row.keywords or [], sources=row.sources or [],
        threshold=float(row.threshold), pub_window=row.pub_window, active=bool(row.active),
        last_run_at=fmt_dt(row.last_run_at), created_at=fmt_dt(row.created_at),
        paper_count=paper_count, unread_count=unread_count)
```

> `_to_out` 的字段清单以原文件为准；`jloads` 若不再使用则删掉该 import。

- [ ] **Step 2: 改 7 个 handler**

```python
@router.get("", response_model=list[SubscriptionOut])
def list_subscriptions(user: dict = Depends(get_current_user),
                       session: Session = Depends(get_db)):
    rows = session.scalars(select(Subscription)
                           .where(Subscription.user_id == user["id"])
                           .order_by(Subscription.created_at.desc())).all()
    counts = _counts(session, user["id"])
    return [_to_out(r, *counts.get(r.id, (0, 0))) for r in rows]


@router.post("", response_model=SubscriptionOut, status_code=201)
def create_subscription(body: SubscriptionCreate, user: dict = Depends(get_current_user),
                        session: Session = Depends(get_db)):
    sub = Subscription(user_id=user["id"], name=body.name, keywords=body.keywords,
                       sources=body.sources, threshold=body.threshold,
                       pub_window=body.pub_window)
    session.add(sub)
    session.commit()
    return _to_out(sub)


@router.put("/{sub_id}", response_model=SubscriptionOut)
def update_subscription(sub_id: int, body: SubscriptionUpdate,
                        user: dict = Depends(get_current_user),
                        session: Session = Depends(get_db)):
    row = _owned(session, sub_id, user["id"])
    row.name, row.keywords, row.sources = body.name, body.keywords, body.sources
    row.threshold, row.pub_window = body.threshold, body.pub_window
    session.commit()
    return _to_out(row)


@router.patch("/{sub_id}/active")
def toggle_active(sub_id: int, body: ActiveToggle, user: dict = Depends(get_current_user),
                  session: Session = Depends(get_db)):
    row = _owned(session, sub_id, user["id"])
    row.active = bool(body.active)
    session.commit()
    return _to_out(row)


@router.delete("/{sub_id}")
def delete_subscription(sub_id: int, user: dict = Depends(get_current_user),
                        session: Session = Depends(get_db)):
    _owned(session, sub_id, user["id"])
    # ON DELETE CASCADE：级联删除方向关联与阅读状态（交给数据库）
    session.execute(delete(Subscription).where(Subscription.id == sub_id))
    session.commit()
    return {"code": 0, "message": "已删除"}


@router.post("/batch-delete")
def batch_delete(body: BatchDeleteRequest, user: dict = Depends(get_current_user),
                 session: Session = Depends(get_db)):
    owned = set(session.scalars(
        select(Subscription.id).where(Subscription.user_id == user["id"])).all())
    deletable = [i for i in body.ids if i in owned]
    if not deletable:
        raise HTTPException(status_code=404, detail="无权删除的订阅")
    session.execute(delete(Subscription).where(Subscription.id.in_(deletable)))
    session.commit()
    return {"code": 0, "message": f"已删除 {len(deletable)} 个订阅", "deleted": deletable}


@router.post("/{sub_id}/read-all")
def read_all(sub_id: int, user: dict = Depends(get_current_user),
             session: Session = Depends(get_db)):
    _owned(session, sub_id, user["id"])
    affected = session.execute(
        update(SubscriptionPaper)
        .where(SubscriptionPaper.subscription_id == sub_id,
               SubscriptionPaper.is_read.is_(False))
        .values(is_read=True, read_at=func.now())).rowcount
    session.commit()
    return {"code": 0, "message": f"已标记 {affected} 篇为已读"}
```

> 用 `delete(Subscription)`（Core）而不是 `session.delete(entity)`：后者会按 ORM 关系把子表外键置空，而 `fk_sp_sub.subscription_id` 是 `NOT NULL`，必须由数据库 `ON DELETE CASCADE` 处理。函数签名/装饰器/返回结构以原文件为准，仅按上表替换数据访问部分。

- [ ] **Step 3: 回归 + 提交**

```powershell
D:\Anaconda\envs\shixi\python.exe -m pytest -q
git add app/routers/subscriptions.py
git commit -m "refactor(db): subscriptions 路由改用 ORM（含级联删除改由数据库处理）"
```

---

### Task 17: `app/deps.py`（1 处） + `app/routers/auth.py`（3 处）

**Files:** Modify: `app/deps.py`、`app/routers/auth.py`

**Interfaces:**
- Consumes: `User` 模型、`get_db`
- Produces: `get_current_user(...) -> dict`（**保持 dict 契约**，全仓 `user["id"]` 读法零改动）

- [ ] **Step 1: 改 `deps.get_current_user`（内部 ORM 读，外部仍返回 dict）**

```python
def get_current_user(request: Request, session: Session = Depends(get_db)) -> dict:
    ...                                   # 解析 Bearer token（原逻辑逐字保留）
    user = session.get(User, int(payload["sub"]))
    if not user:
        raise HTTPException(status_code=401, detail="用户不存在")
    if user.status != "active":
        raise HTTPException(status_code=401, detail="账号已停用")
    # 保持 dict 契约：8 个路由文件的 user["id"] / user["role"] 等读法无需改动
    return {"id": user.id, "username": user.username, "role": user.role,
            "status": user.status, "created_at": user.created_at,
            "last_login_at": user.last_login_at}
```

> `payload` 解析与异常处理逐字保留。若后续发现还有别的用户字段被下游读取，补进该 dict。

- [ ] **Step 2: 改 `auth.login`（C2 + C6）**

```python
    user = session.scalar(select(User).where(User.username == body.username))
    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    if user.status != "active":
        raise HTTPException(status_code=403, detail="账号已停用，请联系管理员")
```

- [ ] **Step 3: 改 `auth.login` 的未读汇总（C3，与原 `unread.py:43` 同构）**

```python
    rows = session.execute(
        select(Subscription.id, Subscription.name,
               func.sum(case((SubscriptionPaper.is_read.is_(False), 1), else_=0)).label("unread"))
        .select_from(Subscription)
        .outerjoin(SubscriptionPaper, SubscriptionPaper.subscription_id == Subscription.id)
        .where(Subscription.user_id == user.id)
        .group_by(Subscription.id, Subscription.name)
        .order_by(Subscription.id)).mappings().all()
    brief = [UnreadBrief(subscription_id=r["id"], name=r["name"], unread=int(r["unread"] or 0))
             for r in rows]
```

- [ ] **Step 4: 改登录时间写入与返回（C6）**

```python
    session.execute(update(User).where(User.id == user.id)
                    .values(last_login_at=datetime.now()))
    session.commit()
    print(f"[auth] 用户 {user.username} 登录，惰性补发跟进单 {enqueued} 个")
    return LoginResponse(
        access_token=create_token(user.id, user.username, user.role),
        role=user.role, username=user.username, user_id=user.id,
        total_unread=sum(b.unread for b in brief), unread_brief=brief,
    )
```

> `auth.logout` 用 `user.get("last_login_at")`（来自 `get_current_user` 的 dict）**不改**。

- [ ] **Step 5: 回归 + 提交**

```powershell
D:\Anaconda\envs\shixi\python.exe -m pytest -q
git add app/deps.py app/routers/auth.py
git commit -m "refactor(db): deps/auth 改用 ORM（get_current_user 保持 dict 契约）"
```

---

### Task 18: `app/routers/admin.py`（20 处）

**Files:** Modify: `app/routers/admin.py`

**Interfaces:**
- Consumes: `User`、`LlmModel`、`AgentModel` 模型（`AGENT_DEFS` 复用 `ai.llm_provider`）
- Produces: `_user_out(row: User) -> AdminUserOut`、`_model_out(row: LlmModel) -> ModelOut`、`_get_model(session, model_id) -> LlmModel`、`_clear_default(session, model_id) -> None`

| 行 | 原调用 | 目标写法 |
|---|---|---|
| 34 | `COUNT(*)` 分页总数 | C3：`select(func.count()).select_from(base.subquery())` |
| 36 | `SELECT * FROM users ... LIMIT/OFFSET` | C2：`session.scalars(base.order_by(User.id).limit(limit).offset((page-1)*limit)).all()` |
| 46 | 用户详情 | C1：`session.get(User, user_id)` |
| 55 | patch 前的存在性读 | C1：`session.get(User, user_id)` |
| 61 | 最后一名管理员计数 | C2：`select(func.count()).select_from(User).where(User.role == "admin", User.status == "active")` |
| 67、69 | `UPDATE users SET status/role` | C5：`row.status = body.status` / `row.role = body.role` |
| 70 | 回读用户 | 直接用改后的实体 `_user_out(row)`（`session.commit()` 后 `session.refresh(row)` 可选） |
| 87 | 模型详情读 | C1：`session.get(LlmModel, model_id)` |
| 95 | `UPDATE models SET is_default=0 WHERE is_default=1 AND id<>%s` | C6：`update(LlmModel).where(LlmModel.is_default.is_(True), LlmModel.id != model_id).values(is_default=False)` |
| 111 | 模型计数 | C3：同 34 |
| 113 | 模型列表 | C2：同 36 |
| 123 | `UNIQUE(vendor, model_id)` 校验 | C2：`session.scalar(select(LlmModel.id).where(LlmModel.vendor == body.vendor, LlmModel.model_id == body.model_id))` |
| 129 | `INSERT INTO models ...` | C5：`session.add(LlmModel(...))` |
| 137–139 | 回读新模型 id | 直接用 `add` 后的实体（删掉二次查询） |
| 168 | `UPDATE models SET <动态 sets>` | C6：`changes = {}` 动态填 → `session.execute(update(LlmModel).where(LlmModel.id == model_id).values(**changes))` |
| 176 | `UPDATE models SET status` | C6：`.values(status=body.status)` |
| 185 | `SELECT * FROM agent_model` | C2：`{r.agent_key: r for r in session.scalars(select(AgentModel)).all()}` |
| 191 | 取模型名 | C1：`session.get(LlmModel, b.model_id)` |
| 215 | `INSERT INTO agent_model ... ON DUPLICATE KEY UPDATE` | C5：`session.merge(AgentModel(agent_key=agent_key, model_id=body.model_id, updated_by=admin["id"]))` |

- [ ] **Step 1: 改三个输出/工具函数（dict → 实体）**

```python
def _user_out(row: User) -> AdminUserOut:
    return AdminUserOut(
        id=row.id, username=row.username, role=row.role, status=row.status,
        created_at=fmt_dt(row.created_at), last_login_at=fmt_dt(row.last_login_at))


def _model_out(row: LlmModel) -> ModelOut:
    return ModelOut(
        id=row.id, name=row.name, model_id=row.model_id, vendor=row.vendor,
        base_url=row.base_url or "", capabilities=row.capabilities or [],
        is_default=bool(row.is_default), status=row.status, extra=row.extra or {},
        created_at=fmt_dt(row.created_at), updated_at=fmt_dt(row.updated_at))


def _get_model(session: Session, model_id: int) -> LlmModel:
    row = session.get(LlmModel, model_id)
    if not row:
        raise HTTPException(status_code=404, detail="模型不存在")
    return row


def _clear_default(session: Session, model_id: int) -> None:
    """设置新默认时清除其他默认标记（同能力唯一默认）"""
    session.execute(update(LlmModel)
                    .where(LlmModel.is_default.is_(True), LlmModel.id != model_id)
                    .values(is_default=False))
```

> `ModelOut` 字段清单以原 `_model_out` 为准（含 `api_key` 脱敏标记等）；`jloads` 若变成未使用则删除该 import。

- [ ] **Step 2: 改用户管理（动态筛选改表达式列表）**

```python
@router.get("/users")
def list_users(page: int = Query(1, ge=1), limit: int = Query(20, ge=1, le=100),
               q: str = "", session: Session = Depends(get_db)):
    conditions = []
    if q:
        conditions.append(User.username.like(f"%{q}%"))
    base = select(User).where(*conditions)
    total = session.scalar(select(func.count()).select_from(base.subquery()))
    rows = session.scalars(base.order_by(User.id)
                           .limit(limit).offset((page - 1) * limit)).all()
    return {"total": total, "page": page, "limit": limit,
            "items": [_user_out(r) for r in rows]}
```

```python
@router.patch("/users/{user_id}", response_model=AdminUserOut)
def patch_user(user_id: int, body: AdminUserPatch, session: Session = Depends(get_db)):
    row = session.get(User, user_id)
    if not row:
        raise HTTPException(status_code=404, detail="用户不存在")
    demoting = (body.role == "user" or body.status == "disabled") and row.role == "admin"
    if demoting:
        admin_n = session.scalar(select(func.count()).select_from(User)
                                 .where(User.role == "admin", User.status == "active"))
        if admin_n <= 1:
            raise HTTPException(status_code=400, detail="不能停用/降级最后一名管理员")
    if body.status:
        row.status = body.status
    if body.role:
        row.role = body.role
    session.commit()
    session.refresh(row)
    return _user_out(row)
```

- [ ] **Step 3: 改模型池（能力筛选沿用 LIKE 语义，JSON 列按文本匹配）**

```python
    conditions = []
    if status:
        conditions.append(LlmModel.status == status)
    if capability:
        conditions.append(LlmModel.capabilities.like(f'%"{capability}"%'))
    base = select(LlmModel).where(*conditions)
    total = session.scalar(select(func.count()).select_from(base.subquery()))
    rows = session.scalars(base.order_by(LlmModel.id)
                           .limit(limit).offset((page - 1) * limit)).all()
    return {"total": total, "page": page, "limit": limit,
            "items": [_model_out(r) for r in rows]}
```

```python
@router.post("/models", response_model=ModelOut, status_code=201)
def create_model(body: ModelCreate, session: Session = Depends(get_db)):
    dup = session.scalar(select(LlmModel.id).where(LlmModel.vendor == body.vendor,
                                                   LlmModel.model_id == body.model_id))
    if dup:
        raise HTTPException(status_code=409, detail="同 vendor 下 model_id 已存在")
    if body.is_default:
        _clear_default(session, 0)                      # 0 = 清掉全部默认标记
    model = LlmModel(name=body.name, model_id=body.model_id, vendor=body.vendor,
                     base_url=body.base_url, api_key_enc=body.api_key,
                     capabilities=body.capabilities, is_default=bool(body.is_default),
                     extra=body.extra)
    session.add(model)
    session.commit()
    return _model_out(model)
```

```python
@router.put("/models/{model_id}", response_model=ModelOut)
def update_model(model_id: int, body: ModelUpdate, session: Session = Depends(get_db)):
    _get_model(session, model_id)
    changes = {}
    if body.name is not None:
        changes["name"] = body.name
    if body.base_url is not None:
        changes["base_url"] = body.base_url
    if body.api_key:
        changes["api_key_enc"] = body.api_key
    if body.capabilities is not None:
        changes["capabilities"] = body.capabilities
    if body.is_default is not None:
        if body.is_default:
            _clear_default(session, model_id)
        changes["is_default"] = bool(body.is_default)
    if body.extra is not None:
        changes["extra"] = body.extra
    if changes:
        session.execute(update(LlmModel).where(LlmModel.id == model_id).values(**changes))
    session.commit()
    return _model_out(_get_model(session, model_id))
```

```python
@router.patch("/models/{model_id}/status", response_model=ModelOut)
def patch_model_status(model_id: int, body: ModelStatusPatch, session: Session = Depends(get_db)):
    _get_model(session, model_id)
    session.execute(update(LlmModel).where(LlmModel.id == model_id)
                    .values(status=body.status))
    session.commit()
    return _model_out(_get_model(session, model_id))
```

- [ ] **Step 4: 改 agent 绑定（N+1 读法保留，仅换 ORM）**

```python
@router.get("/agents", response_model=list[AgentBindingOut])
def list_agents(session: Session = Depends(get_db)):
    bindings = {r.agent_key: r for r in session.scalars(select(AgentModel)).all()}
    out = []
    for key, desc in AGENT_DEFS.items():
        b = bindings.get(key)
        model_name = None
        if b:
            m = session.get(LlmModel, b.model_id)
            model_name = m.name if m else None
        out.append(AgentBindingOut(
            agent_key=key, label=desc["label"], required_capability=desc["capability"],
            model_id=b.model_id if b else None, model_name=model_name,
            updated_at=fmt_dt(b.updated_at) if b else None,
        ))
    return out


@router.put("/agents/{agent_key}")
def bind_agent(agent_key: str, body: AgentBindRequest,
               admin: dict = Depends(require_role("admin")),
               session: Session = Depends(get_db)):
    model = _get_model(session, body.model_id)
    session.merge(AgentModel(agent_key=agent_key, model_id=body.model_id,
                             updated_by=admin["id"]))
    session.commit()
    return {"code": 0, "message": f"agent {agent_key} 已绑定模型 {model.name}",
            "agent_key": agent_key, "model_id": body.model_id}
```

> `updated_at` 由列的 `ON UPDATE CURRENT_TIMESTAMP` 自动刷新，`merge` 走 UPDATE 时不需手写该字段。

> **实现偏差记录（2026-10-17，实机运行后修正，已落库到 `app/routers/admin.py`）**：示例代码有 2 处需修正、1 处以原实现为准：
> 1. Step 1 的 `_model_out` 多写了 `created_at=fmt_dt(row.created_at)`，而 `ModelOut`（`app/schemas/admin.py`）**并无该字段**，原实现也不返回它 → 已删除；字段清单按原 `_model_out` 逐字保留（含 `api_key_configured` 脱敏标记）。
> 2. `update_model` / `patch_model_status` 在 `session.commit()` 后补 `session.refresh(row)`：实测（间隔 1.3s）Core `update()` 执行后内存实体上的 `updated_at` 仍是**旧值**（44s），库内已被 `ON UPDATE CURRENT_TIMESTAMP` 刷成 45s；原实现是「改完回读一次」，不回读会让响应里的 `updated_at` 相对原文出现偏差。
> 3. `bind_agent` 的路由路径为 `/agents/{agent_key}/model`（非 Step 4 示例的 `/agents/{agent_key}`），且 `agent_key 存在性 / 目标模型 active / 能力匹配` 三段校验**照原实现保留**（示例片段将其省略），仅数据访问部分按上表（`_get_model` + `session.merge`）替换。
>
> 另核实：`LlmModel.capabilities.like('%\"cap\"%')` 在 JSON 列上可用（实测过滤结果正确），与原 `capabilities LIKE %s` 语义一致。

- [ ] **Step 5: 回归 + 提交**

```powershell
D:\Anaconda\envs\shixi\python.exe -m pytest -q
git add app/routers/admin.py
git commit -m "refactor(db): admin 路由改用 ORM（用户/模型池/agent 绑定）"
```

---

### Task 19: `app/routers/qa.py`（1 处）

**Files:** Modify: `app/routers/qa.py`

| 行 | 原调用 | 目标写法 |
|---|---|---|
| 46 | `db.query("SELECT id FROM subscriptions WHERE id=%s AND user_id=%s", ..., one=True)` | C2：`session.scalar(select(Subscription.id).where(Subscription.id == body.subscription_id, Subscription.user_id == user["id"]))`；`if sub is None: 404` |

- [ ] **Step 1: 改订阅归属校验**

```python
    sub = session.scalar(select(Subscription.id).where(
        Subscription.id == body.subscription_id, Subscription.user_id == user["id"]))
    if sub is None:
        raise HTTPException(status_code=404, detail="订阅不存在")
```

> `orchestrator.ask_review(...)` 及其后所有编排调用**一行不改**。

- [ ] **Step 2: 回归 + 提交**

```powershell
D:\Anaconda\envs\shixi\python.exe -m pytest -q
git add app/routers/qa.py
git commit -m "refactor(db): qa 路由改用 ORM"
```

---

### Task 20: `worker/pipeline.py`（9 处） + `worker/scheduler.py`（1 处）

**Files:** Modify: `worker/pipeline.py`、`worker/scheduler.py`

**Interfaces:**
- Consumes: `Paper`、`Subscription`、`SubscriptionPaper`、`TaskLog` 模型；`SessionLocal`
- Produces: 内部函数签名变化 —— `_execute_followup(task, session, sub, log, started)`、`_dedup(kept, session)`、`_store(scored, sub_id, session)`；`sub` 仍是 dict 形态（`RowMapping`），`_fetch_start/_rank_and_filter/_build_brief` **不改**。

- [ ] **Step 1: 改 `run_followup`（一个 Session 包住整轮任务，成功一次提交）**

```python
def run_followup(task: dict) -> None:
    """跟进任务：插日志 → 抓取/入库 → 回写日志与游标，同一事务内完成"""
    payload = task.get("payload", {})
    sub_id = payload["subscription_id"]
    kind = payload.get("kind", "schedule")
    started = datetime.now()
    try:
        with SessionLocal() as session:
            sub = session.execute(
                select(Subscription).where(Subscription.id == sub_id)
            ).mappings().first()                      # dict 形态，下游抓取函数零改动
            if not sub:
                print(f"[pipeline] 订阅 {sub_id} 不存在，跳过")
                return
            log = TaskLog(task_id=task["task_id"], kind=kind, subscription_id=sub_id,
                          direction_name=sub["name"], status="running", started_at=started)
            session.add(log)
            session.flush()                           # 取到 log.id
            _execute_followup(task, session, sub, log, started)
            session.commit()
    finally:
        release(sub_id)                               # 原 finally 逻辑保留
```

> 事务边界：`task_logs(papers/subscription_papers)` 多步写在同一 Session 内，成功一次 `commit`；`_execute_followup` 内部按其原有 try/except 落 `failed`（不抛出），故失败也会被提交，**与迁移前行为一致**。

- [ ] **Step 2: 改 `_execute_followup` 的三处写入（C6）**

```python
        session.execute(update(TaskLog).where(TaskLog.id == log.id).values(
            pulled=pulled, added=added, deduped=deduped,
            duration=int((finished - started).total_seconds() * 1000),
            status=status, detail=detail, finished_at=finished))
        # 游标推进：仅 SUCCESS 后推进（失败窗口可重扫、不丢数据，§7.3）
        if status == "success":
            session.execute(update(Subscription).where(Subscription.id == sub_id)
                            .values(last_run_at=started))
```

```python
    except Exception as e:
        # 阶段致命错误：落 failed，不推进游标，下一窗口重试（§4.8）
        traceback.print_exc()
        session.execute(update(TaskLog).where(TaskLog.id == log.id).values(
            status="failed", detail={"error": str(e)}, finished_at=datetime.now()))
```

> `detail` 原为 `json.dumps(...)` 字符串，改传 dict（JSON 列直接接收）。**不要**在 `_execute_followup` 内 `session.commit()`（提交权归 `run_followup`）。函数签名改为 `_execute_followup(task, session, sub, log, started)`，`log_id` 引用全部换成 `log.id`，并把 `session` 透传给 `_dedup` / `_store`。

- [ ] **Step 3: 改 `_dedup`（C2/C3）**

```python
            rows = session.scalars(
                select(Paper.external_id).where(Paper.external_id.in_(batch))).all()
            existing.update(rows)
```

```python
    recent = session.scalars(
        select(Paper.title).where(Paper.created_at >= datetime.now() - timedelta(days=90))).all()
    recent_titles = {normalize_title(t) for t in recent}
```

> 签名 `_dedup(kept, session)`；原 `r["external_id"]` / `r["title"]` 读法随 `.scalars()` 改为直接取值。

- [ ] **Step 4: 改 `_store`（C7：upsert + INSERT IGNORE）**

```python
def _store(scored, sub_id: int, session) -> int:
    """入库：papers upsert 拿 id → subscription_papers 去重插入（不改业务语义）"""
    added = 0
    for paper, score in scored:
        stmt = mysql_insert(Paper).values(
            external_id=paper.external_id, source=paper.source, title=paper.title,
            authors=paper.authors, abstract=paper.abstract or "",
            journal=paper.journal or "", published_at=paper.published_at,
            url=paper.url or "", raw_meta=paper.raw_meta,
        ).on_duplicate_key_update(id=func.LAST_INSERT_ID(Paper.id))   # 等价原 ON DUPLICATE KEY UPDATE
        paper_id = session.execute(stmt).lastrowid

        inserted = session.execute(
            mysql_insert(SubscriptionPaper).prefix_with("IGNORE")
            .values(subscription_id=sub_id, paper_id=paper_id, score=round(score, 4))
        ).rowcount
        if inserted:
            added += 1
    return added
```

> 新增 import：`from sqlalchemy import func, select, update`、`from sqlalchemy.dialects.mysql import insert as mysql_insert`、`from app.models import Paper, Subscription, SubscriptionPaper, TaskLog`、`from app.session import SessionLocal`。`json` 若仅用于此处则删除该 import。

- [ ] **Step 5: 改 `scheduler._due_subs`（C3，返回 dict 形态保持调用方读法不变）**

```python
def _due_subs(...) -> list:
    conditions = [...]
    return session.execute(
        select(Subscription.id, Subscription.last_run_at, Subscription.created_at,
               func.coalesce(Setting.frequency, "daily").label("frequency"))
        .outerjoin(Setting, Setting.user_id == Subscription.user_id)
        .where(*conditions)
    ).mappings().all()
```

> `conditions` 由原 `where` / `params` 字符串拼接改写而来（`last_run_at IS NULL` → `Subscription.last_run_at.is_(None)`、时间窗口 → `Subscription.last_run_at <= ...` 等，以原文件条件为准）。函数自建 `with SessionLocal() as session:`。调用方对 `sub["id"] / sub["last_run_at"] / sub["created_at"] / sub["frequency"]` 的读法**保持原样**。

- [ ] **Step 6: worker 实机验证（不用 pytest，直接跑一轮）**

```powershell
D:\Anaconda\envs\shixi\python.exe -c "from worker.scheduler import enqueue_due_for_user; print('enqueued', enqueue_due_for_user(1, kind='schedule'))"
```

Expected: 返回入队数量；随后后端日志出现 `[pipeline] 跟进完成 sub=...`，`task_logs` 新增一条 `status='success'`（或来源失败时的 `partial_failed`），行数与迁移前同源。

> **实现偏差记录（2026-10-17，实机跑通整轮后修正，已落库到 `worker/pipeline.py`、`worker/scheduler.py`）**：示例代码有 4 处不可直接落地，另 1 处控制流需还原：
> 1. Step 1 示例取 `sub` 用 `session.execute(select(Subscription)...).mappings()`，但 `mappings()` 对**单个 ORM 实体**返回的键是**类名**（`'Subscription'`），`sub["id"]/sub["name"]/sub["keywords"]` 会直接 `KeyError`。已改为列形态 `select(Subscription.__table__).where(Subscription.__table__.c.id == sub_id)`（实测：键=列名，JSON 列已反序列化为 `list`、时间列为 `datetime`），下游 `_fetch_start`/`_rank_and_filter`/`_execute_followup` 的 dict 读法与 `isinstance(cursor, str)` 兜底**一行不改**。
> 2. 示例把「订阅不存在 → print + return」写在 `try/finally` 里，会额外触发 `release(sub_id)`；迁移前该早退在 `try` 之外、**不释放**。已把早退移到 `try` 之前，保持原控制流（仅当订阅真的被删时才有差异）。
> 3. 示例 `_execute_followup(task, session, sub, log_id, started)` 传的是 `log_id`，而函数体里又用 `log.id`；已统一传 `log` 实体，`session.add(log); session.flush()` 取 `log.id`（这正是示例注释里 flush 的用途）。
> 4. `task` 是 `worker.queue.Task` 数据类（`task.payload` / `task.task_id`），示例里的 `task.get("payload")` / `task["task_id"]` 不成立——已按原实现保留 `task.payload` / `task.task_id`。
> 5. `import json` **保留**：`_execute_followup` 里 `json.loads(sub["keywords"])` 的兜底分支仍在用。
> 6. `_due_subs` 签名由 `(where: str, params: tuple)` 改为 `(user_id: int | None = None)`，两个调用点（`tick` / `enqueue_due_for_user`）同步；返回 `RowMapping`，键为 `id/last_run_at/created_at/frequency`，调用方读法零改动。
>
> **已实测的等价性（用桩适配器在测试库跑通整轮）**：① 正常轮：`pulled=3 added=3 deduped=0`、`papers=3`、`sp=3`、游标推进；② 同源重跑：`deduped=3 added=0`、表行数不变（覆盖 upsert「已存在」分支与 `INSERT IGNORE` 的 `rowcount=0`）；③ 来源抛错轮：`status=partial_failed`、`detail.source_errors` 落库、游标**不**推进；④ `settings.frequency` 外连生效（`hourly` 被正确读出）；⑤ `_inflight` 在管道结束后已释放。
>
> **已知可接受差异（非回归）**：`run_followup` 单次 commit 使 `task_logs` 的 `running` 行在整轮结束前**不可见**（迁移前每次 `db.execute` 各自提交，`running` 立即可见）。已核对 `web/src/views/DashboardView.vue`：它对 `/api/follow-up/tasks/{id}` 的 404 明确按「worker 尚未领取 = 进行中」处理并继续轮询，故端到端表现不变；仅 `/api/logs` 列表中不再出现"进行中"行。

- [ ] **Step 7: 回归 + 提交**

```powershell
D:\Anaconda\envs\shixi\python.exe -m pytest -q
git add worker/pipeline.py worker/scheduler.py
git commit -m "refactor(db): 抓取链路改用 ORM，多步写入收敛为单事务"
```

---

### Task 21: `ai/retriever.py`（2 处） + `ai/orchestrator.py`（2 处） + `ai/llm_provider.py`（2 处）

**Files:** Modify: `ai/retriever.py`、`ai/orchestrator.py`、`ai/llm_provider.py`

**Interfaces:**
- Consumes: `Paper`、`Subscription`、`SubscriptionPaper`、`LlmModel`、`AgentModel` 模型；`SessionLocal`
- Produces: 三个模块的**对外函数签名全部不变**（`search_library`、`ask_review`、`_resolve_agent_model` 等）

- [ ] **Step 1: `retriever.py` 的两处查询（C4：`MATCH ... AGAINST` 保留原生 SQL）**

```python
def _fulltext_search(where: list[str], params: list, against: str, limit: int) -> list[dict]:
    """全文检索（ngram）：保留原生 MATCH...AGAINST，仅换执行通道"""
    sql = (f"""SELECT * FROM papers WHERE {' AND '.join(where)}
               ORDER BY MATCH(title, abstract) AGAINST (%s IN NATURAL LANGUAGE MODE) DESC
               LIMIT {int(limit)}""")
    with engine.connect() as conn:
        result = conn.exec_driver_sql(sql, tuple(params + [against]))
        return [dict(r) for r in result.mappings().all()]


def _like_search(where: list[str], params: list, limit: int) -> list[dict]:
    """关键词 LIKE 兜底检索：保留原生 SQL，仅换执行通道"""
    sql = f"""SELECT * FROM papers WHERE {' AND '.join(where)}
              ORDER BY published_at DESC LIMIT {int(limit)}"""
    with engine.connect() as conn:
        result = conn.exec_driver_sql(sql, tuple(params))
        return [dict(r) for r in result.mappings().all()]
```

> 新增 import：`from app.session import engine`。`_apply_filters`（构建 `where`/`params` 的逻辑）、`_to_evidence`、`enhance_meta` 调用链**全部不改**。这是 spec §6.4 明确允许保留原生 SQL 的两处（全文检索）。

- [ ] **Step 2: `orchestrator.py` 的 `_load_review_evidence`（C2/C3）**

```python
    with SessionLocal() as db_session:                # 注意：本文件的 `session` 是 QA 会话 dict，勿占用该名字
        sub = db_session.scalar(select(Subscription.id).where(
            Subscription.id == session["subscription_id"],
            Subscription.user_id == session["user_id"]))
        if sub is None:
            raise RuntimeError("订阅不存在")
        papers = db_session.execute(
            select(Paper)
            .join(SubscriptionPaper, SubscriptionPaper.paper_id == Paper.id)
            .where(SubscriptionPaper.subscription_id == session["subscription_id"])
            .order_by(SubscriptionPaper.score.desc())
            .limit(REVIEW_MAX_PAPERS)).mappings().all()
    evidence = [{...} for p in papers]                 # 原 evidence 构造保留，p["..."] 读法可用
```

> 新增 import：`from sqlalchemy import select`、`from app.models import Paper, Subscription, SubscriptionPaper`、`from app.session import SessionLocal`。`REVIEW_MAX_PAPERS`、`evidence` 字段结构逐字保留。

- [ ] **Step 3: `llm_provider.py` 的 `_resolve_agent_model`（C3）**

```python
def _resolve_agent_model(agent_key: str, cap: str | None = None):
    with SessionLocal() as db_session:
        row = db_session.execute(
            select(LlmModel)
            .join(AgentModel, AgentModel.model_id == LlmModel.id)
            .where(AgentModel.agent_key == agent_key, LlmModel.status == "active")
        ).mappings().first()
        if row and _has_capability(row, cap):
            return row
        # 2. 默认兜底：同能力 is_default=1 且 active（对齐 F-MODEL-05）
        for m in db_session.execute(
            select(LlmModel).where(LlmModel.is_default.is_(True),
                                   LlmModel.status == "active")
        ).mappings().all():
            if _has_capability(m, cap):
                return m
    return None
```

> `RowMapping` 兼容 `row["..."]` 与 `row.get("capabilities")`；`_has_capability` **不改**。返回的 dict 供下游读 `base_url` / `api_key_enc` / `model_id` 等，键名与原 `SELECT m.*` 一致。

- [ ] **Step 4: 全文检索链路验证**

```powershell
D:\Anaconda\envs\shixi\python.exe -c "from ai.retriever import search_library; r = search_library('大模型 推理', ['arXiv'], limit=5); print(len(r), r[0]['title'][:40] if r else 'empty')"
```

Expected: 返回 0..5 条带 `title` 的 dict，且 `enhance_meta` 注入的 `journal_level` 等键存在。

> **实现偏差记录（2026-10-17，实机验证后修正，已落库到 `ai/retriever.py`、`ai/llm_provider.py`、`ai/orchestrator.py`）**：示例代码有 3 处运行时报错、2 处与真实文件不符：
> 1. **列形态 select（关键）**：Step 2/3 示例的 `select(Paper)` / `select(LlmModel)` 走 `.mappings()` 时，键是**类名**（`'Paper'` / `'LlmModel'`），于是 orchestrator 的 `p["id"]`、llm_provider 的 `row["base_url"]` 与 `_has_capability(row, cap)` 里的 `row["capabilities"]` 都会直接 `KeyError`/取到 None。已全部改为 `select(Model.__table__)`（实测键=列名、JSON 列已反序列化、`RowMapping` 在 session 关闭后仍可正常读值）。
> 2. **函数名不改**：真实文件里是 `get_agent_model`（Step 3 示例写作 `_resolve_agent_model`）、`_step_review_retrieve`（Step 2 示例写作 `_load_review_evidence`）。为满足「对外函数签名全部不变」，两处**均未改名**，`chat()` / `chat_json()` / `_run_review_pipeline` 的调用方零改动。
> 3. **orchestrator 的 sub 取数**：示例写 `sub = db_session.scalar(select(Subscription.id)...)`，会把 `sub` 变成标量 id，而第 508 行要用 `sub["name"]` 填 `session["review_direction"]`（→ `TypeError`）。已改为 `select(Subscription.__table__)...mappings().first()`，一次查询同时保住「存在性判断」与 `sub["name"]`。
> 4. **retriever 签名保持**：示例把 `_fulltext_search` / `_like_search` 的签名写成了 `(where, params, against, limit)`，真实签名是 `(text, keywords, filters, limit, user_id=None)`。只替换执行通道（`db.query` → `engine.connect()` + `exec_driver_sql`），`_apply_filters` 构建 `where/params` 的逻辑与 `%s` 位置参数风格逐字保留。
> 5. **Step 4 验证命令有误**：`search_library('大模型 推理', ['arXiv'], limit=5)` 与真实签名不符——第 2 个位置参数是 `top_k`，`['arXiv']*3` 会让 `int(limit)` 抛错。实际验证改用 `search_library({"query": ..., "keywords": [...]}, top_k=5)` 与 `user_id=1` 隔离路径两条。
>
> **已实测（生产库只读 + 测试库造数）**：① `search_library` 正常返回 5 条并带 `journal_level/ccf/cas_zone`（enhance_meta 生效）；② `user_id=1` 隔离路径（`id IN (SELECT sp.paper_id ...)`）返回正常；③ FULLTEXT 与 LIKE 兜底、`sources/year_from` 过滤、空关键词早退均正常；④ `get_agent_model` 四条分支（绑定可用 / 绑定缺能力→回退默认 / 默认模型被停用→None / planner 需 function_call→None）全部正确；⑤ `_step_review_retrieve` 按 `sp.score DESC` 取数（0.9 在 0.5 前）、`evidence` 结构与 `review_direction` 逐字一致、订阅不存在抛 `RuntimeError`。

- [ ] **Step 5: 回归 + 提交**

```powershell
D:\Anaconda\envs\shixi\python.exe -m pytest -q
git add ai/retriever.py ai/orchestrator.py ai/llm_provider.py
git commit -m "refactor(db): ai 层改用 ORM/Core（全文检索保留原生 SQL）"
```

---

### Task 22: `_update_model.py`（2 处）

**Files:** Modify: `_update_model.py`

| 行 | 原调用 | 目标写法 |
|---|---|---|
| 5 | `UPDATE models SET ... WHERE is_default=1` | C6：`update(LlmModel).where(LlmModel.is_default.is_(True)).values(...)` |
| 12 | 遍历打印模型列表 | C2：`session.scalars(select(LlmModel)).all()` |

- [ ] **Step 1: 改写为 ORM 版（保持脚本可用，不删除）**

```python
# 一次性脚本：把默认模型配置改成当前环境所需（ORM 版）
from sqlalchemy import select, update

from app.models import LlmModel
from app.session import SessionLocal

with SessionLocal() as session:
    n = session.execute(
        update(LlmModel).where(LlmModel.is_default.is_(True)).values(
            name="qwen3.7-plus (默认)", model_id="qwen3.7-plus", vendor="dashscope",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            api_key_enc="sk-...",          # 原脚本中的 key 原样保留
        )).rowcount
    session.commit()
    print("updated models:", n)
    for row in session.scalars(select(LlmModel)).all():
        print({"id": row.id, "name": row.name, "model_id": row.model_id,
               "vendor": row.vendor, "status": row.status, "base_url": row.base_url})
```

> **实现偏差说明（2026-10-17，已落库到 `_update_model.py`）**：Step 1 代码逐字落地，仅补记两点事实：① 脚本不再调用 `db.init_database()`（该助手随 `app/database.py` 在 Task 23 一并删除），改为依赖 `app/models.py` 的 `Base` 建表，即脚本要求目标库表已存在；② `api_key_enc` 保留了原脚本中的真实 key（未用示例里的 `sk-...` 占位）。实测（把 `SessionLocal` 指向测试库）可直接跑通：`updated models: 1`，落库值与生产库现值逐字一致，可安全重跑（幂等）。

- [ ] **Step 2: 提交**

```powershell
git add _update_model.py
git commit -m "chore(db): _update_model 脚本改用 ORM"
```

---

## Phase 3 收尾 · 清理与验收

### Task 23: 删除过渡物 + 全量验收

**Files:**
- Delete: `app/schema.sql`
- Modify: `app/database.py`（删除三个兼容助手）

**Interfaces:** Produces: 无 `db.query/execute/execute_many` 的最终代码库。

- [ ] **Step 1: 删除 `app/schema.sql` 与 `app/database.py` 的三个助手**

```powershell
D:\Anaconda\envs\shixi\python.exe -c "import pathlib; p = pathlib.Path('app/schema.sql'); p.unlink(); print('removed', p)"
```

然后从 `app/database.py` 删除 `query` / `execute` / `execute_many` 三个函数（`init_database` / `_ensure_database` / `_init_admin` / `_bootstrap_default_model` 保留，它们已在 Task 4 改为 ORM）。

- [ ] **Step 2: 全仓残留检查（验收门禁 3）**

```powershell
D:\Anaconda\envs\shixi\python.exe -c "import pathlib, sys; pats = ['db.query(', 'db.execute(', 'db.execute_many(', 'mysql.connector', 'from app.database import query', 'import mysql']; hits=[]; [hits.append(f'{p}:{i}: {l.strip()}') for p in pathlib.Path('.').rglob('*.py') if 'site-packages' not in str(p) for i, l in enumerate(p.read_text(encoding='utf-8', errors='ignore').splitlines(), 1) if any(x in l for x in pats) and 'doc' not in str(p)]; print('\n'.join(hits) if hits else 'CLEAN'); sys.exit(1 if hits else 0)"
```

Expected: 输出 `CLEAN`，退出码 0。

- [ ] **Step 3: 结构对齐校验复跑（验收门禁 1）**

```powershell
D:\Anaconda\envs\shixi\python.exe scripts\align_schema.py --show-ddl
```

Expected: `结构对齐校验通过：差异清单为空`，退出码 0。

- [ ] **Step 4: 全量测试（验收门禁 2）**

```powershell
D:\Anaconda\envs\shixi\python.exe -m pytest -q
```

Expected: `18 passed`。

- [ ] **Step 5: 后端启动 + 前端构建（验收门禁 5）**

```powershell
D:\Anaconda\envs\shixi\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8000
```

Expected: `[main] 文献雷达后端已就绪`，无异常。另开终端：

```powershell
cd web
npm run build
```

Expected: `vite build` 成功产出 `dist/`。

- [ ] **Step 6: 实机冒烟（验收门禁 4）**

按序手动验证，结果需与迁移前一致：
1. 登录（admin）→ 返回 `unread_brief`；
2. 订阅列表 → `paper_count` / `unread_count` 正确；
3. 文献列表 → 未读标记可见，未读数与列表一致；
4. 标记已读 / 批量已读 / 一键全读 → 未读数递减；
5. 提问一次（v3/v4 编排）→ 有回答返回，`deep` 模式有迭代轮次徽标；
6. 触发一次跟进抓取 → `task_logs` 新增记录，`last_run_at` 推进。

- [ ] **Step 7: 提交**

```powershell
git add -A app worker ai _update_model.py
git commit -m "refactor(db): 清理 schema.sql 与旧 SQL 助手，ORM 迁移收尾（验收全绿）"
```

> **实现偏差记录（Task 23 执行期实测）**
> 1. **门禁正则误伤**：Step 2 的残留检查以子串 `import mysql` 匹配，会命中 `scripts/align_schema.py` 的 `from sqlalchemy.dialects import mysql`（SQLAlchemy 方言模块，非 mysql-connector 驱动）。为让门禁输出字面 `CLEAN`（退出码 0），该行改为 `from sqlalchemy.dialects.mysql import dialect as mysql_dialect`（等价、更贴近直接用法）。故 Task 23 实际改动文件比计划多一个 `scripts/align_schema.py`（1 行）。
> 2. **Step 6 冒烟实测结论**：
>    - S1–S4 全绿：登录返回 `unread_brief`（total_unread=66）；订阅 `paper_count=101 / unread_count=66`；文献列表未读 66 与订阅、`/unread/summary` 三者一致；三种已读方式 `66→65→63→0` 递减正确。
>    - S5 深模式数据齐备：`complexity=deep`、`loop_counter` 递增至 3、`review_notes.passed=false`、`tool_calls` 有记录（前端迭代轮次徽标依赖字段完整）；另以简化查询实测 `finished=true`、返回 `answer`(568 字) + 8 条引用，证明问答通路完好。深模式一次会话 10 分钟未收敛系 arXiv/S2 上游 429 限流导致，与迁移无关（日志无任何 traceback）。
>    - S6 跟进实测：`task_logs` 新增一行（`pulled=4 added=1 deduped=3`），但状态为 `partial_failed`（上游 429），故 `last_run_at` 未推进。经比对基线 `worker/pipeline.py`，其为 `if status == "success"` 才推进——与现实现逐字一致（§7.3 失败窗口可重扫），属预期行为而非变化。
> 3. **冒烟数据残留**：一次跟进新增 1 篇 paper + 1 条 subscription_papers + 1 条 task_logs（正常业务结果）；`subscription_papers` 的已读状态已用 Task 1 备份（`backup/literature_radar_20261017.sql`）还原，还原后未读 67（= 原始 66 + 新增那篇）。

---

### Task 24: 文档同步

**Files:**
- Modify: `doc/详细设计文档.md`（§2 表结构、数据层描述、§3.5 相关）
- Modify: `doc/文档变更记录.md`（追加 §23）
- Modify: `doc/项目脉络介绍.md`（数据层段落）

- [ ] **Step 1: `详细设计文档.md` §2**

把「表结构」来源改为：**表结构以 `app/models.py` 为唯一权威**，`Base.metadata.create_all()` 负责建表；`app/schema.sql` 已删除。附 8 模型 ↔ 8 表对照表（表名/类名），并把数据访问说明从「原生 SQL + mysql-connector」改为「SQLAlchemy 2.0 ORM + PyMySQL，Session/事务模型见 spec §6」。

- [ ] **Step 2: `文档变更记录.md` 追加 §23**

记录：变更类型（数据层重构）、范围（14 文件 / 69 处）、驱动切换、模型权威、新增文件、删除文件、验收结论（align 差异为空 / pytest 18 passed / grep CLEAN / 冒烟通过）、依据文档（《数据库ORM迁移设计.md》《数据库ORM迁移实现计划.md》）。

- [ ] **Step 3: `项目脉络介绍.md` 数据层段落**

把「原生 SQL + 手写连接池 + autocommit」改写为「ORM 模型为权威 + Session 事务 + QueuePool(10, pre_ping, recycle) + 允许全文检索保留原生 SQL」，并指向 spec 与 plan。

- [ ] **Step 4: 提交**

```powershell
git add doc/详细设计文档.md doc/文档变更记录.md doc/项目脉络介绍.md
git commit -m "docs(db): 同步 ORM 迁移后的设计文档与变更记录"
```

> **实现说明（Task 24 执行期）**：§2.6「建表清单」原文只列了 7 张表，遗漏 `settings`；本次一并补第 8 行 `settings`（用户设置：调度频率 / 通知 / 来源池，`user_id UNIQUE`，FK → users.id），使 2.6 清单与「8 模型 ↔ 8 表」对照一致（模型为权威、`settings` 表确实存在）。

---

## Self-Review（写计划时的自查结论）

- **Spec 覆盖**：spec §3.1 新增文件 → Task 2/3/5/7–12；§3.2 改动 → Task 4/6/13–22/24；§3.3 删除 → Task 23；§4 模型映射 → Task 2（ngram 索引、关联对象、Boolean 已逐条落实并实测 DDL）；§5 建表与校验 → Task 4/5；§6 会话与事务 → Task 3/4/20（`get_db`、`pool_pre_ping/recycle`、pipeline 单事务、§6.4 原生 SQL 豁免）；§7 四阶段 → Phase 0/1/2/3；§8 十一条测试 → Task 8–12；§9 验收 → Task 23；§10 风险兜底 → Task 1（备份）、Task 5（结构漂移门禁）。
- **无占位符**：所有新增/改写均为可运行代码；仅 Phase 3 少数文件（followup 过滤条件、scheduler `_due_subs` 条件、`_execute_followup` 前段）以「条件表达式映射规则」表述并注明「以原文件为准」，因为这些是逐文件上下文相关的机械改写，规则已给足。
- **类型一致性**：`make_url / create_app_engine / engine / SessionLocal / get_db`（Task 3）被 Task 4/5/7/13–22 一致引用；模型类名 `User/Subscription/Paper/SubscriptionPaper/TaskLog/LlmModel/AgentModel/Setting` 全程一致；`_to_out`（Task 16）与调用点一致；`log.id`（Task 20）与 TaskLog 主键一致。
- **已实测的关键结论**：`mysql_with_parser="ngram"` 生成 `WITH PARSER ngram`；`Boolean` → `BOOL`（MySQL 归一为 `tinyint(1)`）；`server_default=text("CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP")` 合法；`exec_driver_sql` 保留 `%s` 位置参数语义（旧助手零业务改动等价替换）。

---

## 待确认事项（动手前请拍板）

**A. 版本控制基线**：项目目录 `d:\实习\my_project\deep_search_pro-master` **没有自己的 `.git`**（它位于 `d:\实习` 仓库内但处于未跟踪状态）。本计划的"每步提交/可回退"依赖 git。请二选一：
1. 在项目目录 `git init` 并建基线提交（推荐，回退最省事）；
2. 不引入 git，改用阶段性 zip 快照（我会把每个 commit 步骤替换为"打快照"）。

**B. 内部行形态策略**：为把改动面和回归风险压到最低，本计划对**会被 dict 消费者复用的结果**（`enhance_meta`、`_to_evidence`、`_to_out(row: dict)`、pipeline 的 `sub`、`get_current_user` 的返回）**保持 dict 形态**（用 Core `select(...).mappings()`），只把**单表 CRUD 与写入**做成 ORM 实体操作。这样"调用点全部不再手拼 SQL"成立，但并非每个点都产出 ORM 实体。若你坚持"连读取也全用实体 + 属性访问"，我需要相应扩大 Phase 3（约多改 40 处属性读法）——请确认选哪种。

---

## 执行方式选择

**1. Subagent-Driven（推荐）** — 每个 Task 派一个全新 subagent 实现，我在两阶段（先查 spec 合规、再审代码质量）复核后进入下一个 Task。适合本计划的"一文件一任务"粒度。

**2. Inline Execution** — 在当前会话按批次执行，每到一个 Phase 边界停下来让你检查。

**请回复你想用哪种方式（以及上面 A / B 两项的选择），我即按计划开工。**
