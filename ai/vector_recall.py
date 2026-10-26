# 向量召回层（dense recall）：本地 bge-large-en-v1.5 编码 + 余弦相似度 + 向量缓存
#   定位：与词法路（MySQL FULLTEXT / LIKE）并行的第二路召回；两路融合在 ai/retriever.py 用 RRF 做
#   降级：模型目录缺失 / 编码异常 → 返回空列表，主流程回落单路词法召回（失败回退原则）
#   缓存：data/embedding_cache_bge.npz（文献 id → 文本哈希 + 归一化向量），文本变更即失效重算
import hashlib
import os
import threading
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
# 本地模型目录（默认项目根 BAAI/bge-large-en-v1.5，可用环境变量覆盖）
MODEL_DIR = Path(os.getenv("LITRADAR_EMBEDDING_MODEL") or (ROOT / "BAAI" / "bge-large-en-v1.5"))
CACHE_FILE = Path(os.getenv("LITRADAR_EMBEDDING_CACHE")
                  or (ROOT / "data" / "embedding_cache_bge.npz"))
DEVICE = os.getenv("LITRADAR_EMBEDDING_DEVICE", "cpu")
ENCODE_BATCH = 16
DOC_CHARS = 1024   # 单篇参与编码的文本长度（title + abstract 截断，控 CPU 编码耗时）
# bge-en-v1.5 短查询→长文档场景的官方指令前缀（文档侧不加前缀）
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "

_lock = threading.Lock()
_model = None                                    # SentenceTransformer 实例（惰性加载）
_model_error = ""                                # 加载失败原因（只报一次，避免重复刷日志）
_cache: dict[str, tuple[str, np.ndarray]] = {}   # 文献 id → (文本哈希, 归一化向量)
_cache_loaded = False


# ---------- 对外接口 ----------

def available() -> bool:
    """模型是否可用于向量召回（只做廉价探测：本地目录齐备 + 依赖可导入）"""
    if _model is not None:
        return True
    if not (MODEL_DIR / "config.json").exists() or not (MODEL_DIR / "model.safetensors").exists():
        return False
    try:
        import sentence_transformers  # noqa: F401
    except Exception:
        return False
    return True


def rank(query: str, rows: list[dict]) -> list[dict]:
    """按向量相似度降序返回候选：不可用/异常时返回 []，由调用方回落词法路

    :param query: 自然语言查询（内部自动补 bge 官方指令前缀）
    :param rows: 候选文献行（须含 id / title / abstract）
    """
    if not rows or not (query or "").strip():
        return []
    model = _get_model()
    if model is None:
        return []
    try:
        with _lock:
            vectors = _ensure_vectors(model, rows)
            query_vec = model.encode(QUERY_PREFIX + query, normalize_embeddings=True,
                                     convert_to_numpy=True)
        sims = vectors @ np.asarray(query_vec, dtype=np.float32).reshape(-1)
    except Exception as e:   # 编码/打分异常 → 回落词法路，不阻断检索
        print(f"[vector_recall] 向量打分失败，回落单路词法召回: {e}")
        return []
    order = np.argsort(-sims)
    return [rows[int(i)] for i in order]


# ---------- 模型与缓存 ----------

def _get_model():
    """惰性加载本地模型（线程安全；加载失败只记一次原因并永久降级）"""
    global _model, _model_error
    if _model is not None or _model_error:
        return _model
    with _lock:
        if _model is not None or _model_error:
            return _model
        try:
            from sentence_transformers import SentenceTransformer
            _model = SentenceTransformer(str(MODEL_DIR), device=DEVICE)
        except Exception as e:
            _model_error = f"{type(e).__name__}: {e}"
            print(f"[vector_recall] 本地模型加载失败，退化为单路词法召回: {_model_error}")
    return _model


def _ensure_vectors(model, rows: list[dict]) -> np.ndarray:
    """返回 rows 顺序对齐的归一化向量矩阵；缺缓存/文本变更的条目补算后落盘"""
    _load_cache()
    texts = [_doc_text(r) for r in rows]
    missing = [i for i, t in enumerate(texts)
               if _cache.get(str(rows[i]["id"]), ("", None))[0] != _doc_hash(t)]
    if missing:
        batch = model.encode([texts[i] for i in missing], batch_size=ENCODE_BATCH,
                             normalize_embeddings=True, convert_to_numpy=True)
        for i, vec in zip(missing, np.asarray(batch, dtype=np.float32)):
            _cache[str(rows[i]["id"])] = (_doc_hash(texts[i]), vec)
        _save_cache()
    return np.stack([_cache[str(r["id"])][1] for r in rows])


def _load_cache() -> None:
    """加载磁盘缓存（进程内只做一次；读取失败则当空缓存重建）"""
    global _cache_loaded
    if _cache_loaded:
        return
    _cache_loaded = True
    if not CACHE_FILE.exists():
        return
    try:
        data = np.load(CACHE_FILE, allow_pickle=False)
        for pid, digest, vec in zip(data["ids"], data["hashes"], data["vectors"]):
            _cache[str(pid)] = (str(digest), np.asarray(vec, dtype=np.float32))
    except Exception as e:
        print(f"[vector_recall] 向量缓存读取失败，按空缓存重建: {e}")


def _save_cache() -> None:
    """原子落盘（先写临时文件再替换，避免半截缓存把后续查询算错）"""
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        ids = list(_cache.keys())
        # 临时文件名须以 .npz 结尾：np.savez 会自动给非 .npz 后缀补后缀，导致后续替换找不到文件
        tmp = CACHE_FILE.with_name(CACHE_FILE.stem + ".tmp.npz")
        np.savez(tmp, ids=np.array(ids),
                  hashes=np.array([_cache[i][0] for i in ids]),
                  vectors=np.stack([_cache[i][1] for i in ids]))
        os.replace(tmp, CACHE_FILE)
    except Exception as e:
        print(f"[vector_recall] 向量缓存落盘失败（不影响本次结果）: {e}")


def _doc_text(row: dict) -> str:
    """参与编码的文档文本：标题 + 摘要切片（摘要按 DOC_CHARS 截断）"""
    return f"{row.get('title') or ''}\n{(row.get('abstract') or '')[:DOC_CHARS]}"


def _doc_hash(text: str) -> str:
    """文本指纹：内容变更即缓存失效（避免旧向量继续生效）"""
    return hashlib.md5(text.encode("utf-8")).hexdigest()
