"""
歷史績效重算腳本

對 data/performance/202605.csv 中每筆原始建議，
用 yfinance 逐日重新模擬進場 → 觸達目標/停損 → 結案，
修正買賣方向邏輯與損益計算。

修正點：
  1. 觀察賣出：目標觸發條件改為 lo <= target，停損改為 hi >= stop_loss
  2. 觀察賣出：損益方向改為 (entry - exit) / entry
  3. 停損優先於目標（同一天雙向觸達時，保守假設先碰停損）
  4. 結案用限價成交的 target/stop_loss 價，不用收盤價
  5. 同一 (report_date, code) 只保留第一筆原始參數，不延用 superseded 鏈
"""
from __future__ import annotations

import csv
import math
from datetime import datetime
from pathlib import Path

import yfinance as yf

MAX_WATCH_DAYS = 10
CAPITAL = 100_000

DATA_DIR  = Path("data/performance")
INPUT_CSV = DATA_DIR / "202605.csv"
OUT_CSV   = DATA_DIR / "202605_reprocessed.csv"

FIELDS = [
    "report_date", "code", "name", "rule_id", "action",
    "entry_low", "entry_high", "target", "stop_loss",
    "entry_date", "actual_entry_price", "exit_price",
    "close_reason", "close_date",
    "pnl_pct", "pnl_twd", "days_watched", "confidence",
]


# --------------------------------------------------------------------------- #

def _suffix(code: str) -> str:
    """上市 .TW / 上櫃 .TWO（從 TWSE cache 判斷，查不到預設 .TW）。"""
    try:
        from scripts.lib.twse_client import _load_today_all
        info = _load_today_all().get(code)
        return ".TWO" if (info and info.get("market") == "TWO") else ".TW"
    except Exception:
        return ".TW"


def _fetch(code: str, start: str) -> dict[str, dict]:
    """回傳 {YYYYMMDD: {open,high,low,close}}，start 之後的交易日。
    自動嘗試 .TW 與 .TWO 兩種 suffix。
    """
    preferred = _suffix(code)
    candidates = [preferred, ".TWO" if preferred == ".TW" else ".TW"]
    for suf in candidates:
        sym = code + suf
        try:
            h = yf.Ticker(sym).history(start=start, auto_adjust=True)
        except Exception:
            continue
        if h.empty:
            continue
        out: dict[str, dict] = {}
        for dt, row in h.iterrows():
            o, hi, lo, cl = row["Open"], row["High"], row["Low"], row["Close"]
            if any(v is None or (isinstance(v, float) and math.isnan(v)) for v in [o, hi, lo, cl]):
                continue
            out[dt.strftime("%Y%m%d")] = {"open": o, "high": hi, "low": lo, "close": cl}
        if out:
            return out
    return {}


def _simulate(rec: dict, hist: dict) -> dict:
    is_sell    = rec["action"] == "觀察賣出"
    entry_low  = float(rec["entry_low"])
    entry_high = float(rec["entry_high"])
    target     = float(rec["target"])    if rec.get("target")    else None
    stop_loss  = float(rec["stop_loss"]) if rec.get("stop_loss") else None

    days = sorted(d for d in hist if d > rec["report_date"])

    state      = "watching"
    entry_p: float | None = None
    entry_dt   = None
    watched    = 0

    for date_str in days:
        day = hist[date_str]
        op, hi, lo, cl = day["open"], day["high"], day["low"], day["close"]

        if state == "watching":
            watched += 1
            # 進場：日內範圍與進場區間重疊
            if lo <= entry_high and hi >= entry_low:
                state    = "holding"
                entry_dt = date_str
                if is_sell:
                    # 賣出：取最高可成交價 min(max(open, entry_low), entry_high)
                    entry_p = round(min(max(op, entry_low), entry_high), 2)
                else:
                    # 買入：取最低可成交價 max(min(open, entry_high), entry_low)
                    entry_p = round(max(min(op, entry_high), entry_low), 2)
                # 進場當天也可能立刻觸達目標/停損（fall through to holding block）
            else:
                if watched >= MAX_WATCH_DAYS:
                    return _build(rec, "expired", date_str, None, None, None, watched)
                continue

        if state == "holding":
            # 停損優先（同日雙向觸達時，保守假設先碰停損）
            if is_sell:
                # 賣出停損：股價漲到 stop_loss
                if stop_loss and hi >= stop_loss:
                    return _build(rec, "triggered_stop", date_str, entry_p, stop_loss, entry_dt, watched)
                # 賣出達標：股價跌到 target
                if target and lo <= target:
                    return _build(rec, "triggered_target", date_str, entry_p, target, entry_dt, watched)
            else:
                # 買入停損：股價跌到 stop_loss
                if stop_loss and lo <= stop_loss:
                    return _build(rec, "triggered_stop", date_str, entry_p, stop_loss, entry_dt, watched)
                # 買入達標：股價漲到 target
                if target and hi >= target:
                    return _build(rec, "triggered_target", date_str, entry_p, target, entry_dt, watched)

    # 資料截止仍未結案
    last_close = hist[days[-1]]["close"] if days else None
    if state == "holding":
        return _build(rec, "still_open", None, entry_p, last_close, entry_dt, watched)
    return _build(rec, "not_triggered", None, None, None, None, watched)


def _build(rec, reason, close_date, entry_p, exit_p, entry_dt, watched) -> dict:
    pnl_pct = pnl_twd = None
    if entry_p and exit_p:
        is_sell = rec["action"] == "觀察賣出"
        raw = ((entry_p - exit_p) / entry_p) if is_sell else ((exit_p - entry_p) / entry_p)
        pnl_pct = round(raw * 100, 2)
        pnl_twd = round(raw * CAPITAL, 0)
    return {
        "report_date":        rec["report_date"],
        "code":               rec["code"],
        "name":               rec["name"],
        "rule_id":            rec.get("rule_id", ""),
        "action":             rec["action"],
        "entry_low":          rec["entry_low"],
        "entry_high":         rec["entry_high"],
        "target":             rec.get("target", ""),
        "stop_loss":          rec.get("stop_loss", ""),
        "entry_date":         entry_dt,
        "actual_entry_price": entry_p,
        "exit_price":         exit_p,
        "close_reason":       reason,
        "close_date":         close_date,
        "pnl_pct":            pnl_pct,
        "pnl_twd":            pnl_twd,
        "days_watched":       watched,
        "confidence":         rec.get("confidence", ""),
    }


# --------------------------------------------------------------------------- #

def run() -> None:
    with open(INPUT_CSV, newline="", encoding="utf-8") as f:
        raw = list(csv.DictReader(f))

    # 每個 (report_date, code) 只保留第一筆原始建議參數（去除 superseded 鏈）
    seen: set[tuple] = set()
    recs: list[dict] = []
    for r in raw:
        key = (r["report_date"], r["code"])
        if key not in seen:
            seen.add(key)
            recs.append(r)

    print(f"去重後 {len(recs)} 筆建議（原始 {len(raw)} 筆）")

    # 按 code 批次 fetch yfinance（避免重複請求）
    codes = sorted({r["code"] for r in recs})
    min_start = min(r["report_date"] for r in recs)
    # 從最早報告日的前一天開始，確保取到 report_date 之後的第一個交易日
    start_dt = (datetime.strptime(min_start, "%Y%m%d")).strftime("%Y-%m-%d")

    print(f"抓取 {len(codes)} 支股票自 {start_dt} 的歷史資料…")
    hist_cache: dict[str, dict] = {}
    for code in codes:
        hist_cache[code] = _fetch(code, start_dt)
        n = len(hist_cache[code])
        print(f"  {code}: {n} 個交易日")

    # 逐筆模擬
    results: list[dict] = []
    for rec in recs:
        code = rec["code"]
        result = _simulate(rec, hist_cache.get(code, {}))
        results.append(result)

    # 輸出
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(results)
    print(f"\n→ 輸出 {len(results)} 筆：{OUT_CSV}")

    # 統計摘要
    closed   = [r for r in results if r["close_reason"] in ("triggered_target", "triggered_stop")]
    targets  = [r for r in closed if r["close_reason"] == "triggered_target"]
    stops    = [r for r in closed if r["close_reason"] == "triggered_stop"]
    still    = [r for r in results if r["close_reason"] == "still_open"]
    expired  = [r for r in results if r["close_reason"] in ("expired", "not_triggered")]

    total_pnl = sum(r["pnl_twd"] for r in closed if r["pnl_twd"] is not None)
    buy_pnl   = sum(r["pnl_twd"] for r in closed if r["pnl_twd"] is not None and r["action"] != "觀察賣出")
    sell_pnl  = sum(r["pnl_twd"] for r in closed if r["pnl_twd"] is not None and r["action"] == "觀察賣出")

    print(f"\n{'='*55}")
    print(f"  建議總數   {len(results)} 筆")
    print(f"  已結案     {len(closed)} 筆  ✅達標 {len(targets)}  ❌停損 {len(stops)}")
    print(f"  仍持倉中   {len(still)} 筆")
    print(f"  逾期未進場 {len(expired)} 筆")
    print(f"  達標率     {len(targets)/len(closed)*100:.0f}%  停損率 {len(stops)/len(closed)*100:.0f}%"
          if closed else "  （無結案）")
    print(f"  模擬損益   {total_pnl:+,.0f} 元（每筆 10 萬，{len(closed)} 筆結案）")
    print(f"    買進部分 {buy_pnl:+,.0f} 元")
    print(f"    賣出部分 {sell_pnl:+,.0f} 元")
    print(f"{'='*55}")


if __name__ == "__main__":
    run()
