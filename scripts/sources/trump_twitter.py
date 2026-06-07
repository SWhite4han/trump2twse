"""Trump Truth Social 貼文擷取器。

優先順序（依序 fallback）：
  1. TRUMP_SOURCE_URL（.env 設定的 RSSHub / Nitter RSS 鏡像）
  2. 多個公開 RSSHub 實例（hardcoded 備援清單）
  3. DuckDuckGo 新聞搜尋（最後備援，結果較少）

輸出存至 data/raw/trump/YYYY-MM-DD.json。
"""
from __future__ import annotations

import json
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup

from scripts.lib.config import config

# Truth Social 帳號 ID（Donald J. Trump = 107780257626128497）
_TRUTH_SOCIAL_ACCOUNT_ID = "107780257626128497"

# RSSHub 公共鏡像（Truth Social 路由，多數已失效，保留以防復活）
_RSSHUB_MIRRORS: list[str] = []

# Google News RSS 查詢（聚焦新政策動作，&tbs=qdr:d7 限 7 天內）
_GOOGLE_NEWS_QUERIES = [
    "Trump tariff trade executive order",
    "Trump policy announcement deal signed",
]

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; MarketTrack/1.0; +https://github.com/market-track)"
}
_TIMEOUT = 20


# --------------------------------------------------------------------------- #
# 公開介面
# --------------------------------------------------------------------------- #

def fetch(save: bool = True, date: str | None = None) -> list[dict]:
    posts = _try_rss() or _try_google_news() or _try_ddgs_fallback()
    if save and posts:
        _save(posts, date=date)
    return posts


# --------------------------------------------------------------------------- #
# RSS 解析
# --------------------------------------------------------------------------- #

def _try_rss() -> list[dict]:
    urls = _build_rss_urls()
    for url in urls:
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
            resp.raise_for_status()
            return _parse_rss(resp.text, url)
        except Exception:
            time.sleep(1)
    return []


def _build_rss_urls() -> list[str]:
    urls = []
    if config.trump_source_url:
        urls.append(config.trump_source_url)
    for mirror in _RSSHUB_MIRRORS:
        urls.append(mirror.format(uid=_TRUTH_SOCIAL_ACCOUNT_ID))
    return urls


def _parse_rss(xml_text: str, source_url: str) -> list[dict]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    posts = []

    # Atom feed
    for entry in root.findall("atom:entry", ns):
        text = _strip_html(entry.findtext("atom:content", "", ns) or entry.findtext("atom:summary", "", ns))
        pub = entry.findtext("atom:published", "", ns) or entry.findtext("atom:updated", "", ns)
        link_el = entry.find("atom:link", ns)
        link = link_el.attrib.get("href", "") if link_el is not None else ""
        post_id = entry.findtext("atom:id", "", ns)
        if text:
            posts.append(_make_post(post_id or link, text, pub, link))

    # RSS 2.0 feed
    if not posts:
        for item in root.iter("item"):
            text = _strip_html(
                (item.findtext("description") or "") + " " + (item.findtext("title") or "")
            ).strip()
            pub = item.findtext("pubDate", "")
            link = item.findtext("link", "")
            guid = item.findtext("guid", link)
            if text:
                posts.append(_make_post(guid, text, pub, link))

    return posts


def _make_post(post_id: str, text: str, pub: str, link: str) -> dict:
    return {
        "id": post_id,
        "text": text.strip(),
        "published_at": _normalise_date(pub),
        "source_url": link,
    }


def _strip_html(html: str) -> str:
    return BeautifulSoup(html, "lxml").get_text(separator=" ").strip()


def _normalise_date(raw: str) -> str:
    """Parse RFC 2822 / ISO date strings to UTC ISO format."""
    if not raw:
        return raw
    # RFC 2822（含 GMT）—— email.utils 處理最完整
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(raw.strip()).astimezone(timezone.utc).isoformat()
    except Exception:
        pass
    for fmt in (
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%SZ",
    ):
        try:
            dt = datetime.strptime(raw.strip(), fmt)
            return dt.astimezone(timezone.utc).isoformat()
        except ValueError:
            continue
    return raw.strip()


# --------------------------------------------------------------------------- #
# Google News RSS 備援
# --------------------------------------------------------------------------- #

def _parse_pub_datetime(date_str: str) -> datetime | None:
    """Parse normalized ISO published_at string to timezone-aware datetime."""
    if not date_str:
        return None
    try:
        return datetime.fromisoformat(date_str.replace("Z", "+00:00"))
    except Exception:
        return None


def _try_google_news() -> list[dict]:
    """從 Google News RSS 抓 Trump 相關新聞作為穩定備援。只保留 3 天內的文章。"""
    posts = []
    seen: set[str] = set()
    for query in _GOOGLE_NEWS_QUERIES:
        url = f"https://news.google.com/rss/search?q={requests.utils.quote(query)}&tbs=qdr:d7&hl=en-US&gl=US&ceid=US:en"
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
            resp.raise_for_status()
            for item in ET.fromstring(resp.text).iter("item"):
                link = item.findtext("link", "")
                if link in seen:
                    continue
                seen.add(link)
                # Google News description 只是 `<a>title</a> source` 的 HTML 包裝，
                # title 已含「標題 - 來源」全部資訊，直接用 title 即可
                text = item.findtext("title", "").strip()
                posts.append(_make_post(link, text, item.findtext("pubDate", ""), link))
        except Exception:
            continue
        time.sleep(0.5)

    # 按發布日期排序（最新在前），讓 _step_classify 的 [:8] 自然取最新的
    posts.sort(key=lambda p: p.get("published_at", ""), reverse=True)
    print(f"       Google News：{len(posts)} 則（日期排序，最新在前）")
    return posts


# --------------------------------------------------------------------------- #
# DuckDuckGo 備援
# --------------------------------------------------------------------------- #

def _try_ddgs_fallback() -> list[dict]:
    """用 duckduckgo-search 抓最近 Trump 相關新聞作為備援。今日無結果時嘗試 3 日內。"""
    try:
        from ddgs import DDGS  # type: ignore
    except ImportError:
        try:
            from duckduckgo_search import DDGS  # type: ignore
        except ImportError:
            return []

    query = "Trump tariff trade announcement executive order"
    for timelimit in ("d", "w"):  # 先試今日，再試近一週
        try:
            with DDGS() as ddgs:
                results = list(ddgs.news(query, max_results=20, timelimit=timelimit))
            if results:
                posts = []
                for r in results:
                    posts.append({
                        "id": r.get("url", ""),
                        "text": (r.get("title", "") + " " + r.get("body", "")).strip(),
                        "published_at": r.get("date", ""),
                        "source_url": r.get("url", ""),
                    })
                print(f"       DDGS（timelimit={timelimit}）：{len(posts)} 則")
                return posts
        except Exception:
            continue
    return []


# --------------------------------------------------------------------------- #
# 存檔
# --------------------------------------------------------------------------- #

def _save(posts: list[dict], date: str | None = None) -> Path:
    today = date or datetime.now().strftime("%Y-%m-%d")
    out_dir = config.data_dir / "raw" / "trump"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"{today}.json"

    existing: list[dict] = []
    if out_file.exists():
        with open(out_file, encoding="utf-8") as f:
            existing = json.load(f)

    seen_ids = {p["id"] for p in existing}
    new_posts = [p for p in posts if p["id"] not in seen_ids]
    merged = existing + new_posts

    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)

    print(f"[trump] 儲存 {len(new_posts)} 則新貼文 → {out_file}")
    return out_file


if __name__ == "__main__":
    items = fetch()
    print(f"共擷取 {len(items)} 則")
    for p in items[:3]:
        print(f"  [{p['published_at']}] {p['text'][:80]}...")
