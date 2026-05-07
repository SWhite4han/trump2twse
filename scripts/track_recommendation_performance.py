"""績效追蹤：評估昨日建議的實際表現。

流程：
  1. 讀取昨日 daily_report_{YYYYMMDD}.json
  2. 透過 TWSE OpenAPI 查今日收盤資料
  3. 判斷每筆建議的狀態（未觸發 / 觸發達標 / 觸發停損 / 觸發中）
  4. 結果存入 data/performance/{YYYY-MM}.csv
  5. 依績效產生規則更新建議，交給 auto_update_rules（shadow 模式下只記錄）

執行方式：
    python scripts/track_recommendation_performance.py
"""
from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from scripts.lib.config import config
from scripts.lib.twse_client import get_prices


# --------------------------------------------------------------------------- #
# 資料結構
# --------------------------------------------------------------------------- #

PERF_STATUS = {
    "not_triggered": "未觸發",
    "triggered_target": "觸發達標",
    "triggered_stop": "觸發停損",
    "triggered_hold": "觸發持有中",
    "no_data": "查無行情",
}


# --------------------------------------------------------------------------- #
# 公開介面
# --------------------------------------------------------------------------- #

def run(report_date: Optional[str] = None, shadow: bool = False) -> list[dict]:
    """執行績效追蹤。

    Args:
        report_date: 要評估的報告日期 YYYYMMDD，預設昨天。
        shadow: True 時，規則更新建議只記錄到 data/shadow_updates/，不實際改 YAML。

    Returns:
        每筆建議的績效結果。
    """
    if report_date is None:
        report_date = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")

    recs = _load_recommendations(report_date)
    if not recs:
        print(f"[perf] 找不到 {report_date} 的建議，中止。")
        return []

    codes = [r["code"] for r in recs]
    prices = get_prices(codes)

    results = []
    for rec in recs:
        price_info = prices.get(rec["code"])
        status = _evaluate(rec, price_info)
        results.append({**rec, **status})

    _save_csv(results, report_date)
    _print_summary(results, report_date)

    # Step 5：產生規則更新建議
    updates = _generate_updates(results, report_date)
    if updates:
        from scripts.auto_update_rules import apply_updates
        apply_updates(updates, shadow=shadow)

    return results


# --------------------------------------------------------------------------- #
# 讀取建議
# --------------------------------------------------------------------------- #

def _load_recommendations(date_str: str) -> list[dict]:
    report_file = config.data_dir / "reports" / f"daily_report_{date_str}.json"
    if not report_file.exists():
        return []
    with open(report_file, encoding="utf-8") as f:
        data = json.load(f)
    return data.get("recommendations", [])


# --------------------------------------------------------------------------- #
# 績效評估
# --------------------------------------------------------------------------- #

def _evaluate(rec: dict, price: Optional[dict]) -> dict:
    if price is None or price.get("close") is None:
        return {"status": "no_data", "status_label": PERF_STATUS["no_data"],
                "actual_low": None, "actual_high": None, "actual_close": None}

    lo = price["low"]
    hi = price["high"]
    close = price["close"]
    entry_low = rec.get("entry_low")
    entry_high = rec.get("entry_high")
    target = rec.get("target")
    stop_loss = rec.get("stop_loss")

    base = {"actual_low": lo, "actual_high": hi, "actual_close": close}

    # 未進入進場區間
    if entry_low is None or entry_high is None:
        return {**base, "status": "not_triggered", "status_label": PERF_STATUS["not_triggered"]}

    triggered = lo <= entry_high and hi >= entry_low
    if not triggered:
        return {**base, "status": "not_triggered", "status_label": PERF_STATUS["not_triggered"]}

    # 已觸發進場，判斷結果
    if target is not None and hi >= target:
        return {**base, "status": "triggered_target", "status_label": PERF_STATUS["triggered_target"]}
    if stop_loss is not None and lo <= stop_loss:
        return {**base, "status": "triggered_stop", "status_label": PERF_STATUS["triggered_stop"]}
    return {**base, "status": "triggered_hold", "status_label": PERF_STATUS["triggered_hold"]}


# --------------------------------------------------------------------------- #
# 存 CSV
# --------------------------------------------------------------------------- #

def _save_csv(results: list[dict], report_date: str) -> None:
    month = report_date[:6]  # YYYYMM
    perf_dir = config.data_dir / "performance"
    perf_dir.mkdir(parents=True, exist_ok=True)
    csv_file = perf_dir / f"{month}.csv"

    fieldnames = [
        "eval_date", "report_date", "code", "name", "rule_id",
        "entry_low", "entry_high", "target", "stop_loss",
        "actual_low", "actual_high", "actual_close",
        "status", "status_label",
    ]
    today = datetime.now().strftime("%Y-%m-%d")
    rows = [{
        "eval_date": today,
        "report_date": report_date,
        **{k: r.get(k, "") for k in fieldnames if k not in ("eval_date", "report_date")},
    } for r in results]

    write_header = not csv_file.exists()
    with open(csv_file, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        if write_header:
            writer.writeheader()
        writer.writerows(rows)

    print(f"[perf] 績效紀錄已附加 → {csv_file}")


# --------------------------------------------------------------------------- #
# 終端機摘要
# --------------------------------------------------------------------------- #

def _print_summary(results: list[dict], report_date: str) -> None:
    total = len(results)
    counts: dict[str, int] = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1

    print(f"\n📊 {report_date} 績效摘要（共 {total} 筆）")
    print("─" * 50)
    for status, label in PERF_STATUS.items():
        print(f"  {label}：{counts.get(status, 0)}")
    print("─" * 50)
    for r in results:
        close = r.get("actual_close")
        close_str = f"收 {close:.2f}" if close else "N/A"
        print(f"  {r.get('code', '')} {r.get('name', ''):<8} {r.get('status_label', ''):<8} {close_str}")


# --------------------------------------------------------------------------- #
# 績效 → 規則更新
# --------------------------------------------------------------------------- #

def _generate_updates(results: list[dict], report_date: str) -> list[dict]:
    """依據績效結果產生規則異動建議。

    規則（保守版，Phase 6 先用這組觀察）：
    - 同一規則觸發 >= 2 筆，且停損率 > 60% → DOWNGRADE（bullish → mixed）
    - 停損率 < 20% 且達標率 >= 50% → 暫不異動（保持樂觀）
    - 樣本 < 2 筆 → 跳過（資料太少）
    """
    # 按 rule_id 彙整
    by_rule: dict[str, list[dict]] = {}
    for r in results:
        rule_id = r.get("rule_id", "")
        if not rule_id:
            continue
        by_rule.setdefault(rule_id, []).append(r)

    updates: list[dict] = []
    for rule_id, recs in by_rule.items():
        triggered = [r for r in recs if r["status"] not in ("not_triggered", "no_data")]
        if len(triggered) < 2:
            continue  # 樣本不足

        stops = sum(1 for r in triggered if r["status"] == "triggered_stop")
        targets = sum(1 for r in triggered if r["status"] == "triggered_target")
        stop_rate = stops / len(triggered)

        if stop_rate > 0.6:
            updates.append({
                "operation": "DOWNGRADE",
                "event": rule_id,
                "reason": (
                    f"{report_date} 績效：{stops}/{len(triggered)} 觸發停損"
                    f"（停損率 {stop_rate:.0%}）"
                ),
                "evidence_source": "quant",
            })
            print(f"[perf] 建議 DOWNGRADE 規則「{rule_id}」（停損率 {stop_rate:.0%}）")
        else:
            print(
                f"[perf] 規則「{rule_id}」表現可接受"
                f"（達標 {targets}，停損 {stops}，共 {len(triggered)} 筆觸發）"
            )

    return updates


if __name__ == "__main__":
    run()
