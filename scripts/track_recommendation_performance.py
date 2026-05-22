"""績效追蹤：持倉管理 + 自動結案。

每日流程：
  1. 載入 open_positions.json（所有進行中的追蹤倉位）
  2. 載入昨日 daily_report 的新推薦
  3. 對所有持倉查今日 TWSE 收盤：
     - 已進場（holding）→ 判斷觸達目標 / 停損
     - 未進場（watching）→ 判斷是否進入進場區間
  4. 新推薦的股票若已在持倉中 → 以「被覆蓋」結案，開新倉
  5. 超過 MAX_WATCH_DAYS 仍未進場 → 以「逾期」結案
  6. 結案的倉位寫入 data/performance/YYYYMM.csv
  7. 依結案績效產生規則更新建議

收盤原因（close_reason）：
  triggered_target  觸達目標價
  triggered_stop    觸達停損價
  superseded        相同股票出現新推薦（覆蓋）
  expired           MAX_WATCH_DAYS 天內未進場
"""
from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from scripts.lib.config import config
from scripts.lib.twse_client import get_prices

MAX_WATCH_DAYS = 10  # 未進場的最長觀察天數

CLOSE_REASON_LABEL = {
    "triggered_target": "✅ 觸達目標",
    "triggered_stop":   "❌ 觸達停損",
    "superseded":       "🔄 新推薦覆蓋",
    "expired":          "⏰ 逾期未進場",
}

OPEN_POSITIONS_FILE = lambda: config.data_dir / "performance" / "open_positions.json"


# --------------------------------------------------------------------------- #
# 公開介面
# --------------------------------------------------------------------------- #

def run(report_date: Optional[str] = None, shadow: bool = False,
        today_str: Optional[str] = None) -> dict:
    """
    report_date: 昨日報告日期（YYYYMMDD），預設自動推算
    today_str:   今日日期（YYYY-MM-DD），補跑時傳入以避免用系統 now
    """
    if today_str:
        from datetime import datetime as _dt
        _today = _dt.strptime(today_str, "%Y-%m-%d")
        today = _today.strftime("%Y%m%d")
        if report_date is None:
            report_date = (_today - timedelta(days=1)).strftime("%Y%m%d")
    else:
        today = datetime.now().strftime("%Y%m%d")
        if report_date is None:
            report_date = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")

    # 1. 載入持倉與新推薦
    open_pos        = _load_open_positions()
    new_recs        = _load_recommendations(report_date)
    position_updates = _load_position_updates(report_date)

    if not open_pos and not new_recs:
        print(f"[perf] 找不到 {report_date} 的建議，且無進行中持倉，中止。")
        return {"open": [], "closed": []}

    # 2. 查詢所有相關股票今日價格
    all_codes = list({p["code"] for p in open_pos} | {r["code"] for r in new_recs})
    prices = get_prices(all_codes) if all_codes else {}

    # 4. 更新現有持倉
    still_open: list[dict] = []
    closed:     list[dict] = []

    for pos in open_pos:
        code  = pos["code"]
        price = prices.get(code)

        # 套用 raise_target 調整（不關閉倉位）
        if code in position_updates:
            upd = position_updates[code]
            if upd.get("new_target"):
                pos["target"] = upd["new_target"]
            if upd.get("new_stop"):
                pos["stop_loss"] = upd["new_stop"]

        # 逾期未進場
        pos["days_watched"] = pos.get("days_watched", 0) + 1
        if pos["days_watched"] > MAX_WATCH_DAYS and pos.get("state") == "watching":
            _close(pos, "expired", price, today)
            closed.append(pos)
            continue

        # 無今日行情（休市）→ 繼續持倉
        if not price or price.get("close") is None:
            still_open.append(pos)
            continue

        lo, hi, close_p = price["low"], price["high"], price["close"]
        op = price.get("open")
        pos["last_close"] = close_p

        # 嘗試進場
        if pos.get("state") == "watching":
            el, eh = pos.get("entry_low"), pos.get("entry_high")
            if el and eh and lo is not None and hi is not None:
                if lo <= eh and hi >= el:
                    pos["state"]            = "holding"
                    pos["entry_date"]       = today
                    # 保守估計：限價單在 entry_high，若開盤更低則用開盤價
                    pos["actual_entry_price"] = round(min(op, eh), 2) if op else eh

        # 判斷結案（holding）
        if pos.get("state") == "holding":
            target    = pos.get("target")
            stop_loss = pos.get("stop_loss")
            if target and hi is not None and hi >= target:
                _close(pos, "triggered_target", price, today)
                closed.append(pos)
                continue
            if stop_loss and lo is not None and lo <= stop_loss:
                _close(pos, "triggered_stop", price, today)
                closed.append(pos)
                continue

        still_open.append(pos)

    # 5. 新推薦加入持倉
    for rec in new_recs:
        if not rec.get("entry_low") or not rec.get("entry_high"):
            continue  # 無進場區間無法追蹤

        price = prices.get(rec["code"])
        pos = {
            "code":               rec["code"],
            "name":               rec.get("name", ""),
            "rule_id":            rec.get("rule_id", ""),
            "action":             rec.get("action", ""),
            "entry_low":          rec.get("entry_low"),
            "entry_high":         rec.get("entry_high"),
            "target":             rec.get("target"),
            "stop_loss":          rec.get("stop_loss"),
            "report_date":        report_date,
            "confidence":         rec.get("confidence"),
            "state":              "watching",
            "entry_date":         None,
            "actual_entry_price": None,
            "days_watched":       0,
            "last_close":         price.get("close") if price else None,
        }

        # 當天即觸發進場
        if price and price.get("low") is not None:
            lo, hi = price["low"], price["high"]
            op = price.get("open")
            if lo <= pos["entry_high"] and hi >= pos["entry_low"]:
                pos["state"]              = "holding"
                pos["entry_date"]         = today
                pos["actual_entry_price"] = round(min(op, pos["entry_high"]), 2) if op else pos["entry_high"]

                if pos["target"] and hi >= pos["target"]:
                    _close(pos, "triggered_target", price, today)
                    closed.append(pos)
                    continue
                if pos["stop_loss"] and lo <= pos["stop_loss"]:
                    _close(pos, "triggered_stop", price, today)
                    closed.append(pos)
                    continue

        still_open.append(pos)

    # 6. 存檔
    _save_open_positions(still_open)
    if closed:
        _save_csv(closed)

    _print_summary(still_open, closed, report_date)

    # 7. 規則更新建議
    updates = _generate_updates(closed, report_date)
    if updates:
        from scripts.auto_update_rules import apply_updates
        apply_updates(updates, shadow=shadow)

    return {"open": still_open, "closed": closed}


# --------------------------------------------------------------------------- #
# 持倉 IO
# --------------------------------------------------------------------------- #

def _load_open_positions() -> list[dict]:
    f = OPEN_POSITIONS_FILE()
    if not f.exists():
        return []
    with open(f, encoding="utf-8") as fp:
        return json.load(fp)


def _save_open_positions(positions: list[dict]) -> None:
    f = OPEN_POSITIONS_FILE()
    f.parent.mkdir(parents=True, exist_ok=True)
    with open(f, "w", encoding="utf-8") as fp:
        json.dump(positions, fp, ensure_ascii=False, indent=2)


def _load_recommendations(date_str: str) -> list[dict]:
    report_file = config.data_dir / "reports" / f"daily_report_{date_str}.json"
    if not report_file.exists():
        return []
    with open(report_file, encoding="utf-8") as f:
        return json.load(f).get("recommendations", [])


def _load_position_updates(date_str: str) -> dict[str, dict]:
    """回傳 {code: update_dict}，只含 raise_target 類型。"""
    report_file = config.data_dir / "reports" / f"daily_report_{date_str}.json"
    if not report_file.exists():
        return {}
    with open(report_file, encoding="utf-8") as f:
        updates = json.load(f).get("position_updates", [])
    return {u["code"]: u for u in updates if u.get("update_type") == "raise_target"}


# --------------------------------------------------------------------------- #
# 結案
# --------------------------------------------------------------------------- #

def _close(pos: dict, reason: str, price: Optional[dict], today: str) -> None:
    pos["close_reason"] = reason
    pos["close_date"]   = today
    if price:
        pos["actual_close"] = price.get("close")
        pos["actual_low"]   = price.get("low")
        pos["actual_high"]  = price.get("high")


# --------------------------------------------------------------------------- #
# CSV（結案記錄）
# --------------------------------------------------------------------------- #

def _save_csv(closed: list[dict]) -> None:
    if not closed:
        return
    month     = datetime.now().strftime("%Y%m")
    perf_dir  = config.data_dir / "performance"
    perf_dir.mkdir(parents=True, exist_ok=True)
    csv_file  = perf_dir / f"{month}.csv"

    fieldnames = [
        "report_date", "close_date", "code", "name", "rule_id", "action",
        "entry_low", "entry_high", "target", "stop_loss",
        "entry_date", "actual_entry_price", "state", "close_reason",
        "actual_low", "actual_high", "actual_close",
        "days_watched", "confidence",
    ]

    # 讀取已存在的 (report_date, code) 集合，防止重複寫入
    existing_keys: set[tuple] = set()
    if csv_file.exists():
        with open(csv_file, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                existing_keys.add((row["report_date"], row["code"]))

    new_rows = [r for r in closed if (r.get("report_date"), r.get("code")) not in existing_keys]
    if not new_rows:
        return

    write_header = not csv_file.exists()
    with open(csv_file, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        if write_header:
            writer.writeheader()
        writer.writerows(new_rows)
    print(f"[perf] {len(new_rows)} 筆結案 → {csv_file}")


# --------------------------------------------------------------------------- #
# 終端機摘要
# --------------------------------------------------------------------------- #

def _print_summary(open_pos: list[dict], closed: list[dict], report_date: str) -> None:
    print(f"\n📊 績效摘要（評估報告日：{report_date}）")
    print("─" * 55)

    if closed:
        print(f"  本次結案 {len(closed)} 筆：")
        for p in closed:
            reason = CLOSE_REASON_LABEL.get(p.get("close_reason",""), p.get("close_reason",""))
            close  = p.get("actual_close")
            close_str = f"收 {close:.1f}" if close else ""
            print(f"    {p['code']} {p.get('name',''):<8} {reason}  {close_str}")

    holding  = [p for p in open_pos if p.get("state") == "holding"]
    watching = [p for p in open_pos if p.get("state") == "watching"]
    print(f"\n  持倉中（已進場）：{len(holding)} 筆")
    for p in holding:
        lc = p.get("last_close")
        lc_str = f"現價 {lc:.1f}" if lc else ""
        print(f"    {p['code']} {p.get('name',''):<8} 目標 {p.get('target','?')}  停損 {p.get('stop_loss','?')}  {lc_str}")

    print(f"  觀察中（未進場）：{len(watching)} 筆")
    print("─" * 55)


# --------------------------------------------------------------------------- #
# 規則更新
# --------------------------------------------------------------------------- #

def _generate_updates(closed: list[dict], report_date: str) -> list[dict]:
    from collections import defaultdict
    by_rule: dict[str, list[dict]] = defaultdict(list)
    for p in closed:
        if p.get("close_reason") in ("triggered_target", "triggered_stop"):
            by_rule[p.get("rule_id","")].append(p)

    updates = []
    for rule_id, recs in by_rule.items():
        if len(recs) < 2:
            continue
        stops     = sum(1 for r in recs if r["close_reason"] == "triggered_stop")
        stop_rate = stops / len(recs)
        if stop_rate > 0.6:
            updates.append({
                "operation":       "DOWNGRADE",
                "event":           rule_id,
                "reason":          f"{report_date} 績效：停損率 {stop_rate:.0%}（{stops}/{len(recs)} 筆）",
                "evidence_source": "quant",
            })
            print(f"[perf] 建議 DOWNGRADE：「{rule_id}」停損率 {stop_rate:.0%}")
    return updates


if __name__ == "__main__":
    run()
