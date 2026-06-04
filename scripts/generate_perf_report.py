"""從 data/performance/*.csv 生成 PERFORMANCE.md。

每次 pipeline 跑完後自動呼叫，結果 commit 到 git，GitHub 上即時可見。
"""
from __future__ import annotations

import csv
import json
import subprocess
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from scripts.lib.config import config
from scripts.track_recommendation_performance import MAX_WATCH_DAYS

CAPITAL_PER_TRADE = 100_000  # 每筆假設投入 10 萬
BASELINE_DATE     = "20260514"  # TA+LLM 系統正式上線日，之前資料不計入績效基準

PERF_STATUS_EMOJI = {
    "triggered_target": "✅ 達標",
    "triggered_stop":   "❌ 停損",
    "triggered_hold":   "⏳ 持有",
    "not_triggered":    "⬜ 未觸發",
    "no_data":          "➖ 無資料",
}


def run(date_str: str | None = None) -> None:
    rows      = _load_all_csv()
    open_pos  = _load_open_positions()

    if not rows and not open_pos:
        print("[report] 尚無績效資料，略過。")
        return

    # 累積績效驅動的規則自動調整（先更新 YAML，再產 PERFORMANCE.md 才能反映最新狀態）
    rule_updates = _check_rule_performance_updates(rows)
    if rule_updates:
        from scripts.auto_update_rules import apply_updates
        apply_updates(rule_updates)

    md = _build_markdown(rows, open_pos)
    out = config.project_root / "PERFORMANCE.md"
    out.write_text(md, encoding="utf-8")
    print(f"[report] → {out}")

    _git_commit(str(out), date_str=date_str)


# --------------------------------------------------------------------------- #

def _load_all_csv() -> list[dict]:
    perf_dir = config.data_dir / "performance"
    rows: list[dict] = []
    for f in sorted(perf_dir.glob("*.csv")):
        with open(f, encoding="utf-8", newline="") as fp:
            rows.extend(csv.DictReader(fp))
    return rows


def _load_open_positions() -> list[dict]:
    f = config.data_dir / "performance" / "open_positions.json"
    if not f.exists():
        return []
    with open(f, encoding="utf-8") as fp:
        return json.load(fp)


def _compute_stats(rows: list[dict]) -> dict:
    """計算一組 rows 的績效指標（只計算已進場且已結案的交易）。"""
    closed = [r for r in rows if r.get("close_reason") in ("triggered_target", "triggered_stop")]
    target_hits = sum(1 for r in closed if r["close_reason"] == "triggered_target")
    stop_hits   = sum(1 for r in closed if r["close_reason"] == "triggered_stop")

    gains, losses, pct_list = [], [], []
    for r in closed:
        try:
            pnl_twd = float(r.get("pnl_twd") or 0)
            pnl_pct = float(r.get("pnl_pct") or 0)
        except (ValueError, TypeError):
            # fallback：從進出場價計算
            try:
                ep = float(r["actual_entry_price"])
                cp = float(r.get("exit_price") or r.get("actual_close") or 0)
                is_sell = r.get("action") == "觀察賣出"
                raw = (ep - cp) / ep if is_sell else (cp - ep) / ep
                pnl_twd = round(raw * CAPITAL_PER_TRADE, 0)
                pnl_pct = round(raw * 100, 2)
            except (ValueError, TypeError, ZeroDivisionError):
                continue
        if pnl_twd > 0:
            gains.append(pnl_twd)
        elif pnl_twd < 0:
            losses.append(pnl_twd)
        pct_list.append(pnl_pct)

    total_pnl = sum(gains) + sum(losses)
    profit_factor = (sum(gains) / abs(sum(losses))) if losses else None
    avg_pct = (sum(pct_list) / len(pct_list)) if pct_list else None

    return {
        "closed": len(closed),
        "target_hits": target_hits,
        "stop_hits": stop_hits,
        "total_pnl": total_pnl,
        "profit_factor": profit_factor,
        "avg_pct": avg_pct,
        "pnl_count": len(pct_list),
    }


def _fmt_stats_rows(s: dict) -> list[str]:
    n = s["closed"]
    hit_rate  = f"{s['target_hits']/n*100:.0f}%" if n else "—"
    stop_rate = f"{s['stop_hits']/n*100:.0f}%"  if n else "—"
    pf_str    = f"{s['profit_factor']:.2f}" if s["profit_factor"] is not None else "—"
    avg_str   = f"{s['avg_pct']:+.1f}%" if s["avg_pct"] is not None else "—"
    pnl_str   = f"{s['total_pnl']:+,.0f} 元（{s['pnl_count']} 筆）" if s["pnl_count"] else "—"
    return [
        f"| 完整交易筆數（有進出場）| {n} |",
        f"| 達標率 | {hit_rate} |",
        f"| 停損率 | {stop_rate} |",
        f"| Profit Factor | {pf_str} |",
        f"| 平均每筆損益 | {avg_str} |",
        f"| 模擬損益合計 | {pnl_str} |",
    ]


def _build_markdown(rows: list[dict], open_pos: list[dict]) -> str:
    now  = datetime.now().strftime("%Y-%m-%d %H:%M")
    days = sorted({r["report_date"] for r in rows})

    stats_all      = _compute_stats(rows)
    baseline_rows  = [r for r in rows if r.get("report_date", "") >= BASELINE_DATE]
    stats_baseline = _compute_stats(baseline_rows)

    lines: list[str] = [
        "# Performance Report",
        "",
        f"> 自動生成　最後更新：{now}",
        "",
        "## 總覽",
        "",
        f"| 項目 | 數值 |",
        f"|---|---|",
        f"| 觀察天數 | {len(days)} 天（{days[0] if days else '—'} ～ {days[-1] if days else '—'}）|",
        f"| 總建議筆數 | {len(rows)} |",
        "",
        f"### 全期績效",
        "",
        "| 項目 | 數值 |",
        "|---|---|",
        *_fmt_stats_rows(stats_all),
        "",
        f"### 基準期績效（≥{BASELINE_DATE}，TA+LLM 定價後）",
        "",
        "| 項目 | 數值 |",
        "|---|---|",
        *_fmt_stats_rows(stats_baseline),
        "",
    ]

    # 各規則勝率（只統計有進出場的交易）
    closed_rows = [r for r in rows if r.get("close_reason") in ("triggered_target", "triggered_stop")]
    by_rule: dict[str, list[dict]] = defaultdict(list)
    for r in closed_rows:
        by_rule[r.get("rule_id", "未知")].append(r)

    if by_rule:
        from scripts.auto_update_rules import _load_rules as _lr
        try:
            _rules_map = {r["event"]: r for r in _lr()}
        except Exception:
            _rules_map = {}

        lines += [
            "## 各規則觸發績效",
            "",
            "| 規則 | 觸發 | ✅ 達標 | ❌ 停損 | 達標率 | 狀態 |",
            "|---|---|---|---|---|---|",
        ]
        for rule, recs in sorted(by_rule.items(), key=lambda x: -len(x[1])):
            t = sum(1 for r in recs if r["close_reason"] == "triggered_target")
            s = sum(1 for r in recs if r["close_reason"] == "triggered_stop")
            rate = f"{t/len(recs)*100:.0f}%" if recs else "—"
            rule_cfg = _rules_map.get(rule)
            if rule_cfg is None:
                status = "⚫ 已移除"
            elif rule_cfg.get("disabled"):
                status = "🔴 停用"
            else:
                min_conf = rule_cfg.get("min_confidence", 0.4)
                status   = f"🟢 門檻 {min_conf:.2f}"
            lines.append(f"| {rule} | {len(recs)} | {t} | {s} | {rate} | {status} |")
        lines.append("")

    # 最近 30 筆明細
    recent = sorted(rows, key=lambda r: r.get("close_date",""), reverse=True)[:30]
    if recent:
        lines += [
            "## 最近建議明細",
            "",
            "| 日期 | 股票 | 規則 | 進場價 | 目標 | 停損 | 出場價 | 當日收盤 | 損益(元) | 結果 |",
            "|---|---|---|---|---|---|---|---|---|---|",
        ]
        for r in recent:
            entry_price = r.get("actual_entry_price") or "—"
            exit_p   = r.get("exit_price") or "—"
            actual_c = r.get("actual_close") or "—"
            status = PERF_STATUS_EMOJI.get(r.get("close_reason",""), r.get("status_label",""))
            try:
                ep  = float(entry_price)
                cp  = float(exit_p)
                is_sell = r.get("action") == "觀察賣出"
                raw = (ep - cp) / ep if is_sell else (cp - ep) / ep
                ret      = f"({raw*100:+.1f}%)"
                pnl_cell = f"{raw*CAPITAL_PER_TRADE:+,.0f}"
            except (ValueError, TypeError):
                ret      = ""
                pnl_cell = "—"
            lines.append(
                f"| {r.get('report_date','')} "
                f"| {r.get('code','')} {r.get('name','')} "
                f"| {r.get('rule_id','')} "
                f"| {entry_price} "
                f"| {r.get('target','—')} "
                f"| {r.get('stop_loss','—')} "
                f"| {exit_p} {ret}"
                f"| {actual_c} "
                f"| {pnl_cell} "
                f"| {status} |"
            )
        lines.append("")

    # 持倉中（open positions）
    holding  = [p for p in open_pos if p.get("state") == "holding"]
    watching = [p for p in open_pos if p.get("state") == "watching"]

    if holding or watching:
        lines += ["## 目前持倉", ""]
        if holding:
            lines += [
                f"### 已進場（{len(holding)} 筆）",
                "",
                "| 股票 | 規則 | 進場日 | 進場價 | 目標 | 停損 | 現價 | 浮動損益 |",
                "|---|---|---|---|---|---|---|---|",
            ]
            for p in holding:
                lc = p.get("last_close")
                ep = p.get("actual_entry_price")
                lc_str = f"{lc:.1f}" if lc else "—"
                ep_str = f"{ep:.1f}" if ep else "—"
                try:
                    is_sell = p.get("action") == "觀察賣出"
                    raw = (ep - lc) / ep if is_sell else (lc - ep) / ep
                    pnl = f"{raw * 100:+.1f}%"
                except (TypeError, ZeroDivisionError):
                    pnl = "—"
                lines.append(
                    f"| {p['code']} {p.get('name','')} "
                    f"| {p.get('rule_id','')} "
                    f"| {p.get('entry_date','—')} "
                    f"| {ep_str} "
                    f"| {p.get('target','—')} "
                    f"| {p.get('stop_loss','—')} "
                    f"| {lc_str} "
                    f"| {pnl} |"
                )
            lines.append("")
        if watching:
            lines += [
                f"### 觀察中－等待進場（{len(watching)} 筆）",
                "",
                "| 股票 | 規則 | 推薦日 | 進場區間 | 目標 | 停損 | 觀察天數 |",
                "|---|---|---|---|---|---|---|",
            ]
            for p in watching:
                entry = f"{p.get('entry_low','—')}–{p.get('entry_high','—')}"
                lines.append(
                    f"| {p['code']} {p.get('name','')} "
                    f"| {p.get('rule_id','')} "
                    f"| {p.get('report_date','—')} "
                    f"| {entry} "
                    f"| {p.get('target','—')} "
                    f"| {p.get('stop_loss','—')} "
                    f"| {p.get('days_watched',0)}/{MAX_WATCH_DAYS} |"
                )
            lines.append("")

    lines += [
        "---",
        "",
        "_資料來源：TWSE OpenAPI　系統：[trump2twse](https://github.com/SWhite4han/trump2twse)_",
    ]
    return "\n".join(lines) + "\n"


def _check_rule_performance_updates(rows: list[dict]) -> list[dict]:
    """依累積績效提出規則自動調整（DISABLE / CONFIDENCE_ADJUST）。

    門檻：
      DISABLE          win_rate < 25% 且 closed_trades ≥ 5
      CONFIDENCE 放寬  win_rate > 70% 且 closed_trades ≥ 5 → min_confidence = 0.30
      CONFIDENCE 收緊  win_rate < 40% 且 closed_trades ≥ 5 → min_confidence = 0.50
    """
    DISABLE_WIN_RATE  = 0.25
    BOOST_WIN_RATE    = 0.70
    TIGHTEN_WIN_RATE  = 0.40
    MIN_TRADES        = 5

    from scripts.auto_update_rules import _load_rules, _find_rule
    rules_list = _load_rules()
    rules_map  = {r["event"]: r for r in rules_list}

    closed = [r for r in rows if r.get("close_reason") in ("triggered_target", "triggered_stop")]
    by_rule: dict[str, list[dict]] = defaultdict(list)
    for r in closed:
        by_rule[r.get("rule_id", "")].append(r)

    today   = datetime.now().strftime("%Y-%m-%d")
    updates = []

    for rule_id, recs in by_rule.items():
        if len(recs) < MIN_TRADES:
            continue
        targets  = sum(1 for r in recs if r["close_reason"] == "triggered_target")
        win_rate = targets / len(recs)
        rule     = rules_map.get(rule_id)
        if not rule:
            continue

        is_disabled   = rule.get("disabled", False)
        current_conf  = rule.get("min_confidence", 0.4)

        # 停用：達標率太低
        if win_rate < DISABLE_WIN_RATE and not is_disabled:
            updates.append({
                "operation":       "DISABLE",
                "event":           rule_id,
                "reason":          f"累積達標率 {win_rate:.0%}（{targets}/{len(recs)} 筆），低於 {DISABLE_WIN_RATE:.0%} 閾值",
                "evidence_source": "quant",
            })
            print(f"[report] 建議 DISABLE：「{rule_id}」達標率 {win_rate:.0%}")
            continue  # 已停用就不再調整門檻

        # Confidence 門檻調整（僅針對未停用規則）
        if not is_disabled:
            if win_rate > BOOST_WIN_RATE:
                new_conf = 0.30
            elif win_rate < TIGHTEN_WIN_RATE:
                new_conf = 0.50
            else:
                new_conf = 0.40

            if abs(current_conf - new_conf) > 0.01:
                updates.append({
                    "operation":       "CONFIDENCE_ADJUST",
                    "event":           rule_id,
                    "min_confidence":  new_conf,
                    "reason":          f"累積達標率 {win_rate:.0%}（{len(recs)} 筆），調整信心門檻 {current_conf:.2f} → {new_conf:.2f}",
                    "evidence_source": "quant",
                })
                print(f"[report] 建議 CONFIDENCE_ADJUST：「{rule_id}」{current_conf:.2f} → {new_conf:.2f}")

    return updates


def _git_commit(filepath: str, date_str: str | None = None) -> None:
    root = str(config.project_root)
    label = date_str or datetime.now().strftime("%Y-%m-%d")
    try:
        subprocess.run(["git", "add", filepath], cwd=root, check=True, capture_output=True)
        result = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=root, capture_output=True
        )
        if result.returncode == 0:
            print("[report] PERFORMANCE.md 無變動，略過 commit。")
            return
        subprocess.run(
            ["git", "commit", "-m", f"perf: update PERFORMANCE.md {label}"],
            cwd=root, check=True, capture_output=True
        )
        subprocess.run(["git", "push"], cwd=root, check=True, capture_output=True)
        print("[report] PERFORMANCE.md 已推送到 GitHub。")
    except subprocess.CalledProcessError as e:
        print(f"[report] git 操作失敗（非致命）：{e.stderr.decode() if e.stderr else e}")


if __name__ == "__main__":
    run()
