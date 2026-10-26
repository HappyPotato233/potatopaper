# Semantic Scholar 数据源适配器：官方开放 API（JSON），可选 key 提升限速额度
from datetime import datetime

import httpx

from app.config import settings
from sources.base import PaperRecord, SourceAdapter, fetch_with_backoff

API_URL = "https://api.semanticscholar.org/graph/v1/paper/search"
FIELDS = "title,authors,abstract,year,publicationDate,externalIds,venue,url"


class SemanticScholarAdapter(SourceAdapter):
    name = "Semantic Scholar"

    def fetch(self, keywords: list[str], since: datetime, max_results: int = 50) -> list[PaperRecord]:
        headers = {}
        if settings.S2_API_KEY:
            headers["x-api-key"] = settings.S2_API_KEY
        params = {
            "query": " ".join(keywords),
            "limit": str(min(max_results, 100)),
            "fields": FIELDS,
        }

        def do_fetch() -> httpx.Response:
            return httpx.get(API_URL, params=params, headers=headers, timeout=30)

        resp = fetch_with_backoff(do_fetch, "semanticScholar")
        return self._parse(resp.json(), since)

    def _parse(self, data: dict, since: datetime) -> list[PaperRecord]:
        records: list[PaperRecord] = []
        for p in data.get("data", []) or []:
            pub_date = p.get("publicationDate") or (
                f"{p['year']}-01-01" if p.get("year") else None
            )
            # 增量过滤：只保留 since 之后发表的（S2 检索不支持时间参数，客户端过滤）
            if pub_date:
                try:
                    if datetime.strptime(pub_date, "%Y-%m-%d") < since:
                        continue
                except ValueError:
                    pass
            ext_ids = p.get("externalIds") or {}
            # 硬去重键：DOI 优先，否则用 S2 paperId
            ext_id = ext_ids.get("DOI") or p.get("paperId") or ""
            if not ext_id:
                continue
            if not ext_ids.get("DOI"):
                ext_id = f"s2:{ext_id}"
            records.append(PaperRecord(
                external_id=ext_id,
                source=self.name,
                title=(p.get("title") or "").strip(),
                authors=[a.get("name", "") for a in p.get("authors", []) if a.get("name")],
                abstract=(p.get("abstract") or "").strip(),
                journal=(p.get("venue") or "").strip(),
                published_at=pub_date,
                url=p.get("url") or "",
                raw_meta={"paperId": p.get("paperId")},
            ))
        return records


adapter = SemanticScholarAdapter()
