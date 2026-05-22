"""TWSE / TPEx OpenAPI 股價查詢客戶端。

同時支援上市（TWSE）與上櫃（TPEx）股票，免費、不需 API Key。
內建限速（3 req / 5s）與本日快取，避免重複打 API。
"""
from __future__ import annotations

import time
from datetime import date
from typing import Optional


import requests

# 上市（TWSE）今日收盤資料（每日約 14:35 更新）
_TWSE_TODAY_URL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
# 上櫃（TPEx）今日收盤資料
_TPEX_TODAY_URL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_quotes"

_RATE_LIMIT_DELAY = 5 / 3  # 3 req / 5s → 每次間隔 1.67s
_last_request_time: float = 0.0
_today_cache: dict[str, dict] = {}
_cache_date: Optional[date] = None


def _throttle() -> None:
    global _last_request_time
    elapsed = time.monotonic() - _last_request_time
    if elapsed < _RATE_LIMIT_DELAY:
        time.sleep(_RATE_LIMIT_DELAY - elapsed)
    _last_request_time = time.monotonic()


def _load_today_all() -> dict[str, dict]:
    """下載今日上市＋上櫃收盤資料並快取（同一天只打一次 API）。"""
    global _today_cache, _cache_date
    today = date.today()
    if _cache_date == today and _today_cache:
        return _today_cache

    result: dict[str, dict] = {}

    # 上市（TWSE）
    _throttle()
    try:
        resp = requests.get(_TWSE_TODAY_URL, timeout=15)
        resp.raise_for_status()
        for row in resp.json():
            code = row.get("Code", "").strip()
            if code:
                result[code] = {
                    "code": code,
                    "name": row.get("Name", "").strip(),
                    "close": _parse_price(row.get("ClosingPrice", "")),
                    "open": _parse_price(row.get("OpeningPrice", "")),
                    "high": _parse_price(row.get("HighestPrice", "")),
                    "low": _parse_price(row.get("LowestPrice", "")),
                    "volume": _parse_int(row.get("TradeVolume", "")),
                    "change": _parse_price(row.get("Change", "")),
                    "date": row.get("Date", ""),
                    "market": "TWSE",
                }
    except Exception as e:
        print(f"       TWSE API 失敗（繼續）：{e}")

    # 上櫃（TPEx）
    _throttle()
    try:
        resp = requests.get(_TPEX_TODAY_URL, timeout=15)
        resp.raise_for_status()
        for row in resp.json():
            code = row.get("SecuritiesCompanyCode", "").strip()
            if code and code not in result:
                result[code] = {
                    "code": code,
                    "name": row.get("CompanyName", "").strip(),
                    "close": _parse_price(row.get("Close", "")),
                    "open": _parse_price(row.get("Open", "")),
                    "high": _parse_price(row.get("High", "")),
                    "low": _parse_price(row.get("Low", "")),
                    "volume": _parse_int(row.get("TradingShares", "")),
                    "change": _parse_price(row.get("Change", "")),
                    "date": row.get("Date", ""),
                    "market": "TWO",
                }
    except Exception as e:
        print(f"       TPEx API 失敗（繼續）：{e}")

    _today_cache = result
    _cache_date = today
    return _today_cache


def _parse_price(raw: str) -> Optional[float]:
    try:
        return float(raw.replace(",", "").strip())
    except (ValueError, AttributeError):
        return None


def _parse_int(raw: str) -> Optional[int]:
    try:
        return int(raw.replace(",", "").strip())
    except (ValueError, AttributeError):
        return None


def get_price(stock_code: str) -> Optional[dict]:
    """查詢單一台股收盤資料。

    Returns:
        dict with keys: code, name, close, open, high, low, volume, change, date
        None if not found or market not yet closed today.
    """
    data = _load_today_all()
    return data.get(stock_code.strip())


def get_prices(codes: list[str]) -> dict[str, Optional[dict]]:
    """批次查詢多支股票（共用同一份快取，無額外 API 呼叫）。"""
    data = _load_today_all()
    return {code: data.get(code.strip()) for code in codes}


def get_yf_ticker(stock_code: str) -> str:
    """回傳 yfinance 用的 ticker，自動判斷上市（.TW）或上櫃（.TWO）。"""
    data = _load_today_all()
    info = data.get(stock_code.strip())
    suffix = ".TWO" if (info and info.get("market") == "TWO") else ".TW"
    return f"{stock_code.strip()}{suffix}"


def format_price_line(info: Optional[dict]) -> str:
    """格式化成一行摘要，供 Telegram 訊息用。"""
    if info is None:
        return "（查無資料）"
    change_str = ""
    if info["change"] is not None:
        sign = "+" if info["change"] >= 0 else ""
        change_str = f" {sign}{info['change']:.2f}"
    close_str = f"{info['close']:.2f}" if info["close"] is not None else "N/A"
    return f"{info['code']} {info['name']}  收 {close_str}{change_str}"
