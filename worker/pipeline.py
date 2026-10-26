# 跟进管道四段式：增量抓取 → 去重 → 三层打分 → 入库/简报（对齐详细设计 §4）
import json
import traceback
from datetime import datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.dialects.mysql import insert as mysql_insert

from app.models import Paper, Subscription, SubscriptionPaper, TaskLog
from app.session import SessionLocal
from app.utils import parse_pub_windows
from ai import qa_cache
from sources import resolve_source
from sources.base import normalize_title
from worker.queue import Task

# LLM 精筛的近阈值带宽与单次上限（成本控制，对齐 AI 方案 §4）
NEAR_THRESHOLD_BAND = 0.1
LLM_FILTER_CAP = 10


def run_followup(task: Task) -> None:
    """执行一次完整跟进：抓取 → 去重 → 筛选 → 入库 → 简报

    payload: {subscription_id, kind, merge(bool), merged_windows(int)}
    """
    payload = task.payload
    sub_id = payload["subscription_id"]
    kind = payload.get("kind", "schedule")
    started = datetime.now()

    with SessionLocal() as session:
        # 列形态 select：mappings() 键即列名，下游抓取函数按 dict 读法零改动
        sub = session.execute(
            select(Subscription.__table__)
            .where(Subscription.__table__.c.id == sub_id)
        ).mappings().first()
        if not sub:
            print(f"[pipeline] 订阅 {sub_id} 不存在，跳过")
            return

        try:
            log = TaskLog(task_id=task.task_id, kind=kind, subscription_id=sub_id,
                          direction_name=sub["name"], status="running", started_at=started)
            session.add(log)
            session.flush()                           # 取到 log.id
            _execute_followup(task, session, sub, log, started)
            session.commit()                          # 整轮写入一次提交
        finally:
            # 释放在途标记，允许调度器下一窗口继续下发
            from worker.scheduler import release
            release(sub_id)


def _execute_followup(task: Task, session, sub: dict, log: TaskLog, started: datetime) -> None:
    """跟进管道主体（异常统一转 FAILED 落库，不推进游标；提交权归 run_followup）"""
    sub_id = sub["id"]
    payload = task.payload
    pulled = 0
    source_errors: list[str] = []
    raw_papers = []
    try:
        # ========== ① 增量抓取（游标 + 时间窗截断，§4.2） ==========
        since = _fetch_start(sub)
        keywords = sub["keywords"] if isinstance(sub["keywords"], list) else json.loads(sub["keywords"])
        sources = sub["sources"] if isinstance(sub["sources"], list) else json.loads(sub["sources"])
        for src_label in sources:
            adapter = resolve_source(src_label)
            if adapter is None:
                source_errors.append(f"{src_label}: 无适配器")
                continue
            try:
                fetched = adapter.fetch(keywords, since, max_results=50)
                raw_papers.extend(fetched)
                pulled += len(fetched)
            except Exception as e:
                # 单来源失败不影响其他来源（PARTIAL_FAILED，§7.1）
                source_errors.append(f"{src_label}: {e}")

        # ========== ② 去重（§4.3：硬去重 → 标题归一化 → FULLTEXT 近似兜底） ==========
        _, kept = _dedup(raw_papers, session)
        deduped = pulled - len(kept)

        # ========== ③ 筛选（三层打分，§4.4） ==========
        scored = _rank_and_filter(kept, sub, keywords)

        # ========== ④ 入库 + 未读（§4.7） ==========
        added = _store(scored, sub_id, session)
        if added:   # 有新文献 → 该用户的问答缓存全部失效，避免回放旧文献集的结果
            qa_cache.clear_user(sub["user_id"])

        # ========== 汇报：状态/游标/简报 ==========
        finished = datetime.now()
        status = "partial_failed" if source_errors else "success"
        brief = _build_brief(sub, added, scored, payload)
        detail = {"brief": brief, "source_errors": source_errors}
        if payload.get("merge") and payload.get("merged_windows", 0) > 1:
            detail["merged_brief"] = f"迟到合并补跑：合并了 {payload['merged_windows']} 个错过的调度窗口"

        session.execute(update(TaskLog).where(TaskLog.id == log.id).values(
            pulled=pulled, added=added, deduped=deduped,
            duration=int((finished - started).total_seconds() * 1000),
            status=status, detail=detail, finished_at=finished))
        # 游标推进：非致命完成即推进。部分来源失败（partial_failed）也推进，
        # 否则永久/长期故障来源会令游标停滞，调度器每分钟重复重跑同一空窗口。
        # 致命异常（failed）不走到这里，游标不推进以便报错后重扫。
        session.execute(update(Subscription).where(Subscription.id == sub_id)
                        .values(last_run_at=started))
        print(f"[pipeline] 跟进完成 sub={sub_id} pulled={pulled} added={added} "
              f"deduped={deduped} status={status}")

    except Exception as e:
        # 阶段致命错误：落 failed，不推进游标，下一窗口重试（§4.8）
        traceback.print_exc()
        session.execute(update(TaskLog).where(TaskLog.id == log.id).values(
            status="failed", detail={"error": str(e)}, finished_at=datetime.now()))


# ---------------- 抓取起点 ----------------

def _fetch_start(sub: dict) -> datetime:
    """游标增量 + pub_window 多选截断：含 3Y+ 不限下界，否则取各窗口下界最早者（§4.2）"""
    now = datetime.now()
    cursor = sub["last_run_at"] or sub["created_at"]
    if isinstance(cursor, str):
        cursor = datetime.fromisoformat(cursor)
    windows = parse_pub_windows(sub["pub_window"])
    if "3Y+" in windows:  # 含 3Y+ 即存在无下界区间，按游标拉取
        return cursor
    bounds = [now - timedelta(days=365 * (3 if w == "1_3Y" else 1)) for w in windows]
    return min([cursor, *bounds])


def _in_pub_window(published_at: str | None, windows: str) -> bool:
    """层1 元数据过滤：多个时间窗取并集，命中任一即保留（§4.4 层1）"""
    selected = parse_pub_windows(windows)
    if not published_at:
        return any(w != "3Y+" for w in selected)  # 无发表时间：纯 3Y+ 才丢弃
    try:
        pub = datetime.strptime(published_at[:10], "%Y-%m-%d")
    except ValueError:
        return True
    now = datetime.now()
    for w in selected:
        if w == "1Y" and pub >= now - timedelta(days=365):
            return True
        if w == "1_3Y" and (now - timedelta(days=365 * 3)) <= pub < (now - timedelta(days=365)):
            return True
        if w == "3Y+" and pub < now - timedelta(days=365 * 3):
            return True
    return False


# ---------------- 去重 ----------------

def _dedup(raw_papers: list, session) -> tuple[int, list]:
    """硬去重（external_id）+ 标题归一化 + FULLTEXT 近似兜底，返回 (去重数, 保留集)"""
    kept: list = []
    seen_ext: set[str] = set()
    seen_titles: set[str] = set()
    for p in raw_papers:
        if p.external_id in seen_ext:
            continue
        nt = normalize_title(p.title)
        if not nt or nt in seen_titles:
            continue
        seen_ext.add(p.external_id)
        seen_titles.add(nt)
        kept.append(p)

    # 库内硬去重：external_id 已存在
    ext_ids = [p.external_id for p in kept]
    existing = set()
    for i in range(0, len(ext_ids), 200):
        batch = ext_ids[i:i + 200]
        rows = session.scalars(
            select(Paper.external_id).where(Paper.external_id.in_(batch))).all()
        existing.update(rows)

    # 库内标题归一化比对（近 90 天同来源标题集合）
    recent = session.scalars(
        select(Paper.title).where(Paper.created_at >= datetime.now() - timedelta(days=90))).all()
    recent_titles = {normalize_title(t) for t in recent}

    result = [p for p in kept
              if p.external_id not in existing
              and normalize_title(p.title) not in recent_titles]
    return (len(raw_papers) - len(result)), result


# ---------------- 三层打分 ----------------

def _keyword_score(paper, keywords: list[str]) -> float:
    """层2 关键词命中打分（BM25 思路简化：题名权重 2 / 摘要权重 1，归一 0~1）"""
    title = (paper.title or "").lower()
    abstract = (paper.abstract or "").lower()
    t_hits = sum(1 for k in keywords if k.lower() in title)
    a_hits = sum(1 for k in keywords if k.lower() in abstract)
    raw = 2 * t_hits + a_hits
    return min(1.0, raw / max(1.0, 2 * len(keywords)))


def _rank_and_filter(papers: list, sub: dict, keywords: list[str]) -> list[tuple[object, float]]:
    """层1 元数据过滤 → 层2 关键词打分 → 层3 可选 LLM 精筛（近阈值样本）"""
    threshold = float(sub["threshold"])
    candidates: list[tuple[object, float]] = []

    for p in papers:
        # 层1：发表时间窗口过滤
        if not _in_pub_window(p.published_at, sub["pub_window"]):
            continue
        # 层2：关键词命中打分
        score = _keyword_score(p, keywords)
        candidates.append((p, score))

    # 层3：LLM 精筛——仅接近阈值的候选（成本可控；无 LLM 时跳过）
    near = [(p, s) for p, s in candidates
            if threshold - NEAR_THRESHOLD_BAND <= s < threshold + NEAR_THRESHOLD_BAND]
    if near:
        adjusted = _llm_refine(near[:LLM_FILTER_CAP], keywords, threshold)
        by_id = {id(p): adj for p, adj in adjusted}
        candidates = [(p, by_id.get(id(p), s)) for p, s in candidates]

    # 阈值过滤 + 按得分排序
    passed = [(p, s) for p, s in candidates if s >= threshold]
    passed.sort(key=lambda x: -x[1])
    return passed


def _llm_refine(near: list, keywords: list[str], threshold: float) -> list[tuple[object, float]]:
    """层3 LLM 精筛：判定相关性并微调分数；任何异常直接原样返回（不阻断）"""
    try:
        from ai.llm_provider import chat_json
    except ImportError:
        return []
    refined: list[tuple[object, float]] = []
    for paper, score in near:
        try:
            verdict = chat_json(
                "qa_router",
                prompt=(
                    "你是文献相关性判定助手。判断以下文献与研究方向的相关性，"
                    '只输出 JSON：{"relevant": true/false, "confidence": 0~1}。\n'
                    f"研究方向关键词：{', '.join(keywords)}\n"
                    f"文献标题：{paper.title}\n"
                    f"摘要：{(paper.abstract or '')[:500]}"
                ),
                timeout=15,
            )
            if isinstance(verdict, dict) and verdict.get("relevant"):
                # 相关：提升到阈值之上通过
                refined.append((paper, threshold + 0.02))
            elif isinstance(verdict, dict):
                refined.append((paper, score * 0.9))  # 不相关：小幅下调
        except Exception:
            continue  # 单条失败保持原分
    return refined


# ---------------- 入库 ----------------

def _store(scored: list[tuple[object, float]], sub_id: int, session) -> int:
    """文献入库（唯一索引硬去重兜底）+ 方向关联（未读），返回新增关联数"""
    added = 0
    for paper, score in scored:
        # upsert 拿 id：ON DUPLICATE KEY UPDATE id = LAST_INSERT_ID(id) 等价原写法
        stmt = (mysql_insert(Paper).values(
            external_id=paper.external_id, source=paper.source, title=paper.title,
            authors=paper.authors, abstract=paper.abstract or "",
            journal=paper.journal or "", published_at=paper.published_at,
            url=paper.url or "", raw_meta=paper.raw_meta,
        ).on_duplicate_key_update(id=func.LAST_INSERT_ID(Paper.id)))
        paper_id = session.execute(stmt).lastrowid

        # 同方向不重复关联同一文献（UNIQUE(subscription_id, paper_id) 兜底）
        inserted = session.execute(
            mysql_insert(SubscriptionPaper).prefix_with("IGNORE")
            .values(subscription_id=sub_id, paper_id=paper_id, score=round(score, 4))
        ).rowcount
        if inserted:
            added += 1
    return added


# ---------------- 简报 ----------------

def _build_brief(sub: dict, added: int, scored: list, payload: dict) -> str:
    """跟进简报：新增汇总说明（迟到合并场景额外说明合并窗口数）"""
    if added == 0:
        return "本次跟进无新增文献（增量窗口内无符合阈值的新文献）"
    top_titles = "；".join(p.title[:50] for p, _ in scored[:3])
    return (
        f"方向「{sub['name']}」本次跟进新增 {added} 篇文献"
        f"（相关度最高：{top_titles}）"
    )
