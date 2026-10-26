# 万方数据源适配器：无公开免费 API，搜索页 JS 渲染——轻量 HTTP 尝试 + 优雅降级
# 实测记录（详细设计 §3.7）：页面可正常加载，但需无头浏览器渲染 DOM；
# 本适配器做轻量尝试（合理 UA/Referer + 页面内嵌 JSON 提取），失败返回空列表记 partial_failed，
# 不安装 Playwright 重依赖（后续升级项）。
import json
import re
from datetime import datetime

import httpx

from sources.base import PaperRecord, SourceAdapter, SourceUnavailableError, fetch_with_backoff

SEARCH_URL = "https://s.wanfangdata.com.cn/so/paper"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Referer": "https://s.wanfangdata.com.cn/",
    "Accept": "text/html,application/xhtml+xml",
}


class WanfangAdapter(SourceAdapter):
    name = "Wanfang"

    def fetch(self, keywords: list[str], since: datetime, max_results: int = 30) -> list[PaperRecord]:
        q = " ".join(keywords)

        def do_fetch() -> httpx.Response:
            return httpx.get(
                SEARCH_URL, params={"q": q}, headers=HEADERS, timeout=20,
                follow_redirects=True,
            )

        try:
            resp = fetch_with_backoff(do_fetch, "wanfang", max_retries=1)
        except httpx.HTTPError as e:
            # 网络/渲染失败：抛出具体原因，由 pipeline/试拉记录（不再静默返回空）
            raise SourceUnavailableError(f"万方请求失败：{e}") from e
        records = self._parse(resp.text, q)
        if not records:
            # 页面可访问但拿不到数据（搜索页依赖 JS 渲染）：明确归为来源不可用
            raise SourceUnavailableError("万方搜索页未返回可解析的文献数据（需浏览器渲染）")
        return records

    def _parse(self, html: str, query: str) -> list[PaperRecord]:
        """从页面内嵌 JSON（window.__INITIAL_STATE__ / script 数据）尽力抽取；失败返回空"""
        records: list[PaperRecord] = []
        # 尝试提取页面内嵌的 JSON 状态
        for m in re.finditer(r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\});?\s*</script>", html, re.S):
            try:
                self._walk(json.loads(m.group(1)), query, records)
            except (json.JSONDecodeError, ValueError):
                continue
            if records:
                break
        return records

    def _walk(self, node, query: str, out: list[PaperRecord], depth: int = 0) -> None:
        """递归遍历 JSON 树，找到形如论文列表的数组（title/authors/年份字段组合）"""
        if depth > 6 or len(out) >= 30:
            return
        if isinstance(node, dict):
            title = node.get("title") or node.get("paperTitle")
            if isinstance(title, str) and 5 < len(title) < 500:
                authors = node.get("authors") or node.get("author") or []
                if isinstance(authors, str):
                    authors = [a.strip() for a in re.split(r"[;,，；]", authors) if a.strip()]
                elif isinstance(authors, list):
                    authors = [a.get("name", "") if isinstance(a, dict) else str(a) for a in authors]
                year = node.get("year") or node.get("publishYear") or ""
                pub = f"{year}-01-01" if str(year).isdigit() else None
                ext = node.get("id") or node.get("paperId") or ""
                if ext:
                    out.append(PaperRecord(
                        external_id=f"wanfang:{ext}",
                        source=self.name,
                        title=title.strip(),
                        authors=authors,
                        abstract=(node.get("abstract") or node.get("summary") or "")[:2000],
                        journal=(node.get("journal") or node.get("sourceTitle") or "")[:300],
                        published_at=pub,
                        url=node.get("url") or "",
                        raw_meta={"query": query},
                    ))
            for v in node.values():
                self._walk(v, query, out, depth + 1)
        elif isinstance(node, list):
            for item in node:
                self._walk(item, query, out, depth + 1)


adapter = WanfangAdapter()
