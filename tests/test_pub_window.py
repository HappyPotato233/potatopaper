# 用例：pub_window 多选时间窗（CSV 解析 / 入参校验 / 并集过滤 / 抓取下界）
from datetime import datetime, timedelta

import pytest
from pydantic import ValidationError

from app.schemas.subscription import SubscriptionCreate
from app.utils import parse_pub_windows
from worker.pipeline import _fetch_start, _in_pub_window


def _days_ago(days: int) -> str:
    return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")


def test_parse_pub_windows_dedup_and_order():
    """CSV 解析：去空白、去重、按固定顺序，未知值丢弃"""
    assert parse_pub_windows("3Y+,1Y") == ["1Y", "3Y+"]
    assert parse_pub_windows("1_3Y, 1_3Y ,1Y") == ["1Y", "1_3Y"]
    assert parse_pub_windows("9Y,1Y") == ["1Y"]
    assert parse_pub_windows(None) == ["1Y"]
    assert parse_pub_windows("") == ["1Y"]


def test_subscription_schema_normalizes_multi_window():
    """入参校验：多值归一成固定顺序，默认单值仍可用"""
    body = SubscriptionCreate(name="x", keywords=["a"], sources=["arXiv"],
                              pub_window="3Y+,1Y")
    assert body.pub_window == "1Y,3Y+"
    assert SubscriptionCreate(name="x", keywords=["a"], sources=["arXiv"]).pub_window == "1Y"


def test_subscription_schema_rejects_empty_and_invalid():
    """入参校验：空值、非法值直接报错"""
    with pytest.raises(ValidationError):
        SubscriptionCreate(name="x", keywords=["a"], sources=["arXiv"], pub_window="")
    with pytest.raises(ValidationError):
        SubscriptionCreate(name="x", keywords=["a"], sources=["arXiv"], pub_window="1Y,5Y")


def test_in_pub_window_union():
    """层1 过滤：多窗口取并集，空档区间被排除"""
    assert _in_pub_window(_days_ago(10), "1Y,3Y+") is True       # 命中 1Y
    assert _in_pub_window(_days_ago(5 * 365), "1Y,3Y+") is True  # 命中 3Y+
    assert _in_pub_window(_days_ago(500), "1Y,3Y+") is False     # 落在 1-3 年空档
    assert _in_pub_window(_days_ago(500), "1_3Y") is True
    assert _in_pub_window(_days_ago(500), "1Y") is False


def test_in_pub_window_missing_published_at():
    """无发表时间：纯 3Y+ 丢弃，含其它窗口则保留"""
    assert _in_pub_window(None, "3Y+") is False
    assert _in_pub_window(None, "1Y,3Y+") is True
    assert _in_pub_window(None, "1Y") is True


def test_fetch_start_multi_window():
    """抓取下界：含 3Y+ 按游标拉取，否则取所选窗口最早下界"""
    now = datetime.now()
    cursor = now - timedelta(days=10)
    sub = {"last_run_at": cursor, "created_at": cursor, "pub_window": "3Y+,1Y"}
    assert _fetch_start(sub) == cursor

    sub["pub_window"] = "1Y,1_3Y"
    start = _fetch_start(sub)
    assert abs((start - (now - timedelta(days=365 * 3))).total_seconds()) < 60

    sub["pub_window"] = "1Y"
    start = _fetch_start(sub)
    assert abs((start - (now - timedelta(days=365))).total_seconds()) < 60
