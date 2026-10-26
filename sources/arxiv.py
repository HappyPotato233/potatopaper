# arXiv 数据源适配器：官方开放 API（Atom XML），无需 key
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from urllib.parse import quote

import httpx

from sources.base import PaperRecord, SourceAdapter, fetch_with_backoff

ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}
API_URL = "https://export.arxiv.org/api/query"  # 强制 https（http 会 301）


def _clean_text(text: str | None) -> str:
    """arXiv 返回的标题/摘要含多余换行与空白，压成单行"""
    return re.sub(r"\s+", " ", (text or "")).strip()


class ArxivAdapter(SourceAdapter):
    name = "arXiv"

    def fetch(self, keywords: list[str], since: datetime, max_results: int = 50) -> list[PaperRecord]:
        # 组装检索式：任一关键词命中（all: 字段），并限制提交时间下界
        kw_query = " OR ".join(f'all:"{k}"' for k in keywords)
        date_lower = since.strftime("%Y%m%d%H%M")
        search_query = f"({kw_query}) AND submittedDate:[{date_lower} TO 999912312359]"
        params = {
            "search_query": search_query,
            "sortBy": "submittedDate",
            "sortOrder": "descending",
            "max_results": str(max_results),
        }

        def do_fetch() -> httpx.Response:
            return httpx.get(
                API_URL, params=params, headers={"User-Agent": "LiteratureRadar/2.0"},
                timeout=30, follow_redirects=True,
            )

        resp = fetch_with_backoff(do_fetch, "arxiv")
        return self._parse(resp.text)

    def _parse(self, xml_text: str) -> list[PaperRecord]:
        records: list[PaperRecord] = []
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            return records
        for entry in root.findall("atom:entry", ATOM_NS):
            # external_id 取短 id（如 2401.12345v1 → 2401.12345），去版本号保证跨版本去重
            entry_id = entry.findtext("atom:id", "", ATOM_NS)  # http://arxiv.org/abs/2401.12345v1
            m = re.search(r"abs/([^v]+)", entry_id)
            if not m:
                continue
            ext_id = f"arxiv:{m.group(1)}"
            published = entry.findtext("atom:published", "", ATOM_NS)[:10]  # YYYY-MM-DD
            authors = [
                _clean_text(a.findtext("atom:name", "", ATOM_NS))
                for a in entry.findall("atom:author", ATOM_NS)
            ]
            journal_ref = _clean_text(entry.findtext("atom:arxiv:journal_ref", namespaces={
                **ATOM_NS, "arxiv": "http://arxiv.org/schemas/atom"
            })) if entry.find("atom:arxiv:journal_ref", {
                **ATOM_NS, "arxiv": "http://arxiv.org/schemas/atom"
            }) is not None else ""
            records.append(PaperRecord(
                external_id=ext_id,
                source=self.name,
                title=_clean_text(entry.findtext("atom:title", "", ATOM_NS)),
                authors=[a for a in authors if a],
                abstract=_clean_text(entry.findtext("atom:summary", "", ATOM_NS)),
                journal=journal_ref,
                published_at=published or None,
                url=entry_id,
                raw_meta={"api": "arxiv"},
            ))
        return records


adapter = ArxivAdapter()
