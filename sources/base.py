# 数据源适配器基类：规范化文献记录 + 限速退避 + 适配器注册表
import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, TypeVar

import httpx

T = TypeVar("T")

# 每个来源的全局限速：上次请求时间戳（来源名 → 时间戳）
_last_request_at: dict[str, float] = {}
# 各来源最小请求间隔（秒），遵守上游限速条款
MIN_INTERVALS = {"arxiv": 3.0, "semanticScholar": 1.2, "wanfang": 2.0}

DEFAULT_HEADERS = {
    "User-Agent": "LiteratureRadar/2.0 (research tracking assistant; academic use)",
    "Accept": "application/json",
}


class SourceUnavailableError(Exception):
    """数据源不可用（网络异常/限流/页面无法解析）：由调用方转成失败提示"""


@dataclass
class PaperRecord:
    """来源无关的规范化文献记录（入库前的中间形态）"""
    external_id: str
    source: str
    title: str
    authors: list[str] = field(default_factory=list)
    abstract: str = ""
    journal: str = ""
    published_at: str | None = None  # YYYY-MM-DD
    url: str = ""
    raw_meta: dict = field(default_factory=dict)


def normalize_title(title: str) -> str:
    """标题归一化：去大小写/空格/标点（标题去重依据，对齐详细设计 §4.3）"""
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", title or "").lower()


def rate_limit_wait(source: str) -> None:
    """简单限速：保证同来源请求间隔不小于 MIN_INTERVALS"""
    interval = MIN_INTERVALS.get(source, 1.0)
    last = _last_request_at.get(source)
    if last is not None:
        elapsed = time.time() - last
        if elapsed < interval:
            time.sleep(interval - elapsed)
    _last_request_at[source] = time.time()


def fetch_with_backoff(
    fetch_fn: Callable[[], httpx.Response],
    source: str,
    max_retries: int = 3,
    base_delay: float = 2.0,
) -> httpx.Response:
    """指数退避（2^n 秒 + 抖动，上界 60s，单次跟进内最多重试 3 次）

    :raises httpx.HTTPError: 重试耗尽后抛出，由调用方记 partial_failed
    """
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        rate_limit_wait(source)
        try:
            resp = fetch_fn()
            if resp.status_code == 429 or resp.status_code >= 500:
                last_exc = httpx.HTTPStatusError(
                    f"上游返回 {resp.status_code}", request=resp.request, response=resp
                )
            else:
                resp.raise_for_status()
                return resp
        except httpx.HTTPError as e:
            last_exc = e
        if attempt < max_retries:
            delay = min(60.0, base_delay ** (attempt + 1)) + random.uniform(0, 1)
            time.sleep(delay)
    raise last_exc  # type: ignore[misc]


class SourceAdapter:
    """数据源适配器接口：子类实现 fetch，返回规范化 PaperRecord 列表"""
    name: str = "base"

    def fetch(self, keywords: list[str], since: datetime, max_results: int = 50) -> list[PaperRecord]:
        """按关键词与增量游标抓取文献（since 之后发表/提交的）"""
        raise NotImplementedError


# 适配器注册表（sources/__init__ 时填充）
_REGISTRY: dict[str, SourceAdapter] = {}


def register_adapter(adapter: SourceAdapter) -> None:
    _REGISTRY[adapter.name] = adapter


def get_adapter(source: str) -> SourceAdapter | None:
    return _REGISTRY.get(source)


def available_sources() -> list[str]:
    return list(_REGISTRY.keys())
