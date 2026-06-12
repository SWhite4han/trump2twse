"""tide-tw.app 板塊資金流客戶端。

公開 JSON 端點 `https://tide-tw.app/data/latest.json` 每日更新，內含 78 個細分
產業板塊的三大法人 1d / 5d / 20d 累計淨買超、板塊位階百分位、報酬與波動度。
資料來源為 TWSE T86（三大法人）與 STOCK_DAY_ALL 的第三方聚合。

設計原則：fail-soft。所有錯誤回 None，呼叫端把 None 當「無此訊號」處理，
pipeline 任何一步都不可因 tide 不可用而中斷。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import requests

_LATEST_URL = "https://tide-tw.app/data/latest.json"
_DATED_URL = "https://tide-tw.app/data/{date}.json"
_CACHE_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "raw" / "tide"
_MEM_CACHE: dict[str, dict] = {}


def fetch_sector_snapshot(target_date: Optional[str] = None) -> Optional[dict]:
    """取得 tide-tw 板塊快照。

    Args:
        target_date: YYYY-MM-DD 字串；None 取最新。

    Returns:
        {date, market_chg_1d, sectors: [{name, stocks, net_1d, net_5d, net_20d,
        position, chg_1d, chg_5d, avg_abs_daily_20d}]}；任何失敗回 None。
    """
    cache_key = target_date or "latest"
    if cache_key in _MEM_CACHE:
        return _MEM_CACHE[cache_key]

    _CACHE_DIR.mkdir(parents=True, exist_ok=True)

    if target_date:
        local = _CACHE_DIR / f"{target_date}.json"
        if local.exists():
            try:
                data = json.loads(local.read_text(encoding="utf-8"))
                result = _normalize(data)
                _MEM_CACHE[cache_key] = result
                return result
            except Exception:
                pass

    url = _DATED_URL.format(date=target_date) if target_date else _LATEST_URL
    try:
        resp = requests.get(url, timeout=10, headers={"User-Agent": "MarketTrack/1.0"})
        if resp.status_code != 200:
            print(f"       [tide] HTTP {resp.status_code}（略過）")
            return None
        data = resp.json()
    except Exception as e:
        print(f"       [tide] fetch 失敗（略過）：{e}")
        return None

    snap_date = data.get("date") or target_date
    if snap_date:
        try:
            (_CACHE_DIR / f"{snap_date}.json").write_text(
                json.dumps(data, ensure_ascii=False), encoding="utf-8"
            )
        except Exception:
            pass

    result = _normalize(data)
    _MEM_CACHE[cache_key] = result
    return result


def _normalize(data: dict) -> dict:
    sectors = []
    for s in data.get("sectors", []) or []:
        sectors.append({
            "name": s.get("name", ""),
            "stocks": s.get("stocks", []) or [],
            "net_1d": s.get("net_1d_yi"),
            "net_5d": s.get("net_5d_yi"),
            "net_20d": s.get("net_20d_yi"),
            "position": s.get("position"),
            "chg_1d": s.get("chg_1d"),
            "chg_5d": s.get("chg_5d"),
            "avg_abs_daily_20d": s.get("avg_abs_daily_20d"),
        })
    return {
        "date": data.get("date"),
        "market_chg_1d": data.get("market_chg_1d"),
        "sectors": sectors,
    }


def top_inflow_sectors(snapshot: Optional[dict], n: int = 3, window: str = "5d") -> list[dict]:
    """資金流入前 N 板塊（net_{window} 由大到小）。"""
    if not snapshot:
        return []
    key = f"net_{window}"
    items = [s for s in snapshot.get("sectors", []) if isinstance(s.get(key), (int, float))]
    items.sort(key=lambda s: s[key], reverse=True)
    return [s for s in items[:n] if s[key] > 0]


def top_outflow_sectors(snapshot: Optional[dict], n: int = 3, window: str = "5d") -> list[dict]:
    """資金流出前 N 板塊（net_{window} 由小到大）。"""
    if not snapshot:
        return []
    key = f"net_{window}"
    items = [s for s in snapshot.get("sectors", []) if isinstance(s.get(key), (int, float))]
    items.sort(key=lambda s: s[key])
    return [s for s in items[:n] if s[key] < 0]
