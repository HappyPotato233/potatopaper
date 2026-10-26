# PDF 临时缓存：按用户隔离存放，登出即清理，避免长期占用磁盘
import shutil
import time
from pathlib import Path

PDF_CACHE_DIR = Path(__file__).resolve().parents[1] / "data" / "pdfs"


def user_pdf_dir(user_id: int) -> Path:
    """每个用户独立的 PDF 缓存目录"""
    return PDF_CACHE_DIR / str(user_id)


def clear_user_pdfs(user_id: int) -> None:
    """用户下线时删除其全部 PDF 缓存"""
    shutil.rmtree(user_pdf_dir(user_id), ignore_errors=True)


def clear_stale_pdfs(max_age_seconds: int = 6 * 3600) -> None:
    """兜底清理长时间未访问的 PDF（用户直接关浏览器、未点退出登录的情况）"""
    if not PDF_CACHE_DIR.exists():
        return
    deadline = time.time() - max_age_seconds
    for path in PDF_CACHE_DIR.glob("*/*.pdf"):
        try:
            if path.stat().st_mtime < deadline:
                path.unlink(missing_ok=True)
        except OSError:
            continue
