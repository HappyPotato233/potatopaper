# 数据模型层：8 张表的 ORM 模型（表结构唯一权威，替代 app/schema.sql）
# 对齐《详细设计文档.md》§2；索引/唯一键/外键名沿用现状，保证生成的 DDL 与存量库逐字一致
from datetime import date, datetime

from sqlalchemy import (BigInteger, Boolean, Date, DateTime, Double, Enum, ForeignKey,
                        Index, Integer, JSON, String, Text, UniqueConstraint, text)
from sqlalchemy.dialects.mysql import LONGTEXT
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
    # 多选时间窗：逗号分隔存多个值（如 '1Y,3Y+'），顺序固定见 app.utils.PUB_WINDOWS
    pub_window: Mapped[str] = mapped_column(String(32), nullable=False,
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
    # 论文全文（按需抓取并缓存；Text 在 MySQL 只有 64KB，用 LONGTEXT）
    full_text: Mapped[str | None] = mapped_column(
        Text().with_variant(LONGTEXT, "mysql"), nullable=True)
    full_text_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
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
