# 问答结果缓存（进程内 TTL）：同用户 + 同问句 + 同检索形态 → 直接回放上次结果，不再调用上游 LLM
#   为什么先用进程内：服务是单进程 uvicorn（流水线跑在后台线程），进程内字典就够；
#   Redis 只在多 worker / 多机部署时才有必要，届时把本模块的 get / put / clear_user 换成 Redis 实现即可，
#   调用方（orchestrator / worker）不用改。
#   失效口径：TTL 到期 / 条目超上限淘汰最久未访问 / worker 抓取入库后按用户清空 / LITRADAR_QA_CACHE=0 整体关闭
import copy
import os
import threading
import time
from collections import OrderedDict

CACHE_TTL = 1800      # 有效期（秒）：文献库按天更新，30 分钟足够新
CACHE_MAX = 200       # 条目上限（超出后淘汰最久未访问的一条）

_lock = threading.Lock()
_store: "OrderedDict[str, tuple[int, float, dict]]" = OrderedDict()   # key → (user_id, 写入时间, 快照)


def enabled() -> bool:
    """总开关：LITRADAR_QA_CACHE=0 时完全跳过缓存

    评测必须置 0：否则重复跑同一题会直接回放上次答案，命中率与 token 统计全部失真。
    """
    return os.getenv("LITRADAR_QA_CACHE", "1") != "0"


def _ttl() -> int:
    """缓存有效期（秒），LITRADAR_QA_CACHE_TTL 可覆盖"""
    return _env_int("LITRADAR_QA_CACHE_TTL", CACHE_TTL)


def _max_size() -> int:
    """条目上限，LITRADAR_QA_CACHE_MAX 可覆盖"""
    return _env_int("LITRADAR_QA_CACHE_MAX", CACHE_MAX)


def _env_int(name: str, default: int) -> int:
    """正整型环境变量解析：非法值/非正数回落默认"""
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


def normalize_query(query: str) -> str:
    """问句归一化：合并连续空白 + 统一小写

    否则「RAG 是什么」和「 rag  是什么 」会被当成两个问句，各缓存一份。
    """
    return " ".join((query or "").split()).lower()


def make_key(user_id, query: str, mode: str, rewrite: bool, enable_planner: bool) -> str:
    """缓存键：用户隔离 + 归一化问句 + 检索形态 + 改写/Planner 开关

    文献库按 user_id 隔离，所以用户必须进键；mode / rewrite / enable_planner 不同则结果可能不同。
    """
    return "|".join([str(user_id), normalize_query(query), mode,
                     "rw1" if rewrite else "rw0", "p1" if enable_planner else "p0"])


def get(key: str) -> dict | None:
    """读缓存：命中则刷新访问顺序并返回快照副本；过期或不存在 → None

    返回副本：调用方会把快照塞进会话 dict，后续节点可能改写字段，不能污染缓存本体。
    """
    if not enabled():
        return None
    now = time.time()
    with _lock:
        item = _store.get(key)
        if not item:
            return None
        _, written_at, snapshot = item
        if now - written_at > _ttl():
            _store.pop(key, None)
            return None
        _store.move_to_end(key)
        return copy.deepcopy(snapshot)


def put(key: str, user_id, snapshot: dict) -> None:
    """写缓存：先清过期条目，再按上限淘汰最久未访问的条目"""
    if not enabled():
        return
    now = time.time()
    ttl = _ttl()
    with _lock:
        _store.pop(key, None)   # 同键覆盖：重新排到最新
        _store[key] = (user_id, now, copy.deepcopy(snapshot))
        for stale_key, (_, written_at, _) in list(_store.items()):
            if now - written_at > ttl:
                _store.pop(stale_key, None)
        while len(_store) > _max_size():
            _store.popitem(last=False)


def clear_user(user_id) -> int:
    """清掉某用户的全部缓存（worker 抓取入库后调用，避免回放旧文献集的结果）"""
    with _lock:
        dead = [k for k, (uid, _, _) in _store.items() if uid == user_id]
        for key in dead:
            _store.pop(key, None)
        return len(dead)


def clear() -> None:
    """清空全部缓存（测试与排障用）"""
    with _lock:
        _store.clear()
