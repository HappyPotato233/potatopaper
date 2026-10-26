# 全文抓取：按来源拿到论文全文（arXiv HTML 优先，PDF 兜底），并做纯文本化
import io
import re

import httpx
from bs4 import BeautifulSoup

from sources.base import rate_limit_wait

# 全文太短说明不是正文（登录页/错误页/仅摘要），按失败处理
MIN_TEXT_LEN = 800
MAX_TEXT_LEN = 1_200_000

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "text/html,application/pdf;q=0.9,*/*;q=0.8",
}


def _normalize_text(text: str) -> str:
    """压空白、统一换行，保留段落分隔"""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()[:MAX_TEXT_LEN]


def _html_to_text(html: str) -> str:
    """HTML 正文提取：去掉导航/脚本等噪声，只保留标题与段落级内容"""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup.find_all(["script", "style", "nav", "header", "footer", "aside", "form"]):
        tag.decompose()
    root = soup.find("article") or soup.find("main") or soup.body
    if root is None:
        return ""
    blocks: list[str] = []
    for el in root.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "blockquote", "figcaption"]):
        text = re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip()
        if text:
            blocks.append(text)
    return _normalize_text("\n\n".join(blocks))


def _pdf_to_text(content: bytes) -> str:
    """PDF 纯文本提取（仅作 HTML 不可用时的兜底）"""
    try:
        from pypdf import PdfReader
    except ImportError:
        return ""
    try:
        reader = PdfReader(io.BytesIO(content))
        pages = [(page.extract_text() or "") for page in reader.pages]
    except Exception as e:
        print(f"[fulltext] PDF 解析失败: {e}")
        return ""
    return _normalize_text("\n\n".join(pages))


def _get(url: str, timeout: float = 30) -> httpx.Response:
    """带 arXiv 限速的 GET（限速器按来源名复用全局间隔）"""
    rate_limit_wait("arxiv")
    return httpx.get(url, headers=HEADERS, timeout=timeout, follow_redirects=True)


def _fetch_arxiv(arxiv_id: str) -> str:
    """arXiv：官方 HTML → ar5iv HTML → PDF，任一成功即返回"""
    for url in (f"https://arxiv.org/html/{arxiv_id}", f"https://ar5iv.labs.arxiv.org/html/{arxiv_id}"):
        try:
            resp = _get(url)
            if resp.status_code == 200 and "html" in resp.headers.get("content-type", ""):
                text = _html_to_text(resp.text)
                if len(text) >= MIN_TEXT_LEN:
                    return text
        except Exception as e:
            print(f"[fulltext] HTML 获取失败 {url}: {e}")

    try:
        resp = _get(f"https://arxiv.org/pdf/{arxiv_id}", timeout=90)
        if resp.status_code == 200:
            text = _pdf_to_text(resp.content)
            if len(text) >= MIN_TEXT_LEN:
                return text
    except Exception as e:
        print(f"[fulltext] PDF 获取失败 arxiv:{arxiv_id}: {e}")
    return ""


def fetch_full_text(source: str, external_id: str, url: str | None = None) -> str:
    """按来源抓取全文；失败返回空字符串（不影响阅读页展示摘要）"""
    try:
        if source == "arXiv":
            arxiv_id = external_id.split("arxiv:", 1)[-1].strip()
            if arxiv_id:
                return _fetch_arxiv(arxiv_id)
        if url and (url.lower().endswith(".pdf") or "/pdf/" in url.lower()):
            resp = _get(url, timeout=90)
            if resp.status_code == 200:
                text = _pdf_to_text(resp.content)
                if len(text) >= MIN_TEXT_LEN:
                    return text
    except Exception as e:
        print(f"[fulltext] 全文抓取失败 {source}:{external_id}: {e}")
    return ""


MAX_PDF_BYTES = 60_000_000


def fetch_pdf_bytes(source: str, external_id: str, url: str | None = None) -> bytes:
    """下载论文 PDF 原始文件；失败返回空字节串"""
    candidates: list[str] = []
    if source == "arXiv":
        arxiv_id = external_id.split("arxiv:", 1)[-1].strip()
        if arxiv_id:
            candidates.append(f"https://arxiv.org/pdf/{arxiv_id}")
    if url and (url.lower().endswith(".pdf") or "/pdf/" in url.lower()):
        candidates.append(url)

    for pdf_url in candidates:
        try:
            resp = _get(pdf_url, timeout=120)
            content = resp.content
            if (resp.status_code == 200 and content[:4] == b"%PDF"
                    and len(content) <= MAX_PDF_BYTES):
                return content
        except Exception as e:
            print(f"[fulltext] PDF 下载失败 {pdf_url}: {e}")
    return b""

