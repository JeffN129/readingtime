"""
苦瓜书盘 (kgbook.com) 书源 — 中文电子书下载，无需代理/登录。

主要提供 6寸PDF 格式，部分书籍有 epub/mobi/azw3。
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import List
from urllib.parse import urljoin

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from readingtime.sources.base import BookResult, BookSource

logger = logging.getLogger(__name__)

BASE_URL = "https://kgbook.com"
SEARCH_URL = f"{BASE_URL}/e/search/index.php"

# Format preference: epub > mobi > azw3 > pdf (pathid: 2=epub, 1=mobi, 0=azw3)
_PATHID_ORDER = [2, 1, 0]
_PATHID_NAMES = {0: "azw3", 1: "mobi", 2: "epub"}


class KgbookSource(BookSource):
    """苦瓜书盘 — 中文电子书，直链下载，无需代理/登录。"""

    name = "kgbook"

    def __init__(self) -> None:
        self._session: requests.Session | None = None

    @staticmethod
    def _get_text(url: str, **kwargs) -> str:
        """Fetch a URL and return correctly-decoded UTF-8 text."""
        s = requests.Session()
        s.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "Accept-Language": "zh-CN,zh;q=0.9",
        })
        resp = s.get(url, **kwargs)
        # kgbook pages are UTF-8 but mis-declared as ISO-8859-1
        return resp.content.decode("utf-8", errors="replace")

    # -- search --------------------------------------------------------------

    def search(
        self,
        query: str,
        language: str = "",
        limit: int = 10,
    ) -> List[BookResult]:
        """Search kgbook for books matching *query*."""
        try:
            s = requests.Session()
            s.headers.update({
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            })
            resp = s.post(SEARCH_URL, data={
                "keyboard": query,
                "show": "title,booksay,bookwriter",
                "tbname": "download",
                "tempid": "1",
            }, timeout=20, allow_redirects=True)
            # Force UTF-8
            if resp.apparent_encoding and resp.apparent_encoding.lower() in ("utf-8", "ascii"):
                resp.encoding = "utf-8"

            if resp.status_code != 200:
                return []
            return self._parse_search_results(resp.text, limit)

        except requests.RequestException as exc:
            logger.error("kgbook search error for '%s': %s", query, exc)
            return []

    # -- download ------------------------------------------------------------

    def download(self, result: BookResult, save_path: str) -> bool:
        """Download book from kgbook. Tries epub first, falls back to mobi/azw3/pdf."""
        out = Path(save_path)

        # source_id: "kgbook:{classid}:{bookid}" or "kgbook:::{bookid}"
        parts = result.source_id.replace("kgbook:", "").split(":")
        classid, book_id = parts[0], parts[-1]  # -1 handles both formats

        # If classid is empty (only book_id known), fetch detail page
        if not classid or not classid.isdigit():
            self._enrich_from_detail(result)
            parts = result.source_id.replace("kgbook:", "").split(":")
            classid, book_id = parts[0], parts[-1]

        if not classid or not classid.isdigit():
            logger.error("Cannot download: missing classid in %s", result.source_id)
            return False
        out.parent.mkdir(parents=True, exist_ok=True)

        for pathid in _PATHID_ORDER:
            dl_url = f"{BASE_URL}/e/DownSys/GetDown?classid={classid}&id={book_id}&pathid={pathid}"
            fmt_name = _PATHID_NAMES[pathid]

            for attempt in range(1, 4):
                try:
                    resp = requests.get(dl_url, timeout=60, stream=True,
                                        headers={"User-Agent": "Mozilla/5.0"})
                    ct = resp.headers.get("content-type", "")

                    # HTML response = format not available, try next
                    if "html" in ct:
                        break

                    if resp.status_code == 200:
                        with open(out, "wb") as fh:
                            for chunk in resp.iter_content(chunk_size=65536):
                                if chunk:
                                    fh.write(chunk)

                        fsize = out.stat().st_size
                        if fsize < 2048:
                            if attempt < 3:
                                time.sleep(2 ** (attempt - 1))
                            continue

                        # Detect actual format and add correct extension
                        ext = ".epub"
                        if "pdf" in ct or (b"%PDF" in open(out, "rb").read(4)):
                            ext = ".pdf"
                            fmt_name = "pdf"
                        elif "mobi" in ct or fmt_name == "mobi":
                            ext = ".mobi"
                        elif fmt_name == "azw3":
                            ext = ".azw3"

                        new_path = out.with_suffix(ext)
                        out.rename(new_path)
                        out = new_path

                        logger.info("Downloaded %s → %s (%.1f KB, %s)",
                                    result.title, out.name, fsize / 1024, fmt_name)
                        return True

                except requests.RequestException as exc:
                    logger.warning("Download %d/3 failed: %s", attempt, exc)
                    if attempt < 3:
                        time.sleep(2 ** (attempt - 1))

        logger.error("All download attempts failed for %s", result.title)
        return False

    # -- detail enrichment ---------------------------------------------------

    def _enrich_from_detail(self, result: BookResult) -> None:
        """Fetch book detail page and fill in author, classid, description."""
        detail_url = result.epub_download_url
        if not detail_url:
            return

        try:
            html = self._get_text(detail_url, timeout=15)

            # Extract download link for classid + bookid
            dl_m = re.search(
                r'GetDown\?classid=(\d+)&(?:amp;|)id=(\d+)',
                html,
            )
            if dl_m:
                result.source_id = f"kgbook:{dl_m.group(1)}:{dl_m.group(2)}"

            # Strip tags for text extraction
            text = re.sub(r"<[^>]+>", " ", html)
            text = re.sub(r"&nbsp;", " ", text)
            text = re.sub(r"\s+", " ", text)

            # Author: 作者：XXX
            author_m = re.search(r"作者[：:]\s*(.+?)(?:\s+(?:格式|语言|大小|星级|发布|整理|标签))", text)
            if author_m:
                result.author = author_m.group(1).strip()[:50]

            # Description: 简介：XXX
            desc_m = re.search(r"简介[：:]\s*(.+?)(?:\s+(?:\d寸|格式|语言|购买|Artalk))", text)
            if desc_m:
                result.description = desc_m.group(1).strip()[:500]

            # Category tag from URL
            cat_m = re.search(r"/([a-z]+)/\d+\.html", detail_url)
            if cat_m:
                result.tags = [cat_m.group(1)]

            logger.debug("Enriched: %s by %s [%s]", result.title, result.author, result.source_id)

        except requests.RequestException as exc:
            logger.debug("Enrich failed for %s: %s", result.title, exc)

    # -- search result parsing -----------------------------------------------

    @staticmethod
    def _parse_search_results(html: str, limit: int) -> List[BookResult]:
        """Parse kgbook search results page."""
        results: List[BookResult] = []
        pattern = re.compile(
            r'<a[^>]*href=["\']([^"\']*/(\d+)\.html)["\'][^>]*>(.*?)</a>',
            re.DOTALL,
        )
        seen_ids = set()

        for href, book_id, title_raw in pattern.findall(html):
            if book_id in seen_ids:
                continue
            if len(results) >= limit:
                break

            title = re.sub(r"<[^>]+>", "", title_raw).strip()
            if not title or title in ("1", "2", "3", "4", "5", "下一页", "上一页"):
                continue
            if href.startswith("http://kgbook.comhttps://"):
                continue

            seen_ids.add(book_id)
            full_url = urljoin(BASE_URL, href)

            results.append(BookResult(
                source_id=f"kgbook:::{book_id}",
                title=title,
                author="",
                language="zh",
                tags=[],
                formats=["pdf", "epub", "mobi"],
                epub_download_url=full_url,
                cover_url=None,
                page_count=None,
                description=None,
                download_count=0,
            ))

        return results
