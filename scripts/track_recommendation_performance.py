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
  triggered_target   觸達目標價
  triggered_stop     觸達停損價
  superseded         相同股票出現新推薦（覆蓋）
  expired            MAX_WATCH_DAYS 天內未進場
  expired_validity   LLM 推薦的 validity_days 已到，仍未進場
"""
from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from scripts.lib.config import config
from scripts.lib.twse_client import get_prices

MAX_WATCH_DAYS    = 10       # 未進場的最長觀察天數
CAPITAL_PER_TRADE = 100_000  # 每筆模擬資金

# 時間視野對應預設值（與 daily_pipeline._HORIZON_DEFAULT_VALIDITY 對齊）
HORIZON_DEFAULT_MAX_HOLDING = {"event": 30, "trend": 90, "cycle": 180}
HORIZON_DEFAULT_VALIDITY    = {"event": 5,  "trend": 7,  "cycle": 10}

CLOSE_REASON_LABEL = {
    "triggered_target":  "✅ 觸達目標",
    "triggered_stop":    "❌ 觸達停損",
    "superseded":        "🔄 新推薦覆蓋",
    "expired":           "⏰ 逾期未進場",
    "expired_validity":  "⌛ 推薦失效",
}

OPEN_POSITIONS_FILE = lambda: config.data_dir / "performance" / "open_positions.json"

ACTION_SELL = "觀察賣出"
ACTION_BUY  = "觀察買進"


# --------------------------------------------------------------------------- #
# 進出場判斷輔助函式
# --------------------------------------------------------------------------- #

def _try_enter_position(pos: dict, lo: float, hi: float, op, today: str) -> bool:
    """嘗試觸發進場：若今日區間重疊進場區，mutate pos 並回傳 True。"""
    el, eh = pos.get("entry_low"), pos.get("entry_high")
    if not (el and eh and lo is not None and hi is not None):
        return False
    if not (lo <= eh and hi >= el):
        return False
    is_sell = pos.get("action") == ACTION_SELL
    pos["state"]      = "holding"
    pos["entry_date"] = today
    if op:
        if is_sell:
            pos["actual_entry_price"] = round(min(max(op, el), eh), 2)
        else:
            pos["actual_entry_price"] = round(max(min(op, eh), el), 2)
    else:
        pos["actual_entry_price"] = eh if not is_sell else el
    return True


def _check_close_triggers(pos: dict, lo: float, hi: float,
                          price: dict, today: str,
                          closed: list) -> bool:
    """檢查 holding 部位是否觸達停損/目標，若是則結案並 append 到 closed，回傳 True。

    停損優先（同日雙向觸達時保守假設先碰停損）。
    """
    target    = pos.get("target")
    stop_loss = pos.get("stop_loss")
    is_sell   = pos.get("action") == ACTION_SELL
    if is_sell:
        if stop_loss and hi is not None and hi >= stop_loss:
            _close(pos, "triggered_stop", price, today)
            closed.append(pos)
            return True
        if target and lo is not None and lo <= target:
            _close(pos, "triggered_target", price, today)
            closed.append(pos)
            return True
    else:
        if stop_loss and lo is not None and lo <= stop_loss:
            _close(pos, "triggered_stop", price, today)
            closed.append(pos)
            return True
        if target and hi is not None and hi >= target:
            _close(pos, "triggered_target", price, today)
            closed.append(pos)
            return True
    return False


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
    if _backfill_horizon_validity(open_pos):
        _save_open_positions(open_pos)
        print(f"[perf] open_positions backfill 完成（horizon/validity 欄位）")
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
            # 方案 D：第一次強化時鎖定原始 target，作為日後 cap 1.5× 的基準
            if upd.get("source") == "reinforce" and not pos.get("target_original"):
                pos["target_original"] = pos.get("target")
            if upd.get("new_target"):
                pos["target"] = upd["new_target"]
            if upd.get("new_stop"):
                new_stop = upd["new_stop"]
                el = pos.get("entry_low") or 0
                is_sell = pos.get("action") == ACTION_SELL
                stop_ok = (not is_sell and new_stop < el) or \
                          (is_sell and new_stop > (pos.get("entry_high") or 0))
                if stop_ok:
                    pos["stop_loss"] = new_stop
                else:
                    print(f"[perf] 拒絕無效 stop 更新 {pos['code']}："
                          f"new_stop={new_stop} 與進場區間倒掛，保留原值 {pos['stop_loss']}")
            # 紀錄強化日期供節流檢查（無論 source 都記，方便除錯）
            pos["last_reinforced_date"] = upd.get("reinforced_on") or report_date

        # 逾期未進場：validity_days 與 MAX_WATCH_DAYS 取較小者
        pos["days_watched"] = pos.get("days_watched", 0) + 1
        if pos.get("state") == "watching":
            v_days = pos.get("validity_days")
            try:
                v_days = int(v_days) if v_days is not None else MAX_WATCH_DAYS
            except (TypeError, ValueError):
                v_days = MAX_WATCH_DAYS
            limit = min(MAX_WATCH_DAYS, v_days)
            if pos["days_watched"] > limit:
                reason = "expired_validity" if v_days < MAX_WATCH_DAYS else "expired"
                _close(pos, reason, price, today)
                closed.append(pos)
                continue

        # 持倉超過時間視野上限（holding 專用）
        if pos.get("state") == "holding" and pos.get("entry_date"):
            entry_dt = datetime.strptime(pos["entry_date"], "%Y%m%d")
            today_dt = datetime.strptime(today, "%Y%m%d")
            days_held = (today_dt - entry_dt).days
            if days_held >= pos.get("max_holding_days", 90):
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
            _try_enter_position(pos, lo, hi, op, today)

        # 判斷結案（holding）—— 停損優先
        if pos.get("state") == "holding":
            if _check_close_triggers(pos, lo, hi, price, today, closed):
                continue

        still_open.append(pos)

    # 5. 新推薦加入持倉
    for rec in new_recs:
        if not rec.get("entry_low") or not rec.get("entry_high"):
            continue  # 無進場區間無法追蹤

        price = prices.get(rec["code"])
        h = rec.get("horizon", "trend")
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
            "horizon":            h,
            "max_holding_days":   rec.get("max_holding_days") or HORIZON_DEFAULT_MAX_HOLDING.get(h, 90),
            "validity_days":      rec.get("validity_days") or HORIZON_DEFAULT_VALIDITY.get(h, 7),
            "valid_until":        rec.get("valid_until"),
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
            if _try_enter_position(pos, lo, hi, op, today):
                # 同日結案判斷（停損優先）
                if _check_close_triggers(pos, lo, hi, price, today, closed):
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


def _load_rules_horizon_map() -> dict[str, str]:
    """rule_id → horizon。讀失敗回傳 {}。"""
    try:
        from ruamel.yaml import YAML as _YAML
        with open(config.rules_file, encoding="utf-8") as f:
            rules = _YAML().load(f) or []
        return {r["event"]: r.get("horizon", "trend") for r in rules if "event" in r}
    except Exception:
        return {}


def _backfill_horizon_validity(positions: list[dict]) -> bool:
    """為缺欄位的部位補上 horizon / max_holding_days / validity_days / valid_until。

    來源：rule YAML 反查 horizon，再依 horizon 推 max_holding_days 與 validity_days。
    valid_until = report_date + validity_days。

    回傳是否有變動（外部決定要不要寫回檔）。
    """
    if not positions:
        return False
    horizon_map = _load_rules_horizon_map()
    changed = False
    for p in positions:
        h = p.get("horizon")
        if not h:
            h = horizon_map.get(p.get("rule_id", ""), "trend")
            p["horizon"] = h
            changed = True
        if not p.get("max_holding_days"):
            p["max_holding_days"] = HORIZON_DEFAULT_MAX_HOLDING.get(h, 90)
            changed = True
        if not p.get("validity_days"):
            p["validity_days"] = HORIZON_DEFAULT_VALIDITY.get(h, 7)
            changed = True
        if not p.get("valid_until") and p.get("report_date"):
            from scripts.daily_pipeline import _compute_valid_until
            p["valid_until"] = _compute_valid_until(p["report_date"], p["validity_days"])
            changed = True
    return changed


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
    """回傳 {code: update_dict}，只含 raise_target 類型且非 shadow。"""
    report_file = config.data_dir / "reports" / f"daily_report_{date_str}.json"
    if not report_file.exists():
        return {}
    with open(report_file, encoding="utf-8") as f:
        updates = json.load(f).get("position_updates", [])
    return {
        u["code"]: u
        for u in updates
        if u.get("update_type") == "raise_target" and not u.get("shadow")
    }


# --------------------------------------------------------------------------- #
# 結案
# --------------------------------------------------------------------------- #

def _close(pos: dict, reason: str, price: Optional[dict], today: str) -> None:
    pos["close_reason"] = reason
    pos["close_date"]   = today
    if price:
        pos["actual_close"] = price.get("close")

    # exit_price：限價成交用 target/stop_loss 價；其他用收盤
    if reason == "triggered_target":
        exit_p = pos.get("target")
    elif reason == "triggered_stop":
        exit_p = pos.get("stop_loss")
    else:
        exit_p = pos.get("actual_close")
    pos["exit_price"] = exit_p

    # P&L
    entry_p = pos.get("actual_entry_price")
    if entry_p and exit_p:
        try:
            ep, cp   = float(entry_p), float(exit_p)
            is_sell  = pos.get("action") == ACTION_SELL
            raw      = (ep - cp) / ep if is_sell else (cp - ep) / ep
            pos["pnl_pct"] = round(raw * 100, 2)
            pos["pnl_twd"] = round(raw * CAPITAL_PER_TRADE, 0)
        except (TypeError, ValueError, ZeroDivisionError):
            pass


# --------------------------------------------------------------------------- #
# CSV（結案記錄）
# --------------------------------------------------------------------------- #

DEFAULT_FIELDNAMES = [
    "report_date", "code", "name", "rule_id", "action",
    "entry_low", "entry_high", "target", "stop_loss",
    "entry_date", "actual_entry_price", "actual_close", "exit_price",
    "close_reason", "close_date",
    "pnl_pct", "pnl_twd", "days_watched", "confidence",
    "horizon", "max_holding_days",
    "validity_days", "valid_until",
]


def _save_csv(closed: list[dict]) -> None:
    if not closed:
        return
    month     = datetime.now().strftime("%Y%m")
    perf_dir  = config.data_dir / "performance"
    perf_dir.mkdir(parents=True, exist_ok=True)
    csv_file  = perf_dir / f"{month}.csv"

    # 讀取已存在的 header（保持 schema 一致），同時收集已有 (report_date, code) 集合
    existing_rows: list[dict] = []
    existing_keys: set[tuple] = set()
    existing_fields: list[str] = []
    if csv_file.exists():
        with open(csv_file, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            existing_fields = list(reader.fieldnames or [])
            for row in reader:
                existing_rows.append(row)
                existing_keys.add((row["report_date"], row["code"]))

    new_rows = [r for r in closed if (r.get("report_date"), r.get("code")) not in existing_keys]
    if not new_rows:
        return

    # 聯集 fieldnames：DEFAULT 為基底，加上既有 CSV 額外欄位，再加新 row 額外欄位
    fieldnames = list(DEFAULT_FIELDNAMES)
    for extra in existing_fields + [k for r in new_rows for k in r.keys()]:
        if extra not in fieldnames:
            fieldnames.append(extra)

    needs_rewrite = existing_rows and existing_fields != fieldnames
    if needs_rewrite:
        with open(csv_file, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(existing_rows)
            writer.writerows(new_rows)
    else:
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
