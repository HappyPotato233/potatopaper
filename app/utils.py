# 通用工具：MySQL JSON 列读取值的宽容解析（str/list/dict → 目标类型）
import json
from datetime import datetime

# 发表时间窗的可选值：库里按逗号分隔存多选结果（如 '1Y,3Y+'），顺序固定
PUB_WINDOWS = ("1Y", "1_3Y", "3Y+")


def parse_pub_windows(raw) -> list[str]:
    """'1Y,3Y+' → ['1Y', '3Y+']：丢弃未知值、按固定顺序去重；空值回落默认 ['1Y']"""
    tokens = {t.strip() for t in str(raw or "").split(",")}
    return [w for w in PUB_WINDOWS if w in tokens] or ["1Y"]


def jloads(value, default):
    """JSON 列宽容解析：None/解析失败返回 default；已是 list/dict 原样返回"""
    if value is None:
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, type(default)) else default
    except (json.JSONDecodeError, TypeError):
        return default


def fmt_dt(value) -> str | None:
    """datetime → 'YYYY-MM-DD HH:MM:SS'（JSON 序列化统一格式）"""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    return str(value) if value else None
