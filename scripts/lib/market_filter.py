"""台股大盤方向濾網

使用 ^TWII（加權指數）判斷近期市場狀態，供 daily_pipeline 過濾逆勢訊號。
未來可替換為台指期（TXF）資料，介面不變。
"""
from __future__ import annotations

import math
from datetime import date
from typing import Optional

# 快取：同一天只算一次
_cache: Optional[dict] = None
_cache_date: Optional[date] = None


def get_market_state() -> dict:
    """回傳大盤狀態 dict。

    Keys:
        state       : "bull" | "neutral" | "bear"
        5d_return   : 近 5 交易日報酬（%）
        ma5         : MA5 收盤
        ma20        : MA20 收盤
        last_close  : 最新收盤
        reason      : 人讀摘要字串
    """
    global _cache, _cache_date
    today = date.today()
    if _cache_date == today and _cache is not None:
        return _cache

    result = _fetch_state()
    _cache = result
    _cache_date = today
    return result


def _fetch_state() -> dict:
    try:
        import yfinance as yf
        h = yf.Ticker("^TWII").history(period="2mo", auto_adjust=True)
    except Exception as e:
        print(f"       [市場濾網] ^TWII 資料失敗（略過濾網）：{e}")
        return _unknown()

    if h is None or h.empty or len(h) < 6:
        print("       [市場濾網] ^TWII 資料不足，略過濾網")
        return _unknown()

    close = h["Close"].dropna()
    if len(close) < 6:
        return _unknown()

    def r(x):
        v = float(x)
        return None if math.isnan(v) else round(v, 1)

    last   = r(close.iloc[-1])
    prev5  = r(close.iloc[-6])   # 5 交易日前
    ma5    = r(close.rolling(5).mean().iloc[-1])
    ma20   = r(close.rolling(20).mean().iloc[-1]) if len(close) >= 20 else None

    if last is None or prev5 is None:
        return _unknown()

    ret5 = round((last - prev5) / prev5 * 100, 2)

    if ret5 >= 2.0:
        state  = "bull"
        reason = f"5日報酬 {ret5:+.1f}%，多頭趨勢"
    elif ret5 <= -2.0:
        state  = "bear"
        reason = f"5日報酬 {ret5:+.1f}%，空頭趨勢"
    else:
        state  = "neutral"
        reason = f"5日報酬 {ret5:+.1f}%，盤整"

    # MA5 vs MA20 作為補強依據（僅用於 reason 說明）
    if ma5 and ma20:
        trend = "MA5>MA20 多排列" if ma5 > ma20 else "MA5<MA20 空排列"
        reason += f"，{trend}"

    return {
        "state":      state,
        "5d_return":  ret5,
        "ma5":        ma5,
        "ma20":       ma20,
        "last_close": last,
        "reason":     reason,
    }


def _unknown() -> dict:
    return {
        "state":      "unknown",
        "5d_return":  None,
        "ma5":        None,
        "ma20":       None,
        "last_close": None,
        "reason":     "資料不可用，略過濾網",
    }
