# 文献雷达（Literature Radar）

填一个研究方向，系统定时去 arXiv、Semantic Scholar、万方抓新论文，去重后入库。
你可以就这个方向直接提问，答案带引用；也可以生成多视角综述，改完大纲导出 Word。

- 前端：Vue 3 + TypeScript，构建后由后端直接托管
- 后端：FastAPI + SQLAlchemy + MySQL 8
- 问答：LangGraph 多 Agent 编排，接 OpenAI 兼容接口的大模型
- 检索：MySQL 全文 / LIKE + 本地 bge 向量，两路召回用 RRF 融合

---

## 功能

- **方向订阅**：选文献源、选发表时间窗（1 年内 / 1–3 年 / 3 年外，可多选）。保存前对每个源试拉 1 条，哪个源不行当场告诉用户，不让脏方向入库。
- **定时跟进**：调度器按方向的到期时间自动跑。单个源失败也照常推进游标，不会因为一个坏源把任务永久卡死。
- **文献库**：按方向查、标记已读 / 未读；站内直接看 PDF 原文，首次打开才下载，缓存按用户分开存，退出登录就删。
- **智能问答**：多 Agent 闭环，按问题复杂度分 quick / normal / deep 三档。答案里的 `[1][2]` 是引用编号，点引用来源能跳回站内阅读页。
- **多视角综述**：按方向取库内文献，生成大纲，支持在线改，最后导出 Word。
- **管理端**：模型池管理、给各个 Agent 绑定模型、操作日志（底层报错只有管理员看得到原文）。

## 目录结构

```
deep_search_pro-master/
├── main.py           # 入口：建库建表 → 启动 worker 和调度器 → 托管前端
├── app/              # Web 层：路由、ORM 模型、schema、登录鉴权
├── ai/               # Agent 层：编排、检索、改写、复审、MCP 工具
├── sources/          # 文献源适配器（arXiv / Semantic Scholar / 万方）+ 全文抓取
├── worker/           # 任务队列 + 定时调度器
├── web/              # 前端源码（web/dist 是构建产物，后端直接托管）
├── data/             # 运行期数据：会话检查点、向量缓存（不入版本库）
├── tests/            # 单测
├── eval/             # 23 题命中率评测
└── doc/              # 设计与变更文档（部署用不到）
```

## 一次问答怎么走

```
前端 /api/qa/ask
      │
      ▼
  rewrite（改写问句、判复杂度、路由）
      ├─ quick ──────────────────────────────> 检索一轮 ─> 汇总 ─> 结束
      │
      └─ plan（Planner 拆任务、排工具）
            │
            ▼
        execute_tools（查库 / 联网 / 引用历史）
            │
            ▼
        summarize（先出草稿）
            │
            ▼
        review（Reviewer 挑毛病）
            ├─ 通过 ─────────────> finalize（定稿）
            └─ 不通过 ──> 回到 plan，带着意见重跑
```

- **quick**：简单问题，跳过 Planner，只查一轮库，汇总给答案。
- **normal**：首轮正常跑，复审不通过就回炉一次。
- **deep**：最多迭代 3 轮，强制复审。
- 会话状态存在 SQLite 检查点里，进程重启后还能接着读进度。

## 核心实现

下面三段都是项目里的真实代码，为了篇幅删了空行和部分注释。

### 1. 多 Agent 闭环：节点与路由

代码在 `ai/orchestrator.py`。图是这么搭的：

```python
def _build_pipeline_graph():
    g = StateGraph(QAGraphState)
    g.add_node("rewrite", _node_rewrite)
    g.add_node("quick", _node_quick)
    g.add_node("plan", _node_plan)
    g.add_node("execute_tools", _node_execute_tools)
    g.add_node("summarize", _node_summarize)
    g.add_node("review", _node_review)
    g.add_node("finalize", _node_finalize)
    g.add_node("fallback", _node_fallback)
    g.set_entry_point("rewrite")

    g.add_conditional_edges("rewrite", _route_after_rewrite,
                            {"quick": "quick", "plan": "plan", "fallback": "fallback"})
    g.add_conditional_edges("plan", _route_after_plan,
                            {"fallback": "fallback", "summarize": "summarize",
                             "execute_tools": "execute_tools"})
    g.add_edge("execute_tools", "summarize")   # 先出草稿，再交给复审
    g.add_edge("summarize", "review")
    g.add_conditional_edges("review", _route_after_review,
                            {"plan": "plan", "finalize": "finalize"})
    g.add_edge("quick", END)
    g.add_edge("fallback", END)
    g.add_edge("finalize", END)
    return g.compile(checkpointer=_PIPELINE_SAVER)
```

这么写的原因：
- 每个节点只干一件事，步骤状态直接推给前端轮询，用户能看到"现在卡在哪一步"。
- 复审不通过就走条件边回 `plan`，不是重跑整条链；轮次上限在 `rewrite` 节点按档位写死，防止死循环。
- `checkpointer` 用 SqliteSaver 落盘，会话和步骤进度不占内存。

### 2. 双路召回 + RRF 融合

代码在 `ai/retriever.py`。一条查询同时走词法路和向量路，按名次融合：

```python
def search_library(query_struct: dict, top_k: int = LIBRARY_TOP_K,
                   user_id: int | None = None,
                   vector_weight: float = 1.0) -> list[dict]:
    """库内检索：词法路（FULLTEXT/LIKE）+ 向量路（本地 bge）双路召回 → RRF 融合

    :param user_id: 传入则按用户订阅做隔离（工具层 library_search 权限校验）
    :param vector_weight: 向量路在 RRF 里的权重（词法路固定 1）；缺省 1.0 即等权
    """
    ranked = _rank_keywords(query_struct, top_k, user_id, vector_weight)
    # 第二组关键词（quick 通道派生的部件名词词集）再召回一次 → 两组名次 RRF 融合
    alt = [k for k in (query_struct.get("alt_keywords") or []) if len(k) >= 2]
    if alt:
        ranked = _fuse_keyword_sets(ranked, _rank_keywords(
            {**query_struct, "keywords": alt}, top_k, user_id, vector_weight))

    # 元数据增强（零 LLM）→ 统一证据结构
    ranked = enhance_meta(ranked[:top_k])
    return [_to_evidence(r, origin="library") for r in ranked]
```

这么写的原因：
- 语料以英文为主，直接用中文原句做 BM25 排序基本没命中，所以排序查询用本轮关键词，不用原问句。
- 过滤条件只在零结果时按"先去年份、再去来源"逐级放宽，用户隔离条件永远不放宽。
- 向量模型不可用或编码异常时，这一路直接返回空，主流程回落单路词法，不会把问答整个打断。

### 3. 文献源适配：加一个源只改一个文件

代码在 `sources/__init__.py`：

```python
from sources.base import PaperRecord, SourceAdapter, get_adapter, register_adapter
from sources.arxiv import adapter as _arxiv
from sources.semantic_scholar import adapter as _s2
from sources.wanfang import adapter as _wanfang

register_adapter(_arxiv)
register_adapter(_s2)
register_adapter(_wanfang)

# 前端传的是中文名「万方」，这里映射到注册名
SOURCE_LABELS = {"arXiv": "arXiv", "Semantic Scholar": "Semantic Scholar",
                 "万方": "Wanfang", "Wanfang": "Wanfang"}


def resolve_source(label: str) -> SourceAdapter | None:
    return get_adapter(label) or get_adapter(SOURCE_LABELS.get(label, ""))
```

这么写的原因：
- 每个源只管把外部返回转成统一的 `PaperRecord`，限速、退避、翻页都收在适配器里；上层调度和入库完全不用知道是哪个源。
- 试拉预检直接复用同一套适配器，提交方向和正式爬取走同一条代码路径，不会出现"试拉能过、正式跑挂"。

## 快速开始

环境：Python 3.11+、Node 20+、MySQL 8（库和表启动时自动建，不用手动执行 SQL）。

1. 配环境变量

```bash
cp .env.example .env
```

至少填这几项：`MYSQL_HOST / MYSQL_PORT / MYSQL_USER / MYSQL_PASSWORD / MYSQL_DATABASE`、`JWT_SECRET_KEY`、`LLM_API_KEY / LLM_BASE_URL / LLM_MODEL_NAME`。

2. 装依赖

```bash
pip install -r requirements.txt
```

3. 起服务

```bash
uvicorn main:app --host 127.0.0.1 --port 8000
```

Windows 也可以双击 `start.bat`。注意脚本里写死了本机的 Python 路径，换机器先改那一行。

4. 打开 <http://127.0.0.1:8000>

首次启动会自动建库建表，并按 `.env` 里的 `ADMIN_USERNAME / ADMIN_PASSWORD` 创建管理员。

5. 前端开发与发布

```bash
cd web
npm install
npm run dev     # 5173，/api 已代理到 8000
npm run build   # 发布前跑，产物 web/dist 由后端托管
```

## 配置项

| 变量 | 默认 | 作用 |
| --- | --- | --- |
| `LITRADAR_QA_MAX_CONCURRENCY` | 10 | 同时跑几条问答流水线，超出的排队 |
| `LITRADAR_QA_QUEUE_TIMEOUT` | 20 | 排队上限（秒），超时直接返回"请求过多" |
| `LITRADAR_QA_CACHE` | 1 | 问答缓存开关，评测时置 0 |
| `LITRADAR_QA_CACHE_TTL` | 1800 | 缓存有效期（秒） |
| `LITRADAR_RRF_K` | 10 | RRF 平滑常数，越小越看重头部名次 |
| `LITRADAR_VECTOR_RECALL` | 1 | 向量召回开关，置 0 退回单路词法 |
| `LITRADAR_QUICK_VECTOR_WEIGHT` | 1.8 | quick 通道向量路权重 |
| `LITRADAR_EMBEDDING_MODEL` | `BAAI/bge-large-en-v1.5` | 本地向量模型目录 |
| `LITRADAR_EMBEDDING_DEVICE` | cpu | 编码设备（cpu / cuda） |

## 测试与评测

```bash
pytest                              # 单测
LITRADAR_QA_CACHE=0 python eval/run_eval.py   # 23 题命中率评测
```

评测必须关缓存，否则同一道题会直接回放上次结果，命中率会失真。

## 已知限制

- 向量召回依赖 `sentence-transformers` 和本地模型目录，缺任意一个就只走词法路，问答还能用，只是召回变差。
- quick 通道只取一轮词，中文长问句效果一般；要稳一点就用 normal / deep。
- 本地小模型做跨文献归纳的深度有限，关键结论建议回原文核对。
- 当前按单进程部署设计：并发上限、排队、缓存都是进程内状态，上多 worker 要换成共享缓存。
- Windows 下 MCP 子进程首次加载 C 扩展需要预热，冷启动会多等几秒。

## 注意

- `.env`、`BAAI/`、`data/`、`web/dist/`、`web/node_modules/` 都不进版本库。
- 上线前必须改掉默认管理员口令和 `JWT_SECRET_KEY`。
