# 数据库层 ORM 化迁移设计（SQLAlchemy + PyMySQL）

> 版本：v1.0　创建：2026-10-17
> 定位：数据访问层重构设计（spec）。不改接口契约、不改前端、不改问答/抓取业务逻辑。
> 关联文档：《详细设计文档.md》§2 表结构、§3.5 接口；《项目脉络介绍.md》数据层；《文档变更记录.md》§23

---

## 1. 背景与目标

### 1.1 现状

- 数据访问层是**原生 SQL + 手写连接池**：`app/database.py` 用 `mysql-connector-python` 建连接池，对外只暴露三个助手函数 `query / execute / execute_many`，业务侧（路由 / worker / ai）全部靠**拼 SQL 字符串**读写。
- 表结构由 `app/schema.sql` 定义，启动时 `init_database()` 按分号拆语句逐条执行。
- 连接池 `autocommit=True`，每条 SQL 独立生效，**没有事务**。
- 全仓调用点：**14 个文件、69 处** `db.query/execute/execute_many` 调用。

### 1.2 目标

把数据访问层**改成 SQLAlchemy ORM**：

- 8 张表建成 8 个 ORM 模型类，表结构以模型为**唯一权威**；
- 业务侧读写改为「Session + ORM 查询/关系」，不再手拼 SQL；
- 用**特征测试**给重构上安全网，全程可回归；
- 迁移过程**分层渐进**，每一步都能交付、能回退。

### 1.3 非目标（明确不做）

- 不引入 Alembic 迁移框架（用 `create_all` + 一次性结构校验脚本足够，避免过度设计）；
- 不新建 Repository / DAO 抽象层（8 张表的小项目，查询直接写在原调用处）；
- 不追求 100% 消灭 SQL（全文检索、聚合统计等允许保留原生 SQL）；
- 不改任何 HTTP 接口契约、不改前端、不改 v3/v4 编排与抓取业务逻辑。

---

## 2. 已确认的关键决策

| # | 决策 | 结论 |
|---|---|---|
| D1 | ORM 程度 | **彻底 ORM**：8 张表全部建模型，调用点全部改 ORM |
| D2 | 建表（DDL）归属 | **模型为权威**：废弃 `schema.sql`，改由 `Base.metadata.create_all()` 建表；配一次性**结构对齐校验脚本**（只读不改） |
| D3 | 验证方式 | **pytest 特征测试**：先给现有行为写测试并跑绿，再迁移，迁移后必须全绿；用独立测试库 `literature_radar_test` |
| D4 | 迁移方式 | **分层渐进**：搭地基 → 上安全网 → 逐个换血，每步可交付可回退 |
| D5 | 驱动 | 由 `mysql-connector-python` **换成 `pymysql`**（连接串 `mysql+pymysql://`） |

---

## 3. 文件清单（新增 / 改动 / 删除）

### 3.1 新增

| 文件 | 职责 |
|---|---|
| `app/models.py` | 8 个 ORM 模型（`DeclarativeBase` + `Mapped[]` 注解风格）。只描述"数据长什么样"，不含业务 |
| `app/session.py` | `create_engine` / `sessionmaker` / FastAPI 依赖 `get_db()`（每请求一个 Session）。只管"怎么连、怎么给会话" |
| `tests/`（新目录） | `conftest.py`（测试库连接、建表/清表 fixture）+ 按模块拆分的测试文件 |
| `scripts/align_schema.py` | 结构对齐校验：读 `information_schema` 与模型 metadata 比对，**只列差异、不自动改** |
| `doc/数据库ORM迁移设计.md` | 本设计文档 |

### 3.2 改动

| 文件 | 改动 |
|---|---|
| `app/database.py` | 阶段 1：`query/execute/execute_many` 改写为 SQLAlchemy 等价实现（**签名与返回值不变**）；阶段 3 末尾：删除旧助手，`init_database()` 改为「建库 → `Base.metadata.create_all()` → 管理员 → 默认模型引导」 |
| 14 个调用点文件（见 §6.3） | 阶段 3 逐个把调用点换成原生 ORM |
| `requirements.txt` / `pyproject.toml` | 新增 `sqlalchemy>=2.0`、`PyMySQL>=1.1.0`、`cryptography>=42`、`pytest>=8`；移除 `mysql-connector-python` |
| `doc/详细设计文档.md` | §2 表结构改为"以 `app/models.py` 为权威"；数据层描述更新 |
| `doc/文档变更记录.md` | 追加 §23 |
| `doc/项目脉络介绍.md` | 数据层段落（当前描述为原生 SQL）同步改写 |

### 3.3 删除（阶段 3 末尾）

- `app/schema.sql`（表结构权威转移至 `app/models.py`）；
- `app/database.py` 中的旧 SQL 助手函数。

---

## 4. 模型映射细则

### 4.1 类名与表名

| 表 | 模型类 | 说明 |
|---|---|---|
| `users` | `User` | `role`/`status` 用原生 `Enum` |
| `subscriptions` | `Subscription` | `keywords`/`sources` 用 `JSON` |
| `papers` | `Paper` | 索引较多，见 §4.3 |
| `subscription_papers` | `SubscriptionPaper` | **关联对象**（非纯 `secondary`） |
| `task_logs` | `TaskLog` | `detail` 用 `JSON` |
| `models` | `LlmModel` | 类名避开 `Model` 歧义（与 pydantic 命名风格区分） |
| `agent_model` | `AgentModel` | 主键为字符串 `agent_key`，非自增 id |
| `settings` | `Setting` | `user_id` 唯一 |

### 4.2 通用要求

- 所有模型显式声明：`__table_args__ = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_unicode_ci"}`。
- **索引名、唯一键名、外键名沿用现状**（`idx_users_role`、`uk_papers_external`、`fk_sp_paper` …），使 ORM 生成的 DDL 与现有表逐字一致——这是 §5 校验脚本的判据。
- 时间戳（目标是生成的 DDL 与现状逐字一致，故不用 `func.now()`，显式写 `text()`）：
  - `DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP` → `server_default=text("CURRENT_TIMESTAMP")`；
  - `DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP` → `server_default=text("CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP")`（MySQL 8 支持该列级写法，得到与现状一致的 DDL）。

### 4.3 关键细节

- **papers 的 5 个索引**必须全部在模型里声明，尤其是全文索引：
  `Index('ft_papers_text', 'title', 'abstract', mysql_prefix='FULLTEXT', mysql_with_parser='ngram')`
- **subscription_papers 用关联对象（association object）**：因为它带 `score` / `is_read` / `read_at` 三个附加字段，纯 `secondary` 表达不了附加字段。定稿做法：
  - `SubscriptionPaper` 建为实体模型，含 `subscription_id` / `paper_id` 外键，并以 `relationship` 双向指向 `Subscription` 与 `Paper`；
  - `Subscription.items` → `list[SubscriptionPaper]`，`Paper.items` → `list[SubscriptionPaper]`；
  - 需要"某方向下的文献列表"时经 `SubscriptionPaper.paper` 取；**默认不再额外加 `viewonly` 的 secondary 便捷关系**（保持简单，按需再加）。
- **布尔字段改用 `Boolean`**：`active` / `is_read` / `is_default` 现为 `TINYINT(1)`，驱动返回 `0/1`。前端 [client.ts](file:///d:/实习/my_project/deep_search_pro-master/web/src/api/client.ts) 早已把它们声明为 `boolean`（第 93/110/210 行），且判断全为真值判断（`s.active ?`、`!p.is_read`、`Number(...)`），无一处 `=== 1`。改 `Boolean` 后接口返回 `true/false`，**前端不受影响，反而修正了类型声明与实际值不一致的隐患**。此点需测试覆盖（见 §7 第 10 条）。

---

## 5. 建表与结构对齐校验

### 5.1 `init_database()` 新流程

1. 建库：用临时 engine 连 server（不含 db 名），`CREATE DATABASE IF NOT EXISTS ... DEFAULT CHARSET utf8mb4 COLLATE utf8mb4_unicode_ci`；
2. 建表：`Base.metadata.create_all(engine)`（**只创建不存在的表**，已存在的表不动结构、不动数据）；
3. `_init_admin()`：无 admin 账号时从环境变量创建（幂等，逻辑不变）；
4. `_bootstrap_default_model()`：模型池为空时写入默认模型并绑定 8 个 agent（幂等，逻辑不变）。

> 关键性质：`create_all` 对已存在的表是**空操作**，因此对现有库（1 用户 / 1 订阅 / 101 文献 / 527 日志）绝对安全。

### 5.2 结构对齐校验脚本

`scripts/align_schema.py`：

- 读 `information_schema.COLUMNS` / `STATISTICS`（现有表）；
- 与 `Base.metadata`（模型）逐表比对：列名、类型、可空、默认值、索引/唯一键/外键；
- 输出差异清单；**只读，不做任何 DDL**，差异由人工判断处理。

**验收标准：差异清单为空。**

---

## 6. 会话与事务

### 6.1 最大行为差异

现状 `autocommit=True`，每条 SQL 独立生效、天然无事务。改 ORM 后是 **Session + 显式 `commit`**，这是本次迁移影响最大的行为变化，需按下面三条规则统一处理。

### 6.2 规则

1. **Web 请求**：`get_db()` 依赖产出一个 Session，路由内写操作显式 `session.commit()`，请求结束自动 close。
2. **Worker**：每轮任务 `with SessionLocal() as s:`，把「插 papers → 建 subscription_papers → 写 task_logs」这类多步写**放进同一个事务**，成功一次提交、失败整体回滚（比现状更安全，是净收益）。事务保持短小，避免锁等待。
3. **只读路径不 commit**。

### 6.3 线程安全与连接池

- Session **不是线程安全的**：必须一请求/一线程一个 Session，绝不跨线程共享；`sessionmaker` 每次调用返回新 Session。
- 连接池：`QueuePool(pool_size=10, pool_pre_ping=True, pool_recycle=3600)`。
  - `pool_pre_ping`：取连接前先探活，避免 MySQL 掐断后拿到僵尸连接；
  - `pool_recycle`：提前回收长连接。

### 6.4 允许保留原生 SQL 的场景

`session.execute(text("..."))` 保留原生 SQL 是**明确允许**的，用于 ORM 表达别扭处：

- `ai/retriever.py` 的 `MATCH ... AGAINST` 全文检索（ngram）；
- 聚合/统计类查询。

原则：**ORM 管结构与常规读写，不追求 100% 消灭 SQL。**

---

## 7. 迁移三步走

### 阶段 0 · 连通性预检（前置门槛）

1. 迁移前对 `literature_radar` 做一次备份（`mysqldump`）；
2. 安装 `sqlalchemy` / `PyMySQL` / `cryptography` / `pytest`；
3. 用 pymysql + 现有账号密码连 `literature_radar`，执行 `SELECT 1` 与读一行 papers。

**门禁：此步不通过，后续一步都不动。**

> 注：MySQL 8 默认认证插件 `caching_sha2_password` 下，PyMySQL 必须配 `cryptography`（或 `PyMySQL[rsa]`），否则报 `RuntimeError: 'cryptography' package is required ...`。这是换驱动唯一的坑。

### 阶段 1 · 搭地基（不动任何业务代码）

- 新增 `app/models.py`、`app/session.py`；
- `app/database.py` 的三个助手改为 SQLAlchemy 等价实现（**签名/返回不变**）；`init_database()` 改为 §5.1 流程；
- 依赖切换（加 sqlalchemy/pymysql/cryptography，移除 mysql-connector-python）；
- 跑 `scripts/align_schema.py` → 差异清单为空。

**交付判据：14 个调用点一行未改，服务仍能正常启动并跑通。**

### 阶段 2 · 上安全网（写特征测试，先绿）

`tests/conftest.py` 负责测试库的全生命周期（不依赖人工预建），用例清单见 §8：

- session 级 fixture：用**独立 engine** 执行 `CREATE DATABASE IF NOT EXISTS \`literature_radar_test\` DEFAULT CHARSET utf8mb4 COLLATE utf8mb4_unicode_ci`，再 `Base.metadata.create_all()` 建表；
- function 级 fixture：每个用例结束后清表（按外键顺序 DELETE 或 TRUNCATE），保证用例互不干扰；
- 库名从环境变量/配置读取，**测试库绝不复用生产库 `literature_radar`**。

**要求：这批测试在阶段 1 结束时即全绿**——这就是"地基等价替换"的证明。

### 阶段 3 · 逐个换血

一个文件一个小任务，把 `db.query(...)` 换成原生 ORM，**换完立刻跑测试**。

建议顺序（由简到繁、由外到内）：

1. 只读列表类：`app/routers/settings.py`(4)、`app/routers/followup.py`(5)、`app/routers/unread.py`(6)
2. `app/routers/subscriptions.py`(11)、`app/routers/auth.py`(3)、`app/deps.py`(1)
3. `app/routers/admin.py`(20)、`app/routers/qa.py`(1)
4. 抓取链路：`worker/pipeline.py`(9)、`worker/scheduler.py`(1)（含事务改造）
5. ai 层：`ai/retriever.py`(2)、`ai/orchestrator.py`(2)、`ai/llm_provider.py`(2)
6. 一次性脚本 `_update_model.py`(2)：一并迁移为 ORM 版（保持仓库内所有脚本可用；不删除）

> 括号内为该文件的调用点数量，合计 **14 文件 / 69 处**。

全部换完后：删除 `app/database.py` 旧助手与 `app/schema.sql`。

---

## 8. 测试清单（特征测试）

| # | 用例 | 断言要点 |
|---|---|---|
| 1 | 建表 | 8 张表齐全；各索引/唯一键/外键存在；`papers` 的 ngram 全文索引存在 |
| 2 | 初始化幂等 | `init_database()` 连跑两遍不报错；管理员创建；默认模型写入 + 8 个 agent 绑定 |
| 3 | users | 按用户名查询；`username` 唯一冲突报错；`role` 枚举值正确 |
| 4 | subscriptions | 增删改查；`active` 切换；删用户级联带走订阅 |
| 5 | papers | `(source, external_id)` 唯一去重生效；`external_id` 唯一 |
| 6 | subscription_papers | `(subscription_id, paper_id)` 唯一去重；`is_read` 标记与 `read_at`；未读数统计 |
| 7 | task_logs | 写入；按 `task_id` / `status` 查询 |
| 8 | models / agent_model | `(vendor, model_id)` 唯一；agent 绑定插入与更新 |
| 9 | settings | `user_id` 唯一；读写往返 |
| 10 | **布尔返回值** | `active` / `is_read` / `is_default` 读出为 `True` / `False` |
| 11 | 批量写 | `execute_many` 语义等价：3 条去重插入只落 2 条 |

---

## 9. 验收标准（Definition of Done）

1. `scripts/align_schema.py` 差异清单为空；
2. `pytest` 全绿；
3. 全仓 grep 无 `db.query(` / `db.execute(` / `db.execute_many(` / `mysql.connector` 残留；
4. 实机冒烟：登录 → 订阅列表 → 文献列表与未读标记 → 提问一次 → 触发一次跟进抓取，结果与迁移前一致；
5. 后端正常启动（`uvicorn`），前端 `vite build` 通过。

---

## 10. 风险与兜底

| 风险 | 说明 | 应对 |
|---|---|---|
| R1 | pymysql + `caching_sha2_password` 认证失败 | 阶段 0 预检拦截；补 `cryptography` |
| R2 | 复杂 SQL 用 ORM 表达困难 | 允许 `session.execute(text(...))` 保留原生 SQL（§6.4） |
| R3 | 事务边界改动引入锁/死锁 | worker 事务保持短小；只读路径不 commit |
| R4 | 模型与存量表结构漂移 | `align_schema.py` 作为门禁，差异清单必须为空 |
| R5 | 误伤现有数据 | `create_all` 对已存在表为空操作；迁移前 `mysqldump` 备份 |

**回滚怎么做（具体做法）**：

- 代码层面：按阶段独立提交，每个阶段结束即是一个可运行状态；回退 = 回到该阶段起点的提交；
- 数据层面：本次**不改任何表结构**（`create_all` 只补不存在的表），正常路径无需数据回滚；万一误操作，用阶段 0 的 `mysqldump` 备份恢复；
- 依赖层面：如需退回 mysql-connector，把 requirements/pyproject 里那两行改回来并重装即可。

**兜底**：分层推进，任一阶段可独立回退；阶段 1 完成后即可随时中止而不留半成品（业务代码未动）。

---

## 11. 文档同步清单

- [x] 本文档 `doc/数据库ORM迁移设计.md`
- [ ] `doc/详细设计文档.md`：§2 表结构权威改为 `app/models.py`；数据层描述更新
- [ ] `doc/文档变更记录.md`：追加 §23
- [ ] `doc/项目脉络介绍.md`：数据层段落改写（原生 SQL → SQLAlchemy ORM）
