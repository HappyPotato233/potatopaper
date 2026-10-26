# 未读/文献路由：未读汇总、方向文献列表（rerank 后元数据增强展示）、已读标记（对齐详细设计 §3.3）
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import case, func, select, update
from sqlalchemy.orm import Session

from ai.meta_enhance import enhance_meta
from app.deps import get_current_user
from app.models import Paper, Subscription, SubscriptionPaper
from app.schemas.paper import (BatchReadRequest, PaperOut, UnreadItem, UnreadSummary)
from app.session import get_db
from app.storage import clear_stale_pdfs, user_pdf_dir
from app.utils import jloads
from sources.fulltext import fetch_full_text, fetch_pdf_bytes

router = APIRouter(prefix="/api", tags=["unread"])


def _paper_query(session: Session, user_id: int, sub_id: int, only_unread: bool) -> list[dict]:
    """方向文献列表：关联 + 打分排序；列表侧同样走元数据增强（零 LLM）"""
    # 显式列投影：不选 sp.id，避免覆盖 p.id 的映射键；结果为"列名为键"的 dict，供 enhance_meta 原地增强
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
    rows = [dict(r) for r in session.execute(stmt).mappings().all()]
    return enhance_meta(rows)


def _to_out(row: dict) -> PaperOut:
    return PaperOut(
        id=row["id"], external_id=row.get("external_id"), source=row["source"],
        title=row["title"], authors=jloads(row.get("authors"), []),
        abstract=row.get("abstract") or "", journal=row.get("journal") or "",
        published_at=str(row["published_at"]) if row.get("published_at") else None,
        url=row.get("url"),
        score=float(row.get("score") or 0.0), is_read=bool(row.get("is_read")),
        journal_level=row.get("journal_level", ""), ccf=row.get("ccf", ""),
        cas_zone=row.get("cas_zone", ""), arxiv_cat=row.get("arxiv_cat", ""),
    )


@router.get("/unread/summary", response_model=UnreadSummary)
def unread_summary(user: dict = Depends(get_current_user),
                   session: Session = Depends(get_db)):
    """总未读 + 各方向未读（徽标数据源）"""
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
    return UnreadSummary(total_unread=sum(i.unread for i in items), items=items)


@router.get("/unread/{sub_id}", response_model=list[PaperOut])
def unread_papers(sub_id: int, user: dict = Depends(get_current_user),
                  session: Session = Depends(get_db)):
    """某方向未读文献列表"""
    return [_to_out(r) for r in _paper_query(session, user["id"], sub_id, only_unread=True)]


@router.get("/subscriptions/{sub_id}/papers", response_model=list[PaperOut])
def direction_papers(sub_id: int, user: dict = Depends(get_current_user),
                     session: Session = Depends(get_db)):
    """某方向全部文献（含已读；工作台方向详情数据源）"""
    return [_to_out(r) for r in _paper_query(session, user["id"], sub_id, only_unread=False)]


@router.get("/papers/{paper_id}", response_model=PaperOut)
def paper_detail(paper_id: int, user: dict = Depends(get_current_user),
                 session: Session = Depends(get_db)):
    """单篇文献详情（阅读页数据源；校验文献属于当前用户某方向）"""
    stmt = (select(Paper.id, Paper.external_id, Paper.source, Paper.title, Paper.authors,
                   Paper.abstract, Paper.journal, Paper.published_at, Paper.url,
                   Paper.raw_meta, Paper.created_at,
                   SubscriptionPaper.score, SubscriptionPaper.is_read)
            .join(SubscriptionPaper, SubscriptionPaper.paper_id == Paper.id)
            .join(Subscription, Subscription.id == SubscriptionPaper.subscription_id)
            .where(Paper.id == paper_id, Subscription.user_id == user["id"])
            .order_by(SubscriptionPaper.score.desc()).limit(1))
    row = session.execute(stmt).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="文献不存在或不属于当前用户")
    return _to_out(enhance_meta([dict(row)])[0])


@router.get("/papers/{paper_id}/fulltext")
def paper_fulltext(paper_id: int, user: dict = Depends(get_current_user),
                   session: Session = Depends(get_db)):
    """论文全文：首次访问按需抓取并缓存到 papers.full_text，之后直接读缓存"""
    stmt = (select(Paper.id, Paper.source, Paper.external_id, Paper.url, Paper.full_text)
            .join(SubscriptionPaper, SubscriptionPaper.paper_id == Paper.id)
            .join(Subscription, Subscription.id == SubscriptionPaper.subscription_id)
            .where(Paper.id == paper_id, Subscription.user_id == user["id"]).limit(1))
    row = session.execute(stmt).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="文献不存在或不属于当前用户")
    if row["full_text"]:
        return {"available": True, "cached": True, "text": row["full_text"]}

    text = fetch_full_text(row["source"], row["external_id"], row["url"])
    if not text:
        return {"available": False, "cached": False, "text": "",
                "message": "暂未获取到该文献全文，可点击下方「打开原文」查看"}
    session.execute(update(Paper).where(Paper.id == paper_id)
                    .values(full_text=text, full_text_at=func.now()))
    session.commit()
    return {"available": True, "cached": False, "text": text}


@router.get("/papers/{paper_id}/pdf")
def paper_pdf(paper_id: int, user: dict = Depends(get_current_user),
              session: Session = Depends(get_db)):
    """论文 PDF 原文：首次访问下载到用户临时目录，登出时清理"""
    stmt = (select(Paper.source, Paper.external_id, Paper.url)
            .join(SubscriptionPaper, SubscriptionPaper.paper_id == Paper.id)
            .join(Subscription, Subscription.id == SubscriptionPaper.subscription_id)
            .where(Paper.id == paper_id, Subscription.user_id == user["id"]).limit(1))
    row = session.execute(stmt).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="文献不存在或不属于当前用户")

    clear_stale_pdfs()
    path = user_pdf_dir(user["id"]) / f"{paper_id}.pdf"
    if not path.exists():
        data = fetch_pdf_bytes(row["source"], row["external_id"], row["url"])
        if not data:
            raise HTTPException(status_code=404, detail="暂未获取到该文献的 PDF 原文")
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".part")
        tmp.write_bytes(data)
        tmp.replace(path)

    return FileResponse(path, media_type="application/pdf",
                        headers={"Content-Disposition": f'inline; filename="paper-{paper_id}.pdf"'})


@router.post("/papers/{paper_id}/read")
def mark_read(paper_id: int, user: dict = Depends(get_current_user),
              session: Session = Depends(get_db)):
    """单篇标记已读（校验文献属于用户某方向）"""
    owned = session.scalar(
        select(SubscriptionPaper.id)
        .join(Subscription, Subscription.id == SubscriptionPaper.subscription_id)
        .where(SubscriptionPaper.paper_id == paper_id,
               Subscription.user_id == user["id"]).limit(1))
    if owned is None:
        raise HTTPException(status_code=404, detail="文献不存在或不属于当前用户")
    # 只更新当前用户自己方向下的关联，避免把别人方向的阅读状态一起改掉
    session.execute(update(SubscriptionPaper)
                    .where(SubscriptionPaper.paper_id == paper_id,
                           SubscriptionPaper.subscription_id.in_(
                               select(Subscription.id).where(Subscription.user_id == user["id"])))
                    .values(is_read=True, read_at=func.now()))
    session.commit()
    return {"code": 0, "message": "已标记已读"}


@router.post("/papers/batch-read")
def batch_read(body: BatchReadRequest, user: dict = Depends(get_current_user),
               session: Session = Depends(get_db)):
    """批量标记已读：{subscription_id, paper_ids[]}"""
    sub = session.scalar(select(Subscription.id).where(
        Subscription.id == body.subscription_id, Subscription.user_id == user["id"]))
    if sub is None:
        raise HTTPException(status_code=404, detail="订阅不存在")
    ids = body.paper_ids[:500]  # 单次上限保护
    # IN 交给 in_() 参数化（原手拼占位符整段删除）
    affected = session.execute(
        update(SubscriptionPaper)
        .where(SubscriptionPaper.subscription_id == body.subscription_id,
               SubscriptionPaper.paper_id.in_(ids),
               SubscriptionPaper.is_read.is_(False))
        .values(is_read=True, read_at=func.now())).rowcount
    session.commit()
    return {"code": 0, "message": f"已标记 {affected} 篇为已读"}
