"""股癌 Podcast 逐字稿筆記擷取器。

來源：socialworkerdaily.com，URL 規律：/notes-of-gooaye-ep-{N}/
策略：
  1. 爬首頁取得最新一集的集數。
  2. 下載該集筆記頁面並解析文字。
  3. 存至 data/raw/gua_cancer/YYYY-MM-DD.json。
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup

from scripts.lib.config import config

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; MarketTrack/1.0)"
}
_TIMEOUT = 20
_BASE_URL = ""  # 從 config.gua_cancer_base_url 取得
_NOTE_PATH = "/notes-of-gooaye-ep-{ep}/"


# --------------------------------------------------------------------------- #
# 公開介面
# --------------------------------------------------------------------------- #

def fetch(save: bool = True) -> list[dict]:
    """擷取最新的股癌筆記。

    Returns:
        筆記清單，每筆 dict：{episode, title, url, content, fetched_at}
    """
    base = config.gua_cancer_base_url.rstrip("/")
    latest_ep = _find_latest_episode(base)
    if latest_ep is None:
        print("[gua_cancer] 無法取得最新集數，中止。")
        return []

    notes = []
    # 抓最新 2 集（避免昨天的還沒有，今天的還沒更新的情況）
    for ep in range(latest_ep, max(latest_ep - 2, 0), -1):
        note = _fetch_episode(base, ep)
        if note:
            notes.append(note)
        time.sleep(1)

    if save and notes:
        _save(notes)
    return notes


# --------------------------------------------------------------------------- #
# 集數發現
# --------------------------------------------------------------------------- #

def _find_latest_episode(base: str) -> Optional[int]:
    """從首頁或搜尋最大集數。"""
    # 嘗試從首頁文章連結中找最大集數
    try:
        resp = requests.get(base, headers=_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")
        eps = _extract_episode_numbers(soup)
        if eps:
            return max(eps)
    except Exception as e:
        print(f"[gua_cancer] 首頁讀取失敗：{e}")

    # 備援：用二分搜尋估計最新集數（從 600 往上試）
    return _binary_search_latest(base, lo=550, hi=700)


def _extract_episode_numbers(soup: BeautifulSoup) -> list[int]:
    eps = []
    for a in soup.find_all("a", href=True):
        m = re.search(r"notes-of-gooaye-ep-(\d+)", a["href"])
        if m:
            eps.append(int(m.group(1)))
    return eps


def _binary_search_latest(base: str, lo: int, hi: int) -> Optional[int]:
    """二分搜尋找最大存在的集數。"""
    result = None
    while lo <= hi:
        mid = (lo + hi) // 2
        url = base + _NOTE_PATH.format(ep=mid)
        try:
            r = requests.head(url, headers=_HEADERS, timeout=10, allow_redirects=True)
            if r.status_code == 200:
                result = mid
                lo = mid + 1
            else:
                hi = mid - 1
        except Exception:
            hi = mid - 1
        time.sleep(0.5)
    return result


# --------------------------------------------------------------------------- #
# 頁面解析
# --------------------------------------------------------------------------- #

def _fetch_episode(base: str, ep: int) -> Optional[dict]:
    url = base + _NOTE_PATH.format(ep=ep)
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
    except Exception as e:
        print(f"[gua_cancer] EP{ep} 擷取失敗：{e}")
        return None

    soup = BeautifulSoup(resp.text, "lxml")
    title = _get_title(soup)
    content = _get_content(soup)
    if not content:
        return None

    return {
        "episode": ep,
        "title": title,
        "url": url,
        "content": content,
        "fetched_at": datetime.now().isoformat(),
    }


def _get_title(soup: BeautifulSoup) -> str:
    og = soup.find("meta", property="og:title")
    if og and og.get("content"):
        return og["content"].strip()
    h1 = soup.find("h1")
    return h1.get_text(strip=True) if h1 else ""


def _get_content(soup: BeautifulSoup) -> str:
    # 嘗試常見的文章容器 selector
    for sel in ["article", ".entry-content", ".post-content", "main"]:
        el = soup.select_one(sel)
        if el:
            # 移除導覽、廣告等雜訊
            for tag in el.find_all(["nav", "aside", "script", "style", "figure"]):
                tag.decompose()
            text = el.get_text(separator="\n", strip=True)
            if len(text) > 200:
                return text
    return ""


# --------------------------------------------------------------------------- #
# 存檔
# --------------------------------------------------------------------------- #

def _save(notes: list[dict]) -> Path:
    today = datetime.now().strftime("%Y-%m-%d")
    out_dir = config.data_dir / "raw" / "gua_cancer"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"{today}.json"

    existing: list[dict] = []
    if out_file.exists():
        with open(out_file, encoding="utf-8") as f:
            existing = json.load(f)

    seen_eps = {n["episode"] for n in existing}
    new_notes = [n for n in notes if n["episode"] not in seen_eps]
    merged = existing + new_notes

    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)

    print(f"[gua_cancer] 儲存 {len(new_notes)} 集筆記 → {out_file}")
    return out_file


if __name__ == "__main__":
    items = fetch()
    print(f"共擷取 {len(items)} 集")
    for n in items:
        print(f"  EP{n['episode']}: {n['title']}")
        print(f"    {n['content'][:120]}...")
