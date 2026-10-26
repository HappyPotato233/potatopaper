# 展示前元数据增强：本地静态映射（期刊等级/CCF/中科院分区/arXiv 分类）
# 零 LLM、不入库、代码级注入（对齐详细设计 §4.6）：ISSN → 期刊名 多级回退匹配
import json
import re
from functools import lru_cache
from pathlib import Path

META_FILE = Path(__file__).resolve().parents[1] / "data" / "journal_meta.json"


@lru_cache(maxsize=1)
def _load_meta() -> dict:
    """加载静态映射表：{entries: [{journal, aliases, issn, level, ccf, cas_zone}], arxiv_cats: {...}}"""
    try:
        return json.loads(META_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"entries": [], "arxiv_cats": {}}


def _match_entry(journal: str, issn: str = "") -> dict | None:
    """多级回退匹配：ISSN 精确 → 期刊全名 → 别名包含"""
    if not journal and not issn:
        return None
    meta = _load_meta()
    issn = (issn or "").strip().upper()
    if issn:
        for e in meta.get("entries", []):
            if e.get("issn", "").upper() == issn:
                return e
    name = (journal or "").strip().lower()
    if name:
        for e in meta.get("entries", []):
            if e.get("journal", "").lower() == name:
                return e
        for e in meta.get("entries", []):
            for alias in e.get("aliases", []):
                if alias.lower() in name or name in alias.lower():
                    return e
    return None


def _arxiv_category(paper: dict) -> str:
    """arXiv 分类：优先 raw_meta.primary_category，其次按标题关键词粗判"""
    raw = paper.get("raw_meta")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = {}
    if isinstance(raw, dict) and raw.get("primary_category"):
        cat = raw["primary_category"]
        label = _load_meta().get("arxiv_cats", {}).get(cat, "")
        return f"{cat} {label}".strip()
    return ""


def enhance_meta(papers: list[dict]) -> list[dict]:
    """rerank 之后、展示/分发之前注入元数据增强字段（原地补充并返回）"""
    for p in papers:
        entry = _match_entry(p.get("journal") or "", p.get("issn") or "")
        p["journal_level"] = (entry or {}).get("level", "")
        p["ccf"] = (entry or {}).get("ccf", "")
        p["cas_zone"] = (entry or {}).get("cas_zone", "")
        p["arxiv_cat"] = _arxiv_category(p) if p.get("source") == "arXiv" else ""
    return papers
