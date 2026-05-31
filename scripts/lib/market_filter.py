"""台股大盤方向濾網

使用 ^TWII（加權指數）+ 外資買賣超 + 美股大盤，綜合判斷市場偏向，
供 daily_pipeline 調整事件 confidence 及過濾逆勢訊號。
"""
from __future__ import annotations

import math
import requests
from datetime import date, datetime
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

    if ma5 and ma20:
        trend = "MA5>MA20 多排列" if ma5 > ma20 else "MA5<MA20 空排列"
        reason += f"，{trend}"

    # --- 新增：美股 + 法人 + 綜合偏向 ---
    us   = _fetch_us_market()
    inst = _fetch_institutional()

    score = 0.0
    if state == "bull":    score += 1.0
    elif state == "bear":  score -= 1.0

    foreign_net = inst.get("foreign_net")
    if foreign_net is not None:
        if foreign_net > 50:    score += 0.8
        elif foreign_net < -50: score -= 0.8

    sp500 = us.get("sp500_1d_pct")
    if sp500 is not None:
        if sp500 > 0.5:    score += 0.5
        elif sp500 < -0.5: score -= 0.5

    bias = "bullish" if score > 0.6 else "bearish" if score < -0.6 else "neutral"

    bias_parts = [f"台股 {state}（5日 {ret5:+.1f}%）"]
    if "不可用" not in inst["comment"]:
        bias_parts.append(inst["comment"])
    if "不可用" not in us["comment"]:
        bias_parts.append(us["comment"])
    bias_label = {"bullish": "多", "bearish": "空", "neutral": "中性"}[bias]
    bias_summary = "，".join(bias_parts) + f" → 整體偏{bias_label}"

    return {
        "state":        state,
        "5d_return":    ret5,
        "ma5":          ma5,
        "ma20":         ma20,
        "last_close":   last,
        "reason":       reason,
        "us_market":    us,
        "institutional": inst,
        "bias":         bias,
        "bias_summary": bias_summary,
    }


def _fetch_us_market() -> dict:
    """回傳美股大盤 1 日漲跌幅。pipeline 在台北 23:00 跑，美股已收盤。"""
    result: dict = {"sp500_1d_pct": None, "nasdaq_1d_pct": None, "comment": "美股資料不可用"}
    try:
        import yfinance as yf
        for label, ticker in [("sp500", "^GSPC"), ("nasdaq", "^IXIC")]:
            hist = yf.download(ticker, period="5d", auto_adjust=True, progress=False, multi_level_index=False)
            if hist is not None and len(hist) >= 2:
                pct = (hist["Close"].iloc[-1] - hist["Close"].iloc[-2]) / hist["Close"].iloc[-2] * 100
                result[f"{label}_1d_pct"] = round(float(pct), 2)
        s = result["sp500_1d_pct"]
        n = result["nasdaq_1d_pct"]
        if s is not None:
            trend = "收漲" if s > 0 else "收跌"
            result["comment"] = f"S&P500 {s:+.1f}%、Nasdaq {n:+.1f}%，美股{trend}"
    except Exception as e:
        print(f"       [市場濾網] 美股資料失敗（略過）：{e}")
    return result


def _fetch_institutional() -> dict:
    """回傳外資 + 投信當日淨買超（億元），從 TWSE BFI82U API 取得。"""
    result: dict = {"foreign_net": None, "trust_net": None, "comment": "法人資料不可用"}
    try:
        today = datetime.now().strftime("%Y%m%d")
        url = f"https://www.twse.com.tw/fund/BFI82U?response=json&dayDate={today}&type=day"
        resp = requests.get(url, timeout=10, headers={"User-Agent": "MarketTrack/1.0"})
        data = resp.json()
        if data.get("stat") != "OK":
            return result
        rows = {row[0]: row for row in data.get("data", [])}
        # 外陸資合計（不含外資自營商）欄位
        foreign = rows.get("外陸資合計(不含外資自營商)")
        trust   = rows.get("投信")
        if foreign:
            # 單位：千元，除以 100,000 → 億元
            result["foreign_net"] = round(int(foreign[3].replace(",", "")) / 100_000, 1)
        if trust:
            result["trust_net"] = round(int(trust[3].replace(",", "")) / 100_000, 1)
        f = result["foreign_net"]
        if f is not None:
            direction = "淨買入" if f > 0 else "淨賣出"
            result["comment"] = f"外資今日{direction} {abs(f):.0f}億"
    except Exception as e:
        print(f"       [市場濾網] 法人資料失敗（略過）：{e}")
    return result


def _unknown() -> dict:
    return {
        "state":        "unknown",
        "5d_return":    None,
        "ma5":          None,
        "ma20":         None,
        "last_close":   None,
        "reason":       "資料不可用，略過濾網",
        "us_market":    {"sp500_1d_pct": None, "nasdaq_1d_pct": None, "comment": "美股資料不可用"},
        "institutional": {"foreign_net": None, "trust_net": None, "comment": "法人資料不可用"},
        "bias":         "neutral",
        "bias_summary": "大盤資料不可用，偏中性",
    }
