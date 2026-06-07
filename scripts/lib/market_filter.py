"""台股大盤方向濾網

使用 ^TWII（加權指數）+ 外資買賣超 + 美股大盤 + 台指期夜盤，綜合判斷市場偏向，
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

    # --- 美股 + 法人 + 台指期夜盤 + 綜合偏向 ---
    us   = _fetch_us_market()
    inst = _fetch_institutional()
    txf  = _fetch_txf_night(last)

    # 連續評分：每 1% 漲跌 = 1 分，各項目有獨立上限避免單一極端值壟斷
    score = 0.0
    if state == "bull":    score += 1.0
    elif state == "bear":  score -= 1.0

    foreign_net = inst.get("foreign_net")
    if foreign_net is not None:
        score += max(-1.0, min(1.0, foreign_net / 100.0))

    sp500 = us.get("sp500_1d_pct")
    if sp500 is not None:
        score += max(-1.5, min(1.5, sp500))

    txf_pct = txf.get("txf_pct")
    if txf_pct is not None:
        score += max(-2.0, min(2.0, txf_pct))

    bias = "bullish" if score > 1.0 else "bearish" if score < -1.0 else "neutral"

    bias_parts = [f"台股 {state}（5日 {ret5:+.1f}%）"]
    if "不可用" not in inst["comment"]:
        bias_parts.append(inst["comment"])
    if "不可用" not in us["comment"]:
        bias_parts.append(us["comment"])
    if "不可用" not in txf["comment"]:
        bias_parts.append(txf["comment"])
    bias_label = {"bullish": "多", "bearish": "空", "neutral": "中性"}[bias]
    bias_summary = "，".join(bias_parts) + f" → 整體偏{bias_label}"

    return {
        "state":         state,
        "5d_return":     ret5,
        "ma5":           ma5,
        "ma20":          ma20,
        "last_close":    last,
        "reason":        reason,
        "us_market":     us,
        "institutional": inst,
        "txf_night":     txf,
        "bias":          bias,
        "bias_summary":  bias_summary,
    }


def _fetch_us_market() -> dict:
    """回傳美股大盤 1 日漲跌幅。pipeline 在台北 23:00 跑，美股已收盤。"""
    result: dict = {"sp500_1d_pct": None, "nasdaq_1d_pct": None, "comment": "美股資料不可用"}
    try:
        import yfinance as yf
        for label, ticker in [("sp500", "^GSPC"), ("nasdaq", "^IXIC")]:
            hist = yf.download(ticker, period="5d", auto_adjust=True, progress=False, multi_level_index=False)
            if hist is None or hist.empty:
                continue
            # 過濾 intraday 部分資料造成的 NaN bar（盤中跑時最新一筆可能未結算）
            close = hist["Close"].dropna()
            if len(close) < 2:
                continue
            pct = float((close.iloc[-1] - close.iloc[-2]) / close.iloc[-2] * 100)
            if math.isnan(pct):
                continue
            result[f"{label}_1d_pct"] = round(pct, 2)
        s = result["sp500_1d_pct"]
        n = result["nasdaq_1d_pct"]
        if s is not None and n is not None:
            trend = "收漲" if s > 0 else "收跌"
            result["comment"] = f"S&P500 {s:+.1f}%、Nasdaq {n:+.1f}%，美股{trend}"
        elif s is not None:
            trend = "收漲" if s > 0 else "收跌"
            result["comment"] = f"S&P500 {s:+.1f}%，美股{trend}"
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


def _fetch_txf_night(twii_close: float) -> dict:
    """從 TAIFEX MIS API 取台指期夜盤近月合約，計算相對今日收盤的隔夜漲跌幅。

    pipeline 在台北 23:00 跑時，夜盤（17:00–05:00）已開 6 小時。
    近月合約 = SymbolID 格式為 TXFx0-M（7碼），以成交量最大者為近月（流動性 proxy）。
    """
    result: dict = {"txf_price": None, "txf_pct": None, "comment": "台指期夜盤不可用"}
    try:
        resp = requests.post(
            "https://mis.taifex.com.tw/futures/api/getQuoteList",
            json={"MarketType": "1", "CcommodityID": "TX"},
            timeout=10,
            headers={"User-Agent": "MarketTrack/1.0"},
        )
        data = resp.json()
        if data.get("RtCode") != "0":
            return result
        quotes = data["RtData"]["QuoteList"]
        # 過濾近月單腿合約（TXFF6-M 共7碼，排除價差如 TXFF6/G6-M）
        candidates = [
            q for q in quotes
            if len(q.get("SymbolID", "")) == 7
            and q["SymbolID"].endswith("-M")
            and q.get("CLastPrice", "0") not in ("0", "0.00", "")
        ]
        if not candidates:
            return result
        # 近月 = 成交量最大者；冷時段或結算切倉時比「最近成交時間」更穩定
        def _vol(q):
            try:
                return int(str(q.get("CTotalVolume", "0")).replace(",", "") or 0)
            except (ValueError, TypeError):
                return 0
        candidates.sort(key=_vol, reverse=True)
        front = candidates[0]
        price = float(front["CLastPrice"])
        pct = round((price - twii_close) / twii_close * 100, 2)
        direction = "偏多" if pct > 0 else "偏空"
        result["txf_price"] = price
        result["txf_pct"] = pct
        result["comment"] = f"台指期夜盤 {price:.0f}（{pct:+.2f}%，隔夜{direction}）"
        print(f"       台指期夜盤：{front['SymbolID']} {price:.0f}（vs TWII {twii_close:.0f}，{pct:+.2f}%）")
    except Exception as e:
        print(f"       [市場濾網] 台指期夜盤失敗（略過）：{e}")
    return result


def _unknown() -> dict:
    return {
        "state":         "unknown",
        "5d_return":     None,
        "ma5":           None,
        "ma20":          None,
        "last_close":    None,
        "reason":        "資料不可用，略過濾網",
        "us_market":     {"sp500_1d_pct": None, "nasdaq_1d_pct": None, "comment": "美股資料不可用"},
        "institutional": {"foreign_net": None, "trust_net": None, "comment": "法人資料不可用"},
        "txf_night":     {"txf_price": None, "txf_pct": None, "comment": "台指期夜盤不可用"},
        "bias":          "neutral",
        "bias_summary":  "大盤資料不可用，偏中性",
    }
